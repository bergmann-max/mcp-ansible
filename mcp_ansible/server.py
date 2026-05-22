#!/usr/bin/env python3
"""
Ansible MCP Server
Provides tools to lint and validate Ansible playbooks and roles.
File creation is handled directly by the agent via its file tools.
IMPORTANT: Only stderr for logs, stdout is reserved for JSON-RPC.
"""
import argparse, logging, os, re, sys, json, signal, asyncio, subprocess, configparser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, unquote

from fastmcp import FastMCP, Context
from pydantic import BaseModel, Field

from . import __version__

log = logging.getLogger("mcp_ansible")

mcp = FastMCP("ansible")

_LINT_PROFILES = {"default", "min", "basic", "moderate", "safety", "shared", "production"}

# Opt-in path-containment guard. Off by default to preserve trusted-local-code
# semantics; flip MCP_ANSIBLE_STRICT_ROOT=1 to reject paths outside the root.
_STRICT_ROOT = os.getenv("MCP_ANSIBLE_STRICT_ROOT") == "1"


def _timeout(env_name: str, default: int) -> int:
    """Read a per-tool timeout from env, fall back to `default` on unset/garbage."""
    raw = os.getenv(env_name)
    if not raw:
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except ValueError:
        return default


# ── Structured response models ────────────────────────────────────────────────
# FastMCP 3.x serializes Pydantic models with full JSON-Schema; Kiro's dynamic
# Power loader uses these schemas to route tool calls.

class BaseResult(BaseModel):
    """Common envelope for every tool. Error paths populate `error`."""
    ok: bool
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


class LintFinding(BaseModel):
    rule: str | None = None
    severity: str | None = None
    file: str | None = None
    line: int | None = None
    message: str | None = None
    url: str | None = None


class LintResult(BaseResult):
    findings: list[LintFinding] = Field(default_factory=list)


class SyntaxResult(BaseResult):
    errors: list[str] = Field(default_factory=list)


class DiffResult(BaseResult):
    recap: dict[str, dict[str, int]] = Field(default_factory=dict)


class FactsResult(BaseResult):
    facts: dict[str, dict[str, Any]] = Field(default_factory=dict)


class HostsResult(BaseResult):
    hosts: list[str] = Field(default_factory=list)


class TagsResult(BaseResult):
    tags: list[str] = Field(default_factory=list)


def _err(model_cls: type[BaseResult], reason: str) -> BaseResult:
    """Build a failure response for the given tool's result model.

    All error paths share the shape `{ok=False, stdout="", stderr=reason,
    error=reason, <structured>=<empty>}` — structured fields fall back to
    each model's declared default.
    """
    return model_cls(ok=False, stdout="", stderr=reason, error=reason)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _file_uri_to_path(uri) -> Path | None:
    parsed = urlparse(str(uri))
    if parsed.scheme != "file":
        return None
    return Path(unquote(parsed.path))


async def _resolve_root(
    ctx: Context, project_root: str, target_hint: str = "",
) -> tuple[Path | None, str | None]:
    """Resolve the workspace.

    Priority:
    1. MCP roots — first usable file:// root, when the client advertises the capability.
       When `target_hint` is an absolute path AND multiple roots are returned,
       prefer the root that is a parent of the target.
    2. Explicit project_root argument — fallback for clients without roots support.

    Returns `(root, None)` on success or `(None, error_message)` on failure.
    """
    try:
        roots = await ctx.list_roots()
        candidates: list[Path] = []
        for r in roots:
            p = _file_uri_to_path(r.uri)
            if p and p.is_absolute() and p.exists():
                candidates.append(p)
        # If hint is absolute, prefer the root that contains the target.
        if candidates and target_hint:
            hint = Path(target_hint)
            if hint.is_absolute():
                hint_resolved = hint.resolve()
                for c in candidates:
                    try:
                        hint_resolved.relative_to(c.resolve())
                        return c, None
                    except ValueError:
                        continue
        if candidates:
            return candidates[0], None
    except Exception as e:
        log.warning("roots lookup failed: %s", e)

    if not project_root:
        return None, (
            "Workspace not resolved. Either the client must advertise the MCP "
            "'roots' capability, or pass project_root as an absolute path."
        )
    if project_root.startswith("${") or project_root.startswith("$("):
        return None, (
            f"project_root contains an unresolved variable: {project_root!r}. "
            "Pass the actual absolute path, e.g. '/home/user/my-project'."
        )
    p = Path(project_root)
    if not p.is_absolute():
        return None, f"project_root must be an absolute path, got: {project_root!r}"
    if not p.exists():
        return None, f"project_root does not exist: {project_root!r}"
    return p, None


def _validate_input_path(value: str, kind: str, root: Path) -> tuple[Path | None, str | None]:
    """Validate a user-supplied file or directory path argument.

    - Empty value → error
    - Relative path → resolved against `root`
    - Absolute path → resolved (symlinks/`..` normalized)
    - Must exist on disk

    Default: absolute paths outside `root` are accepted (trusted local code).
    Set MCP_ANSIBLE_STRICT_ROOT=1 to reject paths that resolve outside `root`.
    """
    if not value:
        return None, f"{kind} is required"
    p = Path(value)
    p = (root / p).resolve() if not p.is_absolute() else p.resolve()
    if not p.exists():
        return None, f"{kind} does not exist: {value!r}"
    if _STRICT_ROOT:
        try:
            p.relative_to(root.resolve())
        except ValueError:
            return None, (
                f"{kind} resolves outside project root (strict mode): {value!r}. "
                "Unset MCP_ANSIBLE_STRICT_ROOT to allow paths outside the root."
            )
    return p, None


def _resolve_inventory(root: Path) -> str | None:
    """Resolve inventory in order of precedence.

    Returns an `ansible -i` argument string — either a single path or a
    comma-separated list. Honors `ANSIBLE_INVENTORY` (passed through verbatim
    to support comma-lists) and `ansible.cfg [defaults] inventory` (comma-list
    resolved against the project root).
    """
    if env := os.getenv("ANSIBLE_INVENTORY"):
        return env
    cfg_path = root / "ansible.cfg"
    if cfg_path.exists():
        cfg = configparser.ConfigParser()
        cfg.read(cfg_path)
        if cfg.has_option("defaults", "inventory"):
            raw = cfg.get("defaults", "inventory").strip()
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            resolved: list[str] = []
            for part in parts:
                rp = (root / part).resolve()
                if rp.exists():
                    resolved.append(str(rp))
            if resolved:
                return ",".join(resolved)
    for candidate in ["hosts.yml", "hosts.yaml", "hosts.ini",
                      "inventory/hosts.yml", "inventory/hosts.yaml", "inventory/hosts.ini"]:
        p = root / candidate
        if p.exists():
            return str(p)
    return None


def _require_inventory(root: Path) -> tuple[str | None, str | None]:
    inv = _resolve_inventory(root)
    if inv is None:
        return None, (
            "No inventory found. Provide one via:\n"
            "  1. ANSIBLE_INVENTORY env var\n"
            "  2. ansible.cfg [defaults] inventory\n"
            "  3. hosts.yml or hosts.ini in project root"
        )
    return inv, None


def _hardened_env() -> dict:
    """Env that keeps parsers deterministic regardless of user shell config."""
    return {
        **os.environ,
        "ANSIBLE_STDOUT_CALLBACK": "default",
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_FORCE_COLOR": "0",
    }


def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> dict:
    """Run subprocess with hardened env, no stdin, process-group cleanup on timeout.

    - `stdin=DEVNULL` prevents ansible from hanging on sudo/SSH password prompts.
    - `start_new_session=True` + `os.killpg` reaps ssh children on timeout.
    - Env hardening pins the stdout callback and disables color so parsers
      (`_parse_play_recap`, `_parse_list_hosts`, …) stay stable.
    """
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True, env=_hardened_env(),
            start_new_session=True,
        )
    except Exception as e:
        return {"ok": False, "stdout": "", "stderr": str(e)}
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return {"ok": proc.returncode == 0, "stdout": stdout.strip(), "stderr": stderr.strip()}
    except subprocess.TimeoutExpired:
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(pgid, signal.SIGKILL)
                proc.communicate(timeout=5)
        except ProcessLookupError:
            pass
        return {
            "ok": False,
            "stdout": "",
            "stderr": (
                f"Command timed out after {timeout}s: {' '.join(cmd)}\n"
                "For large playbooks or many hosts, this is expected. "
                "Consider using --limit to scope the operation."
            ),
        }
    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        return {"ok": False, "stdout": "", "stderr": str(e)}


async def _run_async(cmd: list[str], cwd: Path, timeout: int = 60) -> dict:
    """Off-load blocking subprocess to a worker thread so the MCP event loop
    can keep serving other tool calls in parallel."""
    return await asyncio.to_thread(_run, cmd, cwd, timeout)


# ── Output parsers ────────────────────────────────────────────────────────────

def _parse_lint_findings(stdout: str) -> list[dict]:
    """Parse ansible-lint --format json output into a compact findings list."""
    try:
        items = json.loads(stdout) if stdout else []
    except json.JSONDecodeError:
        return []
    findings = []
    for it in items:
        loc = it.get("location") or {}
        line = None
        pos = loc.get("positions") or {}
        if pos.get("begin"):
            line = pos["begin"].get("line")
        elif loc.get("lines"):
            line = loc["lines"].get("begin")
        findings.append({
            "rule": it.get("check_name"),
            "severity": it.get("severity"),
            "file": loc.get("path"),
            "line": line,
            "message": it.get("description"),
            "url": it.get("url"),
        })
    return findings


def _parse_setup_facts(stdout: str) -> dict[str, dict]:
    """Parse `ansible <host_or_group> -m setup` output.

    Output contains one block per host: 'hostname | STATUS => {json}'.
    Returns `{hostname: facts_dict}` for SUCCESS hosts only. UNREACHABLE!/FAILED!
    hosts are skipped. Empty dict if no parseable blocks.
    """
    results: dict[str, dict] = {}
    pattern = re.compile(
        r"^(\S+)\s*\|\s*(SUCCESS|UNREACHABLE!|FAILED!)\s*=>\s*",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(stdout))
    for i, m in enumerate(matches):
        host = m.group(1)
        status = m.group(2)
        if status != "SUCCESS":
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(stdout)
        body = stdout[start:end].strip()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        results[host] = payload.get("ansible_facts", payload)
    return results


def _parse_list_hosts(stdout: str) -> list[str]:
    """Parse `ansible-playbook --list-hosts` output."""
    hosts: list[str] = []
    in_block = False
    for line in stdout.splitlines():
        if re.match(r"\s*hosts \(\d+\):", line):
            in_block = True
            continue
        if in_block:
            stripped = line.strip()
            if not stripped or stripped.startswith("play #"):
                in_block = False
                continue
            hosts.append(stripped)
    return hosts


def _parse_list_tags(stdout: str) -> list[str]:
    """Parse `ansible-playbook --list-tags` output: 'TASK TAGS: [a, b]'."""
    tags: set[str] = set()
    for m in re.finditer(r"TAGS:\s*\[([^\]]*)\]", stdout):
        for t in m.group(1).split(","):
            t = t.strip()
            if t:
                tags.add(t)
    return sorted(tags)


def _parse_play_recap(stdout: str) -> dict[str, dict]:
    """Parse the PLAY RECAP block from ansible-playbook output."""
    recap: dict[str, dict] = {}
    in_recap = False
    for line in stdout.splitlines():
        if line.startswith("PLAY RECAP"):
            in_recap = True
            continue
        if in_recap:
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^(\S+)\s*:\s*(.+)$", line)
            if not m:
                continue
            host = m.group(1)
            stats = {}
            for kv in re.finditer(r"(\w+)=(\d+)", m.group(2)):
                stats[kv.group(1)] = int(kv.group(2))
            recap[host] = stats
    return recap


# ── Validation Tools ──────────────────────────────────────────────────────────

@mcp.tool
async def lint_file(
    path: str,
    ctx: Context,
    project_root: str = "",
    profile: str = "production",
) -> LintResult:
    """Runs ansible-lint on a file or directory.

    The workspace is taken from the MCP client's roots when supported. Pass
    project_root (absolute) only as a fallback for clients without roots.

    Returns `findings: [{rule, severity, file, line, message, url}]` alongside
    raw `stdout`/`stderr`.

    Args:
        path:         Path to the file or role directory to lint (absolute, or
                      relative to the project root).
        project_root: Optional. Absolute path to the Ansible project root.
        profile:      ansible-lint profile. One of: min, basic, moderate, safety,
                      shared, production. Use "default" (or empty) to respect
                      the project's .ansible-lint config instead.
    """
    # Normalize empty profile to canonical "default" token.
    if profile == "":
        profile = "default"
    if profile not in _LINT_PROFILES:
        return _err(
            LintResult,
            f"unknown profile: {profile!r}. Allowed: {sorted(_LINT_PROFILES)}",
        )
    root, err = await _resolve_root(ctx, project_root, target_hint=path)
    if err:
        return _err(LintResult, err)
    target, err = _validate_input_path(path, "path", root)
    if err:
        return _err(LintResult, err)
    cmd = ["ansible-lint"]
    if profile != "default":
        cmd.extend(["--profile", profile])
    cmd.extend(["--format", "json", str(target)])
    raw = await _run_async(cmd, cwd=root, timeout=_timeout("MCP_ANSIBLE_TIMEOUT_LINT", 300))
    return LintResult(
        ok=raw["ok"],
        stdout=raw["stdout"],
        stderr=raw["stderr"],
        findings=[LintFinding(**f) for f in _parse_lint_findings(raw["stdout"])],
    )


@mcp.tool
async def syntax_check(playbook: str, ctx: Context, project_root: str = "") -> SyntaxResult:
    """Checks the syntax of a playbook without executing it.

    No inventory required — `ansible-playbook --syntax-check` parses the
    playbook standalone.

    Returns `errors: [str]` populated from stderr when syntax invalid.

    Args:
        playbook:     Path to the playbook file (absolute, or relative to root).
        project_root: Optional. Absolute path to the Ansible project root.
    """
    root, err = await _resolve_root(ctx, project_root, target_hint=playbook)
    if err:
        return _err(SyntaxResult, err)
    target, err = _validate_input_path(playbook, "playbook", root)
    if err:
        return _err(SyntaxResult, err)
    raw = await _run_async(
        ["ansible-playbook", "--syntax-check", str(target)],
        cwd=root, timeout=60,
    )
    if raw["ok"]:
        errors: list[str] = []
    else:
        errors = [
            line.strip()
            for line in raw["stderr"].splitlines()
            if line.strip() and not line.strip().startswith("[WARNING]")
        ]
    return SyntaxResult(
        ok=raw["ok"],
        stdout=raw["stdout"],
        stderr=raw["stderr"],
        errors=errors,
    )


@mcp.tool
async def diff_check(
    playbook: str, ctx: Context, project_root: str = "", limit: str = "",
) -> DiffResult:
    """Runs a playbook in check+diff mode to preview changes without applying them.

    Returns `recap: {host: {ok, changed, failed, ...}}` parsed from PLAY RECAP.
    Diff bodies remain in raw `stdout`.

    Args:
        playbook:     Path to the playbook file (absolute, or relative to root).
        project_root: Optional. Absolute path to the Ansible project root.
        limit:        Optional host limit, e.g. 'webservers' or 'web01.example.com'
    """
    root, err = await _resolve_root(ctx, project_root, target_hint=playbook)
    if err:
        return _err(DiffResult, err)
    target, err = _validate_input_path(playbook, "playbook", root)
    if err:
        return _err(DiffResult, err)
    inv, err = _require_inventory(root)
    if err:
        return _err(DiffResult, err)
    cmd = ["ansible-playbook", "--check", "--diff", "-i", inv, str(target)]
    if limit:
        cmd.extend(["--limit", limit])
    raw = await _run_async(cmd, cwd=root, timeout=_timeout("MCP_ANSIBLE_TIMEOUT_PLAY", 300))
    return DiffResult(
        ok=raw["ok"],
        stdout=raw["stdout"],
        stderr=raw["stderr"],
        recap=_parse_play_recap(raw["stdout"]),
    )


@mcp.tool
async def gather_facts(host: str, ctx: Context, project_root: str = "") -> FactsResult:
    """Collects Ansible facts from a host or group using the setup module.

    Returns `facts: {hostname: {...}}` — a mapping per host. Single-host calls
    return a one-entry mapping for consistent shape.

    Args:
        host:         Hostname or group from the inventory.
        project_root: Optional. Absolute path to the Ansible project root.
    """
    if not host:
        return _err(FactsResult, "host is required")
    root, err = await _resolve_root(ctx, project_root)
    if err:
        return _err(FactsResult, err)
    inv, err = _require_inventory(root)
    if err:
        return _err(FactsResult, err)
    raw = await _run_async(
        ["ansible", "-i", inv, host, "-m", "setup"],
        cwd=root,
        timeout=_timeout("MCP_ANSIBLE_TIMEOUT_FACTS", 300),
    )
    return FactsResult(
        ok=raw["ok"],
        stdout=raw["stdout"],
        stderr=raw["stderr"],
        facts=_parse_setup_facts(raw["stdout"]) if raw["ok"] else {},
    )


@mcp.tool
async def list_hosts(
    playbook: str, ctx: Context, project_root: str = "", limit: str = "",
) -> HostsResult:
    """Lists all hosts that would be affected by a playbook run.

    Returns `hosts: [str]` alongside raw output.

    Args:
        playbook:     Path to the playbook file (absolute, or relative to root).
        project_root: Optional. Absolute path to the Ansible project root.
        limit:        Optional host limit, e.g. 'webservers' or 'web01.example.com'
    """
    root, err = await _resolve_root(ctx, project_root, target_hint=playbook)
    if err:
        return _err(HostsResult, err)
    target, err = _validate_input_path(playbook, "playbook", root)
    if err:
        return _err(HostsResult, err)
    inv, err = _require_inventory(root)
    if err:
        return _err(HostsResult, err)
    cmd = ["ansible-playbook", "--list-hosts", "-i", inv, str(target)]
    if limit:
        cmd.extend(["--limit", limit])
    raw = await _run_async(cmd, cwd=root)
    return HostsResult(
        ok=raw["ok"],
        stdout=raw["stdout"],
        stderr=raw["stderr"],
        hosts=_parse_list_hosts(raw["stdout"]),
    )


@mcp.tool
async def list_tags(playbook: str, ctx: Context, project_root: str = "") -> TagsResult:
    """Lists all tags defined in a playbook.

    Returns `tags: [str]` (deduplicated, sorted) alongside raw output.

    Args:
        playbook:     Path to the playbook file (absolute, or relative to root).
        project_root: Optional. Absolute path to the Ansible project root.
    """
    root, err = await _resolve_root(ctx, project_root, target_hint=playbook)
    if err:
        return _err(TagsResult, err)
    target, err = _validate_input_path(playbook, "playbook", root)
    if err:
        return _err(TagsResult, err)
    inv, err = _require_inventory(root)
    if err:
        return _err(TagsResult, err)
    raw = await _run_async(
        ["ansible-playbook", "--list-tags", "-i", inv, str(target)],
        cwd=root,
    )
    return TagsResult(
        ok=raw["ok"],
        stdout=raw["stdout"],
        stderr=raw["stderr"],
        tags=_parse_list_tags(raw["stdout"]),
    )


# ── Entry Point ───────────────────────────────────────────────────────────────

class _StderrParser(argparse.ArgumentParser):
    """argparse parser that routes --help and errors to stderr.

    stdio transport reserves stdout for JSON-RPC; default argparse writes
    --help to stdout, which would corrupt the first frame.
    """
    def _print_message(self, message, file=None):
        # Ignore `file` — argparse's HelpAction passes sys.stdout explicitly.
        # stdio transport reserves stdout for JSON-RPC; force stderr.
        if message:
            sys.stderr.write(message)


def _build_parser() -> argparse.ArgumentParser:
    p = _StderrParser(
        prog="mcp-ansible",
        description="Ansible MCP server — lint, syntax-check, dry-run validation tools.",
    )
    p.add_argument(
        "--version", action="store_true",
        help="print version to stderr and exit",
    )
    p.add_argument(
        "--transport", default="stdio", choices=["stdio"],
        help="transport (default: stdio)",
    )
    return p


def _configure_logging() -> None:
    """Bind mcp_ansible + third-party loggers to a single stderr handler.

    stdio transport reserves stdout for JSON-RPC; any log line on stdout would
    corrupt the first frame. We avoid `logging.basicConfig()` because it can
    attach handlers to the root logger that may already route elsewhere
    depending on parent process configuration.
    """
    level = os.getenv("MCP_ANSIBLE_LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(name)s] %(levelname)s: %(message)s"))

    own = logging.getLogger("mcp_ansible")
    own.setLevel(level)
    own.handlers = [handler]
    own.propagate = False

    # Third-party libraries that may default to stdout via root logger.
    # Pin them to the same stderr handler and suppress below WARNING.
    for name in ("ansible", "urllib3"):
        third = logging.getLogger(name)
        third.handlers = [handler]
        third.setLevel(logging.WARNING)
        third.propagate = False


def main():
    parser = _build_parser()
    args = parser.parse_args()
    if args.version:
        print(f"mcp-ansible {__version__}", file=sys.stderr)
        sys.exit(0)
    _configure_logging()
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
