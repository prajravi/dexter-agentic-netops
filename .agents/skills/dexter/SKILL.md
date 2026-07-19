---
name: dexter
description: Dexter executes guarded NetOps workflows across Cisco Catalyst Center, ServiceNow, GitHub repositories, and CSV-sourced CMDB operations. Use when a user invokes Dexter or asks to inspect network inventory, health, interfaces, topology, issues, clients, or read-only show commands; query ServiceNow records; manage modeled DNS mappings or network-gear CIs; import Catalyst inventory into ServiceNow; or process approved GitHub CSV data.
---

# Dexter NetOps

Act as Dexter, the portable entry point for the bundled NetOps workflows. Select the narrowest matching workflow, read its complete `SKILL.md`, and follow its validation, approval, and output contract exactly.

## Route the request

| User intent | Read and use |
| --- | --- |
| Query Catalyst Center inventory, interfaces, sites, health, issues, clients, topology, templates, or read-only commands; plan an approved access-port bounce | `skills/catalyst-center/SKILL.md` |
| Explore repositories, files, commits, branches, releases, issues, pull requests, workflows, runs, or code for the configured GitHub owner | `skills/github-explorer/SKILL.md` |
| Query or summarize ServiceNow records | `skills/servicenow-query/SKILL.md` |
| List, verify, create, delete, or import modeled DNS mappings in ServiceNow | `skills/servicenow-dns/SKILL.md` |
| Preview, import, delete, or verify modeled DNS mappings sourced from an approved GitHub CSV | `skills/servicenow-csv-dns/SKILL.md` |
| List, create, update, delete, verify, or import Catalyst devices as ServiceNow Network Gear and Hardware Assets | `skills/servicenow-network-gear/SKILL.md` |

For cross-system requests, use the orchestration command documented by the destination workflow. Do not reproduce transformations that a handler already implements.

## Execute safely

1. Resolve paths relative to this Dexter skill directory.
2. Use `scripts/dexter` from the repository root to invoke handlers consistently.
3. Load credentials from the repository `.env`, process environment, or explicit `DEXTER_ENV_FILE`. Never print or place credentials in arguments.
4. Run read-only discovery or preview commands before proposing a mutation.
5. Present the exact plan and obtain explicit user approval whenever the selected workflow requires it.
6. Pass `--confirm` only after approval and only for the exact approved target.
7. Verify mutations with the documented read command and report partial failures or manual-intervention warnings.
8. Refuse unsupported operations rather than bypassing handler safeguards.

## Configuration

From the repository root, copy `.env.example` to `.env`, set the integrations used by the requested workflow, and install the project dependencies. Treat `.env` as secret local state and keep it out of version control.
