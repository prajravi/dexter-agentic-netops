---
name: catalyst-center
description: "Dexter queries Cisco Catalyst Center for inventory, device details, interfaces, sites, assurance health, issues, clients, templates, physical topology, CDP neighbors, and read-only show commands. Use when inspecting or investigating Catalyst Center network state."
---

# Cisco Catalyst Center

Dexter invokes this workflow using the Catalyst Center configured in the repository `.env` file or process environment. The workflow is customer-neutral and contains no organization-specific clusters, domains, accounts, incident processes, or topology assumptions.

## Dexter Workflow

When a user asks Dexter a Catalyst Center question, Dexter should:

1. Identify the matching read-only command from the command reference below.
2. Run `./scripts/dexter catalyst-center` with the user's supplied command, filters, and `--pretty`.
3. Summarize the returned JSON in a concise table or list, clearly noting warnings or missing data.
4. Use `list-neighbors` for the fast topology-derived view or `cdp-neighbors` for direct structured CDP output.
5. Use `command-runner` only for read-only `show` commands; the handler rejects every other command.
6. For a port bounce, run `port-bounce-plan`, show the resolved target and impact, obtain explicit approval, and pass the returned target token to `port-bounce`.

Dexter must not alter Catalyst Center configuration except through the guarded `port-bounce` workflow. If another request requires an unimplemented change, Dexter should explain the missing capability instead of making an unsupported API call.

## Trigger Phrases

- "Check a device in Catalyst Center"
- "List Catalyst Center devices"
- "Show interfaces, sites, issues, clients, or topology"
- "Get Catalyst Center device health"
- "Dexter, show the Catalyst Center inventory"
- "Dexter, show neighbors for access-switch-01"
- "Dexter, run show version on access-switch-01"
- "Dexter, show health for all devices at this site"
- "Dexter, bounce an access port on access-switch-01"

## Safety Boundaries

- All commands are read-only except `port-bounce`.
- The handler accepts no controller override.
- Use the handler rather than raw REST calls so requests remain consistent and auditable.
- `port-bounce` requires explicit user confirmation bound to the exact device and interface returned by `port-bounce-plan`.
- `port-bounce` rejects virtual, routed, and trunk interfaces; only physical access ports are permitted.
- If the restore-to-UP task fails, Dexter must report that manual intervention is immediately required.

## Runtime requirements

Use the Catalyst Center connection configured through the variables documented in `.env.example`.

## Commands

Run commands from the repository root:

```bash
./scripts/dexter catalyst-center <command> [options] --pretty
```

| Command | Required options | Description |
| --- | --- | --- |
| `list-devices` | — | Lists inventory; optional `--hostname`, `--ip`, or `--reachability` filters. |
| `get-device` | `--hostname` or `--device-id` | Returns a single device inventory record. |
| `device-detail` | `--hostname` | Returns rich device, role, location, and assurance details. |
| `redundancy-info` | `--hostname` or `--device-id` | Returns redundancy information for supported controllers. |
| `get-interfaces` | `--hostname` or `--device-id` | Lists interfaces on a device. |
| `get-interface` | `--interface` and `--hostname` or `--device-id` | Returns one interface. |
| `list-neighbors` | `--hostname` or `--device-id` | Returns physical-topology neighbors in a show-CDP-like format. It is not direct device CLI/CDP-table output. |
| `list-sites` | — | Lists sites; optional `--name` substring filter. |
| `list-issues` | — | Lists assurance issues; optional `--device-id`, `--site-id`, or `--priority`. |
| `get-issue` | `--issue-id` | Returns issue details and suggested actions. |
| `device-health` | `--hostname` | Returns assurance health for one device. |
| `site-device-health` | `--hostname` | Returns health for all devices at the same leaf site. |
| `client-detail` | `--mac` | Returns a client detail record; optional `--timestamp`. |
| `client-health` | — | Returns the overall client-health response; optional `--timestamp`. |
| `physical-topology` | — | Returns the physical topology graph. |
| `cdp-neighbors` | `--hostname` or `--device-id` | Runs `show cdp neighbors detail` and returns structured neighbors. |
| `command-runner` | `--hostname` or `--device-id`, `--commands` | Runs comma-separated read-only `show` commands. |
| `get-templates` | `--template-ids` | Returns details for comma-separated template UUIDs. |
| `port-bounce-plan` | `--interface`, `--hostname` or `--device-id` | Validates the target and returns the exact confirmation token required for execution. |
| `port-bounce` | `--interface`, `--hostname` or `--device-id`, `--confirm-target` | Administratively cycles the planned physical access port down and up; optional `--bounce-delay` is 0–60 seconds. |

## Examples

```bash
# Find an inventory device.
./scripts/dexter catalyst-center list-devices --hostname access-switch-01 --pretty

# Inspect a device's interfaces.
./scripts/dexter catalyst-center get-interfaces --hostname access-switch-01 --pretty

# Get physical-topology neighbors for a switch (show-CDP-like, not direct CLI output).
./scripts/dexter catalyst-center list-neighbors --hostname access-switch-01 --pretty

# Get structured neighbors from direct CDP command output.
./scripts/dexter catalyst-center cdp-neighbors --hostname access-switch-01 --pretty

# Run a read-only show command through Catalyst Center.
./scripts/dexter catalyst-center command-runner --hostname access-switch-01 --commands "show version" --pretty

# Obtain health data for a device.
./scripts/dexter catalyst-center device-health --hostname access-switch-01 --pretty
```

## Port Bounce Workflow

Port bounce is disruptive and must never be inferred from a diagnostic request.

1. Resolve and validate the exact target:

```bash
./scripts/dexter catalyst-center port-bounce-plan --hostname access-switch-01 \
  --interface GigabitEthernet1/0/5 --pretty
```

2. Present the resolved interface, mode, current state, description, and expected impact. Obtain explicit approval for that exact target.
3. Use the returned `confirmation_token` without modification:

```bash
./scripts/dexter catalyst-center port-bounce --hostname access-switch-01 \
  --interface GigabitEthernet1/0/5 --bounce-delay 5 \
  --confirm-target "<device-id>:GigabitEthernet1/0/5" --pretty
```

4. Report both Catalyst Center task IDs and the final state.
5. If the UP phase fails, prominently report that the interface may remain disabled and requires immediate manual recovery.

## Output Contract

Every command prints JSON with `status`, `results`, and `next_steps`. A `success` response has exit code `0`; `warning` has exit code `0`; and `error` has exit code `1`.

## Troubleshooting

- An authentication or connection error usually means the controller is unavailable or the Dexter environment is incomplete.
- A no-results response means the requested resource was not returned by the configured controller.
- Inventory and assurance data can change as the network is discovered and monitored.
- Command Runner availability depends on device support and Catalyst Center permissions.
- `command-runner` accepts only commands beginning with `show `; configuration commands are rejected.
- Port bounce requires an interface UUID from Catalyst Center and permission to update interface administrative state.
