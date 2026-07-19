# Dexter Agentic NetOps

Dexter is a portable Agent Skill for guarded network-operations workflows across Cisco Catalyst Center, ServiceNow, GitHub, and CSV-sourced CMDB data. It is repository-local, with no plugin packaging or vendor-specific runtime service.

## Supported agents

The canonical skill is `.agents/skills/dexter`, which Codex and Kimi Code can discover at project scope. `.claude/skills/dexter` is a repository symlink to the same files for Claude Code.

Invoke it with the syntax supported by your agent:

```text
# Codex
$dexter show the Catalyst Center inventory

# Claude Code
/dexter show the Catalyst Center inventory

# Kimi Code
/skill:dexter show the Catalyst Center inventory
```

All three can also select Dexter automatically from a matching natural-language request. Invocation syntax and permission prompts remain agent-specific.

## Capabilities

- Read Catalyst Center inventory, interfaces, sites, health, issues, clients, topology, templates, neighbors, and guarded `show` commands.
- Query and summarize ServiceNow incidents, tasks, CMDB network gear, hardware assets, and explicitly selected tables.
- Explore repositories owned by one configured GitHub account using read-only API requests.
- Preview and manage modeled ServiceNow DNS/IP CMDB relationships.
- Preview and import Catalyst inventory as ServiceNow Network Gear and Hardware Assets.
- Preview and process approved GitHub CSV data into modeled ServiceNow DNS records.

ServiceNow DNS workflows create CMDB metadata; they do not configure an authoritative DNS server.

## Setup

Dexter requires Python 3.11 or newer.

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
```

Edit `.env` and provide only the integrations needed for your demo. Keep `.env` local; it is ignored by Git. You may instead point `DEXTER_ENV_FILE` to an environment file stored elsewhere.

Configuration safeguards:

- `GITHUB_OWNER` is the only GitHub owner handlers may access.
- `SERVICENOW_DEV_INSTANCE` must use HTTPS and its hostname must exactly match `SERVICENOW_ALLOWED_HOST`.
- Secrets are read from the environment and never accepted as CLI arguments.

## Direct launcher

Agents should follow Dexter's `SKILL.md`, but every handler can also be exercised consistently from the repository root:

```bash
./scripts/dexter catalyst-center list-devices --pretty
./scripts/dexter github list-repos --pretty
./scripts/dexter servicenow-query list-records --record-type incidents --pretty
./scripts/dexter servicenow-dns list-dns --pretty
./scripts/dexter servicenow-csv-dns preview-github-csv-dns \
  --repo example-repository --path data/dns.csv --domain example.com --pretty
./scripts/dexter servicenow-network-gear list-gear --pretty
```

Set `DEXTER_PYTHON` if you intentionally use a Python interpreter other than `.venv/bin/python`.

## Safety model

- Read-only discovery or a dry-run preview comes before every supported mutation.
- Mutating commands require an exact plan, explicit user approval, and `--confirm`.
- Catalyst Center permits only read operations except the guarded physical access-port bounce workflow.
- GitHub operations are read-only and restricted to the configured owner.
- ServiceNow handlers enforce host and table allowlists.
- Imports validate the complete source before the first write, are deterministic, and are designed for safe reruns.
- Unsupported operations must be refused instead of bypassing a handler.

Never demonstrate destructive workflows against production systems. Use authorized lab or development instances and review the returned plan before confirming a change.

## Test and validate

```bash
./scripts/test
```

The test suite mocks external mutations and verifies routing, validation, idempotency, ownership markers, confirmation gates, and failure handling. Live integration checks are intentionally separate because they require authorized credentials and reachable systems.

## Demo flow

For a short YouTube demonstration:

1. Show the portable skill layout and sanitized `.env.example`.
2. Invoke Dexter to list Catalyst Center inventory.
3. Query and summarize ServiceNow incidents.
4. List CMDB network gear or hardware assets.
5. Preview a DNS or inventory import and point out that no mutation occurred.
6. Explain the explicit approval gate; use a lab-only record if demonstrating a confirmed mutation.
7. Run `./scripts/test` and show the passing summary.

Before recording, clear terminal history and notifications, hide `.env`, tokens, usernames, instance identifiers, internal hostnames, IP addresses, record IDs, and browser bookmarks. Review the final recording frame by frame before publishing.

## Repository structure

```text
.
├── .agents/skills/dexter/       # canonical portable Agent Skill
│   ├── SKILL.md                  # Dexter router and safety policy
│   ├── agents/openai.yaml        # optional Codex presentation metadata
│   └── skills/*/                 # integration workflows and scripts
├── .claude/skills/dexter         # symlink for Claude Code discovery
├── scripts/dexter                # uniform local handler launcher
├── scripts/test                  # centralized test runner
├── tests/                        # mocked unit tests
├── .env.example                  # sanitized configuration template
└── pyproject.toml                # one dependency definition
```

The `agents/openai.yaml` file is optional metadata for Codex and is ignored by other agents; the operational instructions remain in the shared `SKILL.md` files.
