---
name: servicenow-query
description: Deterministically queries incidents, changes, problems, tasks, network gear, hardware assets, or an explicitly selected ServiceNow table. Use when users ask to list, count, summarize, inspect, or verify ServiceNow records.
---

# ServiceNow Query

Query the configured ServiceNow instance through its REST Table API. Prefer deterministic record-type presets; use arbitrary tables only when the user supplies or approves the exact table and fields.

## Safety boundaries

- Load only the Dexter environment and require `SERVICENOW_DEV_INSTANCE` to match `SERVICENOW_ALLOWED_HOST` exactly.
- Use read-only HTTP `GET` requests. Never insert, update, delete, transition, or import records.
- Reject script-bearing encoded queries such as `javascript:` expressions.
- Never put credentials in commands or responses.
- On `403`, report insufficient permission and stop. Never retry against another instance.

## Runtime requirements

Use the ServiceNow connection and host allowlist documented in `.env.example`.

## Commands

Run from the repository root:

```bash
./scripts/dexter servicenow-query <command> [options] --pretty
```

| Command | Required options | Description |
| --- | --- | --- |
| `describe-record-types` | — | Return supported presets, tables, fields, ordering, and grouping. |
| `list-records` | `--record-type` | List a preset with fixed fields and pagination. |
| `summarize-records` | `--record-type` | Return grouped counts for a preset. |
| `query-table` | `--table` | Query one bounded page with optional query, fields, ordering, limit, and offset. |
| `list-all` | `--table` | Retrieve matching pages up to a protective record cap. |
| `summarize-table` | `--table`, `--group-by` | Count matching records grouped by one field. |
| `get-record` | `--table`, `--sys-id` | Retrieve one record by its 32-character `sys_id`. |
| `count-records` | `--table` | Count an optional encoded query through the Aggregate API. |

Preset record types are `incidents`, `changes`, `problems`, `tasks`, `network-gear`, and `hardware-assets`. The `task` table includes incident, change, problem, and other task-derived records; do not add these categories as if they were disjoint totals.

## Deterministic workflow

1. Map the user's noun exactly to a preset.
2. For a list, run `list-records --record-type <type> --pretty`.
3. For totals by state or type, run `summarize-records --record-type <type> --pretty`.
4. If unclear, run `describe-record-types --pretty`; do not guess.
5. Use generic commands only for a table not represented by a preset and when the exact table and fields are supplied or approved.
6. If `results.complete` is false, report the continuation offset and protective-cap warning.

Examples:

```bash
./scripts/dexter servicenow-query list-records --record-type incidents --pretty
./scripts/dexter servicenow-query summarize-records --record-type tasks --pretty
./scripts/dexter servicenow-query query-table --table incident --query "active=true" \
  --fields number,short_description,state,priority,assigned_to --limit 20 --pretty
./scripts/dexter servicenow-query get-record --table incident \
  --sys-id 0123456789abcdef0123456789abcdef --pretty
```

## Output contract

Every command prints JSON containing `status`, `results`, and `next_steps`. `success` and `warning` return exit code `0`; `error` returns exit code `1`. Treat an empty records array as a successful query with no matches.
