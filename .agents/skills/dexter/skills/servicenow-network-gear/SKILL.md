---
name: servicenow-network-gear
description: Performs deterministic, guarded CRUD for Cisco Catalyst Center devices modeled as ServiceNow Network Gear CIs and linked Hardware Assets. Use for listing, one-command Catalyst inventory import, updating, verifying, or deleting managed network hardware records.
---

# ServiceNow Network Gear Records

Maintains Cisco network devices as linked records in the ServiceNow development instance:

- Network Gear CI: `cmdb_ci_netgear`
- Hardware Asset: `alm_hardware`
- Manufacturer lookup: `core_company`
- Model category lookup: `cmdb_model_category`

## Safety Boundaries

- Requires the configured ServiceNow URL to match `SERVICENOW_ALLOWED_HOST` exactly.
- Restricts API access to the four tables above.
- Read commands require no confirmation.
- Create, update, and delete commands require explicit user approval and `--confirm`.
- Creation is idempotent by hardware serial number and Catalyst Center device ID.
- A duplicate serial owned by another source is treated as a conflict and is never overwritten.
- Created CIs use a `dexter:catalyst-center:network-gear:` correlation marker.
- Update and delete operations reject CIs not created by this skill.
- Deletion rejects linked assets without this skill's ownership marker.
- Asset creation failure rolls back the newly created CI.
- `import-catalyst` validates the complete source inventory before mutation, sorts devices by hostname, and stops on the first ServiceNow error.
- Re-running an import does not create duplicates: serial number and Catalyst device ID are idempotency keys.
- Re-running repairs a missing linked Hardware Asset for an otherwise exact managed CI.
- Bulk import never deletes records missing from Catalyst Center. Deletion is always a separate explicit operation.
- `update-gear` accepts exactly one mutable field per invocation so plans and results remain unambiguous.
- Credentials must never appear in arguments or responses.

## Commands

Run from the repository root:

```bash
./scripts/dexter servicenow-network-gear <command> [options] --pretty
```

| Command | Required options | Description |
| --- | --- | --- |
| `list-gear` | — | Lists records managed by this skill; optionally filter by `--serial` or `--sys-id`. |
| `get-gear` | `--serial` or `--sys-id` | Gets one Network Gear CI and linked Hardware Assets. |
| `create-gear` | `--hostname`, `--ip`, `--serial`, `--platform`, `--software`, `--catalyst-id`, `--confirm` | Creates a Network Gear CI and linked Hardware Asset. Optional: `--mac`, `--description`. |
| `update-gear` | `--sys-id`, exactly one mutable field, `--confirm` | Updates one of hostname, IP, platform, software, MAC, or description on a managed CI. |
| `delete-gear` | `--sys-id`, `--confirm` | Deletes managed linked assets first and then the Network Gear CI. |
| `import-catalyst` | — for plan; `--confirm` to execute | Retrieves Catalyst inventory itself, validates and sorts it, then idempotently imports every device. |

## Deterministic Catalyst Import Workflow

Agents of every model tier, including Luna and Terra, must follow these exact steps:

1. Run `import-catalyst --pretty` without `--confirm`.
2. If status is `error`, report the validation errors and stop. Do not manually repair, guess, or omit source fields.
3. Present `results.plan` and request explicit approval if approval is not already present in the user's request.
4. After approval, run the same command with `--confirm` exactly once.
5. Report `created_count`, `unchanged_count`, and any stopped error.
6. Run `list-gear --pretty` to verify the final managed inventory.

Do not manually run `list-devices`, copy values, construct shell loops, reorder records, or infer missing fields for a bulk import. The `import-catalyst` command performs those steps deterministically in code.

### Import invariants

- Required Catalyst fields: hostname, management IP, serial number, platform ID, software version, and device ID.
- Optional Catalyst field: MAC address.
- Device order is case-insensitive hostname order.
- Duplicate serial numbers or device IDs block the entire import before ServiceNow mutation.
- Existing exact records are unchanged and counted in `unchanged_count`.
- A serial-number conflict with a record from another source stops the import.
- A rerun after partial failure safely resumes because successfully created records are idempotent.
- A CI whose asset creation previously failed is repaired by creating only the missing managed asset.

## Command Selection for Small Models

| User intent | Exact command |
| --- | --- |
| Preview Catalyst import | `import-catalyst --pretty` |
| Execute approved Catalyst import | `import-catalyst --confirm --pretty` |
| Verify all imported gear | `list-gear --pretty` |
| Find one record | `get-gear --serial <serial> --pretty` |
| Change one managed record | `update-gear --sys-id <sys_id> <field> --confirm --pretty` |
| Delete one managed record | `delete-gear --sys-id <sys_id> --confirm --pretty` |

### Output handling for small models

| Output | Required action |
| --- | --- |
| Dry-run `status: warning` | Present the plan; no mutation occurred. |
| `status: success` | Report counts and verify with `list-gear`. |
| `status: error` | Report the returned error and stop; do not improvise another table or field. |
| `created_count: 0` and `unchanged_count > 0` | State that the rerun was idempotent; do not call creation again. |
| Partial processed count | State where import stopped; a later approved rerun safely resumes. |

Keep inventory collection in the Catalyst Center skill and ServiceNow mutation in this skill so both remain modular and independently auditable.

## Examples

```bash
# List only records managed by this skill.
./scripts/dexter servicenow-network-gear \
  list-gear --pretty

# Deterministically preview the complete Catalyst inventory import.
./scripts/dexter servicenow-network-gear \
  import-catalyst --pretty

# Execute the reviewed import after explicit approval.
./scripts/dexter servicenow-network-gear \
  import-catalyst --confirm --pretty

# Create a CI and hardware asset after approval.
./scripts/dexter servicenow-network-gear \
  create-gear --hostname sw1 --ip 192.0.2.10 --serial DEMO123 \
  --platform C9KV-UADP-8P --software 17.12.1 --mac 52:54:00:00:00:01 \
  --catalyst-id 00000000-0000-4000-8000-000000000001 --confirm --pretty

# Update a managed CI.
./scripts/dexter servicenow-network-gear \
  update-gear --sys-id 0123456789abcdef0123456789abcdef \
  --software 17.12.2 --confirm --pretty

# Delete linked records after approval.
./scripts/dexter servicenow-network-gear \
  delete-gear --sys-id 0123456789abcdef0123456789abcdef --confirm --pretty
```

## Output Contract

Every command returns `status`, `results`, and `next_steps`. `success` and `warning` exit with code 0; `error` exits with code 1. A warning on a mutation means no change occurred because confirmation was absent.
