# mcp-ansible

[![Version](https://img.shields.io/github/v/tag/bergmann-max/mcp-ansible?label=version&color=blue&sort=semver&style=for-the-badge)](https://github.com/bergmann-max/mcp-ansible/tags)
[![FastMCP](https://img.shields.io/badge/dynamic/toml?url=https://raw.githubusercontent.com/bergmann-max/mcp-ansible/main/pyproject.toml&query=%24.project.dependencies%5B0%5D&label=fastmcp&color=5468FF&logo=python&logoColor=white&style=for-the-badge)](https://github.com/jlowin/fastmcp)
[![Ansible](https://img.shields.io/badge/dynamic/toml?url=https://raw.githubusercontent.com/bergmann-max/mcp-ansible/main/pyproject.toml&query=%24.project.dependencies%5B1%5D&label=ansible-core&color=red&logo=ansible&logoColor=white&style=for-the-badge)](https://docs.ansible.com/projects/ansible)
[![ansible-lint](https://img.shields.io/badge/dynamic/toml?url=https://raw.githubusercontent.com/bergmann-max/mcp-ansible/main/pyproject.toml&query=%24.project.dependencies%5B2%5D&label=ansible-lint&color=yellow&logo=ansible&logoColor=white&style=for-the-badge)](https://docs.ansible.com/projects/lint/)
[![License](https://img.shields.io/badge/license-MIT-blue?style=for-the-badge)](https://github.com/bergmann-max/mcp-ansible/blob/main/LICENSE)

MCP server for linting, syntax-checking, and validating Ansible playbooks and roles.

## Tools

Every tool returns `{ok, stdout, stderr, <structured key>}`. The structured key holds parsed output for LLM consumption; raw `stdout`/`stderr` remain for debugging.

| Tool | Purpose | Structured key |
|------|---------|----------------|
| `lint_file` | Run ansible-lint on file or role directory | `findings[]` |
| `syntax_check` | Validate playbook syntax without execution | `errors[]` |
| `diff_check` | Preview changes via `--check --diff` (accepts `limit`) | `recap{host}` |
| `gather_facts` | Collect facts from a host via setup module | `facts{host}` |
| `list_hosts` | List hosts affected by a playbook (accepts `limit`) | `hosts[]` |
| `list_tags` | List tags defined in a playbook | `tags[]` |

## Prerequisites

[`uv`](https://docs.astral.sh/uv/) installed. Python >=3.12. Python deps (`fastmcp`, `ansible-core`, `ansible-lint`) auto-install via `uvx` on first run.

```bash
# Linux/macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## Install

Add to your MCP client config:

```json
{
  "mcpServers": {
    "ansible": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/bergmann-max/mcp-ansible@main",
        "mcp-ansible"
      ]
    }
  }
}
```

Pin a tag: `git+https://github.com/bergmann-max/mcp-ansible@v0.3.0`.

Print version and exit: `uvx --from git+https://github.com/bergmann-max/mcp-ansible mcp-ansible --version` (prints to stderr; stdout is reserved for JSON-RPC).

## Workspace resolution

Tools resolve the project root via the MCP `roots` capability when the client advertises it. Otherwise, pass `project_root` as an **absolute** path on each call. Relative paths and unresolved `${workspaceFolder}` placeholders are rejected.

## Inventory resolution

In order of precedence:

1. `ANSIBLE_INVENTORY` env var
2. `ansible.cfg` → `[defaults] inventory`
3. Fallback files in project root: `hosts.yml`, `hosts.yaml`, `hosts.ini`, `inventory/hosts.yml`, `inventory/hosts.yaml`, `inventory/hosts.ini`

Tools that need an inventory (`diff_check`, `gather_facts`, `list_hosts`, `list_tags`) fail when none is found. `syntax_check` parses the playbook standalone and needs no inventory.

## Lint profiles

`lint_file` accepts a `profile` argument:

- `min`, `basic`, `moderate`, `safety`, `shared`, `production` (default)
- `default` (or empty) — respect the project's `.ansible-lint` config instead

## Environment variables

| Variable | Default | Effect |
|----------|---------|--------|
| `ANSIBLE_INVENTORY` | — | Inventory path or comma-list. Highest precedence over `ansible.cfg` and fallback files. Passed verbatim to `ansible -i`. |
| `MCP_ANSIBLE_STRICT_ROOT` | unset | Opt-in path-containment guard. Default off: absolute paths outside the project root are accepted (trusted local code). Set to `1` to reject paths that resolve outside the root. |
| `MCP_ANSIBLE_TIMEOUT_LINT` | `300` | Per-tool timeout in seconds for `lint_file`. Non-int or `<=0` falls back silently. |
| `MCP_ANSIBLE_TIMEOUT_PLAY` | `300` | Timeout for `diff_check`. |
| `MCP_ANSIBLE_TIMEOUT_FACTS` | `300` | Timeout for `gather_facts`. |
| `MCP_ANSIBLE_TIMEOUT_SYNTAX` | `60` | Timeout for `syntax_check`. |
| `MCP_ANSIBLE_TIMEOUT_LIST` | `60` | Timeout for `list_hosts` and `list_tags`. |
| `MCP_ANSIBLE_LOG_LEVEL` | `INFO` | Log level for the `mcp_ansible` logger. Logs always go to stderr. |

## License

[MIT](LICENSE)

## Author

Max Bergmann
