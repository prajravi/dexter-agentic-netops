---
name: juniper-mist
description: Queries a configured Juniper Mist organization, sites, WLANs, and paginated inventory, and safely creates lab sites or disabled WLANs through mandatory preview, exact confirmation, idempotency, and verification. Use when users ask Dexter about Mist organization data, sites, wireless networks, inventory, or approved lab provisioning.
---

# Juniper Mist

Use the configured Juniper Mist regional API for organization-scoped NetOps workflows. Use only the bundled handler; never construct raw API calls or expose the API token or WLAN PSK.

## Safety boundaries

- Require `MIST_API_HOST` to be HTTPS and exactly match `MIST_ALLOWED_HOST`.
- Restrict every organization endpoint to `MIST_ORG_ID` and resolve site IDs from that organization's site list.
- Permit only the documented `GET` and narrow `POST` endpoints implemented by the handler.
- Prefer separate `MIST_READ_TOKEN` and `MIST_WRITE_TOKEN` values; retain `MIST_API_TOKEN` only as a backward-compatible fallback.
- Never accept the Mist token or WLAN PSK in command arguments or return them in output.
- Run a live plan before every write. Present the complete plan and obtain explicit approval for that exact target.
- Pass both `--confirm` and the exact `confirmation_token` only after approval.
- Never update or delete an existing site or WLAN. Block same-name resources with different settings.
- Create WLANs disabled. Enabling, editing, or deleting a WLAN is not implemented.
- Verify each successful write with a new read before reporting completion.
- Do not automatically retry a write after a timeout, connection failure, or HTTP error; verify state first.
- Stop if pagination repeats a page, the API returns non-object records, the organization ID differs, or the token reports no configured-organization privilege.

## Runtime requirements

Use the regional endpoint, organization, tokens, and optional WLAN PSK configured through the variables documented in `.env.example`. Keep tokens and PSKs secret and use only an authorized organization.

## Commands

Run from the repository root:

```bash
./scripts/dexter mist <command> [options] --pretty
```

| Command | Required options | Purpose |
| --- | --- | --- |
| `show-organization` | — | Show the configured organization and the token's organization privileges. |
| `list-sites` | — | List all sites with bounded pagination. |
| `inventory-summary` | — | List paginated inventory and summarize devices by type, model, site, and connectivity. |
| `list-wlans` | `--site-name` | List sanitized WLAN details for an exact site. |
| `create-site-plan` | `--name`, `--country-code`, `--timezone` | Preview a site; `--address` is optional. |
| `create-site` | Same site options, `--confirm`, `--confirm-target` | Create and verify the exact approved site. |
| `create-wlan-plan` | `--site-name`, `--ssid` | Preview a disabled PSK WLAN; optionally use `--security open`. |
| `create-wlan` | Same WLAN options, `--confirm`, `--confirm-target` | Create and verify the exact approved disabled WLAN. |

## Deterministic workflow

For read requests, run the matching command and summarize the returned JSON. Treat empty sites, WLANs, or inventory as valid results.

For a site creation request:

1. Require an exact site name, two-letter country code, and IANA timezone. Do not guess an exact street address.
2. Run `create-site-plan` without confirmation.
3. If `action=blocked`, report the conflict and stop. Never update the existing site.
4. If `action=unchanged`, report that no write is needed.
5. If `action=create`, show the name, country, timezone, address, management marker, and confirmation token. Blocked and unchanged plans intentionally contain no token.
6. Obtain explicit approval for that exact plan.
7. Run `create-site` once with `--confirm` and the exact token.
8. Report success only when `verified=true` and the complete site configuration matches.

For a Bengaluru lab request, the user may approve this city-level profile:

```text
name: Bengaluru Lab
country_code: IN
timezone: Asia/Kolkata
address: Bengaluru, Karnataka, India
```

For a WLAN creation request:

1. Resolve the exact site by name.
2. Require an SSID and security choice. Use `psk` for an employee WLAN unless the user explicitly requests an open lab WLAN.
3. Read the PSK from `MIST_WLAN_PSK`; never display it.
4. Run `create-wlan-plan` without confirmation.
5. Present the site, SSID, bands, security type, and `enabled=false`; obtain approval.
6. Run `create-wlan` once with the exact token.
7. Report success only when `verified=true`, and emphasize that the WLAN remains disabled.

If a confirmed POST times out, loses its connection, or returns a server error, do not retry it. The handler performs one read-only verification: report `created=unknown` and the verification result, then direct the operator to the Mist audit log.

## Examples

```bash
./scripts/dexter mist show-organization --pretty
./scripts/dexter mist list-sites --pretty
./scripts/dexter mist inventory-summary --pretty

./scripts/dexter mist create-site-plan --name "Bengaluru Lab" \
  --country-code IN --timezone Asia/Kolkata \
  --address "Bengaluru, Karnataka, India" --pretty

./scripts/dexter mist create-wlan-plan --site-name "Bengaluru Lab" \
  --ssid "Dexter-Employee" --security psk --pretty
```

## Output contract

Every command prints JSON containing `status`, `results`, and `next_steps`. `warning` on a create plan means no mutation occurred. `error` means stop. `success` on a write must include `verified: true`; otherwise report that manual verification is required.
