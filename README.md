# mcp-ansible

[![Version](https://img.shields.io/github/v/tag/bergmann-max/mcp-ansible?label=version&color=blue&sort=semver&style=for-the-badge)](https://github.com/bergmann-max/mcp-ansible/tags)
[![FastMCP](https://img.shields.io/badge/dynamic/toml?url=https://raw.githubusercontent.com/bergmann-max/mcp-ansible/main/pyproject.toml&query=%24.project.dependencies%5B0%5D&label=fastmcp&color=5468FF&logo=python&logoColor=white&style=for-the-badge)](https://github.com/jlowin/fastmcp)
[![Ansible](https://img.shields.io/badge/dynamic/toml?url=https://raw.githubusercontent.com/bergmann-max/mcp-ansible/main/pyproject.toml&query=%24.project.dependencies%5B1%5D&label=ansible-core&color=red&logo=ansible&logoColor=white&style=for-the-badge)](https://docs.ansible.com/projects/ansible)
[![ansible-lint](https://img.shields.io/badge/dynamic/toml?url=https://raw.githubusercontent.com/bergmann-max/mcp-ansible/main/pyproject.toml&query=%24.project.dependencies%5B2%5D&label=ansible-lint&color=yellow&logo=ansible&logoColor=white&style=for-the-badge)](https://docs.ansible.com/projects/lint/)
[![License](https://img.shields.io/badge/license-MIT-blue?style=for-the-badge)](https://github.com/bergmann-max/mcp-ansible/blob/main/LICENSE)

Standalone MCP server for linting, syntax-checking, and validating Ansible playbooks and roles.

## Tools

Every tool returns `{ok, stdout, stderr, <structured key>}`. The structured key holds parsed output for LLM consumption; raw `stdout`/`stderr` remain for debugging.

| Tool | Purpose | Structured key |
|------|---------|----------------|
| `lint_file` | Run ansible-lint on file or role directory | `findings[]` |
| `syntax_check` | Validate playbook syntax without execution | `errors[]` |
| `diff_check` | Preview changes via `--check --diff` | `recap{host}` |
| `gather_facts` | Collect facts from a host via setup module | `facts{host}` |
| `list_hosts` | List hosts affected by a playbook | `hosts[]` |
| `list_tags` | List tags defined in a playbook | `tags[]` |

## Prerequisites

[`uv`](https://docs.astral.sh/uv/) installed. Python deps (`mcp`, `ansible-core`, `ansible-lint`) auto-install via `uvx` on first run.

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

Pin a tag: `git+https://github.com/bergmann-max/mcp-ansible@v0.2.0`.

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

## License

[MIT](LICENSE)

## Author

Max Bergmann
