---
name: servicenow-dns
description: Deterministically lists, verifies, creates, deletes, and bulk-imports Catalyst Center management DNS mappings in the configured ServiceNow CMDB. Use for DNS Name CIs, IP Address CIs, FQDN-to-IP relationships, or approved Catalyst inventory imports; this does not create authoritative DNS records.
---

# ServiceNow DNS Records

Manages DNS Name configuration items in the dedicated ServiceNow development instance. Each mapping uses a DNS Name CI in `cmdb_ci_dns_name`, an IP Address CI in `cmdb_ci_ip_address`, and a junction record in `cmdb_ip_address_dns_name`. The junction record populates the **IP Address** related list in the classic UI and CMDB Workspace. This skill does not update an authoritative DNS server or guarantee that a name resolves on the network.

## Safety Boundaries

- Requires the configured ServiceNow URL to match `SERVICENOW_ALLOWED_HOST` exactly.
- Access is restricted to `cmdb_ci_dns_name`, `cmdb_ci_ip_address`, and `cmdb_ip_address_dns_name` through HTTP `GET`, `POST`, and guarded record-level `DELETE` only.
- Updates are not implemented.
- `create-dns` requires explicit user approval and `--confirm`.
- Creation is idempotent across the DNS CI, IP CI, and junction record.
- Creation stops on an FQDN conflict rather than replacing an existing IP address.
- `delete-dns` requires explicit approval and `--confirm`. It deletes the junction first, then the DNS CI, and deletes the IP CI only when no other DNS mapping references it.
- Host labels, domains, and IPv4/IPv6 addresses are strictly validated.
- Never place ServiceNow credentials in command arguments or chat responses.
- Use only a domain the user owns or is authorized to use. For documentation-only examples, prefer reserved domains such as `example.com`.
- This skill never creates authoritative DNS A or PTR records. It models mappings only in ServiceNow CMDB.
- `import-catalyst-dns` validates every device and blocks the entire import plan on missing or duplicate source values.
- Before importing, `import-catalyst-dns` reads all target CMDB mappings and classifies each as `create`, `repair`, `unchanged`, or `blocked`.
- Any FQDN conflict blocks the complete import before the first write, preventing partial creation.
- Bulk import sorts by hostname and never deletes mappings absent from Catalyst Center.

## Prerequisites

1. Install the project with `.venv/bin/pip install -e .`.
2. Configure the ServiceNow URL, allowed host, username, and password in the Dexter environment.
3. Ensure the ServiceNow account can read and create records in `cmdb_ci_dns_name`.

## Commands

Run from the repository root:

```bash
./scripts/dexter servicenow-dns <command> [options] --pretty
```

| Command | Required options | Description |
| --- | --- | --- |
| `list-dns` | — | Lists DNS-name CIs; optionally filter with `--fqdn` and/or `--ip`. |
| `verify-dns` | `--hostname`, `--domain`, `--ip` | Verifies the DNS CI, IP CI, and exact junction record. |
| `create-dns` | `--hostname`, `--domain`, `--ip`, `--confirm` | Idempotently creates any missing DNS CI, IP CI, and junction record after approval. |
| `delete-dns` | `--hostname`, `--domain`, `--ip`, `--confirm` | Deletes an exact mapping and removes its IP CI only when unreferenced. |
| `import-catalyst-dns` | `--domain`; add `--confirm` to execute | Retrieves Catalyst inventory itself and deterministically plans or imports every management mapping. |

## Deterministic Catalyst Center Inventory Workflow

Dexter must use this exact workflow:

1. Confirm the user owns or is authorized to use the requested domain. If no domain is supplied, ask for one; do not infer authorization from existing CMDB records.
2. Run `import-catalyst-dns --domain <domain> --pretty` without `--confirm`.
3. If status is `error`, report source errors or preflight conflicts and stop. Never guess missing hostnames or IPs.
4. Present `results.action_counts` and `results.plan`. Explain which mappings will be created, repaired, or left unchanged, then request approval unless the user already explicitly approved creation.
5. Run the same command once with `--confirm`.
6. Report `created_count`, `unchanged_count`, and any conflict.
7. Run `list-dns --pretty` to verify the final CMDB mappings.

If every planned mapping is `unchanged`, stop after the preview, report that no new records are needed, and do not run or retry a confirmed import.

Do not manually invoke Catalyst `list-devices`, copy values, build shell loops, append domains, or infer missing data for bulk import. The handler performs those transformations deterministically.

### Mapping invariants

- An inventory hostname is reduced to its first DNS label before the approved domain is appended.
- Every device requires a hostname and management IP.
- Duplicate generated FQDNs or management IPs block all mutation.
- Existing FQDNs mapped to a different IP block the complete import during preflight.
- Exact existing mappings are unchanged; missing DNS CI, IP CI, or junction records are repaired.
- FQDN conflicts stop the import and are never overwritten.
- Deletion is separate and explicit; import never removes stale mappings.

### Command selection for small models

| User intent | Exact command |
| --- | --- |
| Preview Catalyst DNS mappings | `import-catalyst-dns --domain <domain> --pretty` |
| Execute approved import | `import-catalyst-dns --domain <domain> --confirm --pretty` |
| Verify one mapping | `verify-dns --hostname <label> --domain <domain> --ip <ip> --pretty` |
| Create one approved mapping | `create-dns --hostname <label> --domain <domain> --ip <ip> --confirm --pretty` |
| Delete one approved mapping | `delete-dns --hostname <label> --domain <domain> --ip <ip> --confirm --pretty` |
| List modeled mappings | `list-dns --pretty` |

The ServiceNow handler invokes the Catalyst Center handler only for inventory collection. All validation, preflight reads, and CMDB writes remain inside this constrained ServiceNow skill so the workflow is independently auditable.

## Examples

```bash
# List modeled DNS names.
./scripts/dexter servicenow-dns \
  list-dns --pretty

# Preview all Catalyst management mappings for an authorized domain.
./scripts/dexter servicenow-dns \
  import-catalyst-dns --domain example.com --pretty

# Execute the reviewed mapping import.
./scripts/dexter servicenow-dns \
  import-catalyst-dns --domain example.com --confirm --pretty

# Verify a mapping without changing ServiceNow.
./scripts/dexter servicenow-dns \
  verify-dns --hostname sw1 --domain example.com --ip 192.0.2.10 --pretty

# Create the mapping after explicit approval.
./scripts/dexter servicenow-dns \
  create-dns --hostname sw1 --domain example.com --ip 192.0.2.10 --confirm --pretty

# Delete the exact mapping after explicit approval.
./scripts/dexter servicenow-dns \
  delete-dns --hostname sw1 --domain example.com --ip 192.0.2.10 --confirm --pretty
```

## Output Contract

Every command returns JSON with `status`, `results`, and `next_steps`. Import previews include `action_counts` and an `action` on every planned mapping. Creation reports `dns_created`, `ip_created`, and `relationship_created`. An idempotent match returns success with `created: false`. A conflicting FQDN returns an error before any import write.

Interpret outputs literally: `warning` without `--confirm` is a non-mutating plan; `error` means stop; `success` means present the returned counts. Never claim public or internal DNS resolution was configured—the records are CMDB metadata only.
