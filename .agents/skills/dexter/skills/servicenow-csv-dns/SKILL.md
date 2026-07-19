---
name: servicenow-csv-dns
description: Deterministically previews, imports, verifies, and lists ServiceNow DNS CMDB mappings sourced from CSV files owned by the configured GitHub account. Use for DNS/A/AAAA columns, address-plan CSV data, remote GitHub CSV mappings, or a small-model-safe workflow.
---

# Managing ServiceNow DNS from GitHub CSV

Read a remote CSV through the bundled `github-explorer` workflow and create mappings through `servicenow-dns`. GitHub access remains read-only and ServiceNow writes retain the DNS workflow's confirmation and table restrictions.

This creates ServiceNow CMDB DNS Name CIs, IP Address CIs, and junction records. It does not update an authoritative DNS server and does not create real A, AAAA, or PTR records.

## Prerequisites

1. Configure both existing GitHub and ServiceNow skills.
2. Install the project with `.venv/bin/pip install -e .`.
3. Configure `GITHUB_OWNER` and the ServiceNow host allowlist in the Dexter environment.

## Mandatory Small-Model Algorithm

Follow these steps exactly. Do not parse CSV in chat, copy rows, build shell loops, or invoke `create-dns` per row.

1. Confirm the user owns or is authorized to use the requested domain.
2. Select `A` unless the user explicitly requests `AAAA` or `both`.
3. Run `preview-github-csv-dns` once without `--confirm`.
4. Read `status` before interpreting `results`.
5. If `status=error`, report `results.errors` when present and stop. Do not omit or repair rows manually.
6. Present `results.plan`; ask for approval unless the user already explicitly requested creation for that exact repository, path, domain, and record type.
7. Run `import-github-csv-dns` once with `--confirm`.
8. Report `created_count`, `unchanged_count`, `skipped_count`, and any stopped error.
9. Run `verify-github-csv-dns` with the same source options.
10. Never claim authoritative DNS or PTR records were created.

## Commands

Run from the repository root:

`./scripts/dexter servicenow-csv-dns <command> [options] --pretty`

| Command | Required options | Purpose |
| --- | --- | --- |
| `preview-github-csv-dns` | `--repo`, `--path`, `--domain` | Read, validate, normalize, sort, and preview without mutation. |
| `import-github-csv-dns` | Same options and `--confirm` | Execute the validated plan idempotently. Without confirmation it behaves as preview. |
| `delete-github-csv-dns` | Same options and `--confirm` | Delete only the exact validated DNS/IP mappings represented by the CSV. Without confirmation it previews. |
| `verify-github-csv-dns` | `--repo`, `--path`, `--domain` | Re-read the source and verify every planned CMDB mapping. |
| `list-dns` | — | Delegate to the existing ServiceNow DNS listing command; optional `--fqdn` or `--ip`. |

Common options:

- `--repo`: repository name or `configured-owner/name`; other owners are rejected.
- `--path`: repository-relative `.csv` path.
- `--ref`: optional branch, tag, or commit SHA.
- `--domain`: authorized suffix appended to each `DNS` label.
- `--record-type`: `A` (default), `AAAA`, or `both`.

## CSV Contract

The header must include `DNS`, `A`, and `AAAA` (case-insensitive). Other columns are ignored.

- Rows with neither a DNS label nor a selected address are skipped.
- A DNS label with no selected address is skipped and counted.
- An address without a DNS label is an error.
- `N/A`, `NA`, `none`, `null`, `-`, and blank are treated as missing.
- Hostnames, domains, IPv4 addresses, and IPv6 addresses are normalized and validated.
- Duplicate generated FQDNs or selected IPs block all mutation.
- Any validation error blocks the complete import before mutation.
- Plans are sorted case-insensitively by FQDN and include source row numbers.
- GitHub files truncated at the 250 KB skill limit are rejected.
- The source SHA is returned as `results.source.revision` for auditability.

The current ServiceNow DNS model permits one exact IP mapping per FQDN. Consequently, `--record-type both` is accepted only when each row supplies at most one selected address family. If a row has both A and AAAA values, preview returns an error and instructs the user to choose one family.

## Safety Boundaries

- GitHub operations are read-only and restricted to `GITHUB_OWNER` repositories.
- ServiceNow operations remain restricted to the development instance and DNS/IP CMDB tables.
- Import requires explicit approval and `--confirm`.
- CSV deletion requires explicit approval and `--confirm`, stops on the first ServiceNow error, and deletes no records outside the exact validated plan.
- Import stops on the first ServiceNow conflict; rerunning after correction is idempotent.
- No rows are deleted when absent from a later CSV.
- Never expose GitHub or ServiceNow credentials.
- Never execute fetched repository content.
- PTR records are unsupported because this workflow has no authoritative reverse-DNS integration.

## Output Contract

Every response contains `status`, `results`, and `next_steps`.

Important result fields:

- `source`: repository, path, requested ref, immutable GitHub blob SHA, and URL.
- `total_rows`, `planned_count`, `skipped_count`, `error_count`.
- `plan`, `skipped`, and `errors` during preview.
- `processed_count`, `created_count`, `unchanged_count`, and `outcomes` during import.
- `verified_count` and `missing_count` during verification.

Interpret outputs literally: `warning` is a non-mutating valid plan, `error` means stop, and `success` means the requested operation completed.
