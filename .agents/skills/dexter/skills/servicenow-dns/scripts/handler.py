#!/usr/bin/env python3
"""Safely inspect and create DNS-name CIs in a ServiceNow development instance."""

from __future__ import annotations

import argparse
import importlib.util
import ipaddress
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
SKILLS_ROOT = SCRIPT_DIR.parents[1]
DNS_TABLE = "cmdb_ci_dns_name"
IP_TABLE = "cmdb_ci_ip_address"
RELATION_TABLE = "cmdb_ip_address_dns_name"
DEFAULT_TIMEOUT = 30
DEFAULT_LIMIT = 100
MAX_LIMIT = 100
LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
SYS_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
CATALYST_HANDLER_PATH = SKILLS_ROOT / "catalyst-center" / "scripts" / "handler.py"


def _response(status: str, results: Any, next_steps: list[str]) -> dict[str, Any]:
    """Build the skill's standard response contract."""
    return {"status": status, "results": results, "next_steps": next_steps}


def _load_environment() -> None:
    explicit = os.getenv("DEXTER_ENV_FILE", "").strip()
    if explicit:
        env_path = Path(explicit).expanduser()
        if not env_path.is_file():
            raise ValueError(f"DEXTER_ENV_FILE does not exist: {env_path}.")
        load_dotenv(env_path, override=False)
        return
    for parent in SCRIPT_DIR.parents:
        env_path = parent / ".env"
        if env_path.is_file():
            load_dotenv(env_path, override=False)
            return


def _load_configuration() -> tuple[str, str, str]:
    """Load ServiceNow credentials from the Dexter configuration."""
    _load_environment()
    raw_instance = os.getenv("SERVICENOW_DEV_INSTANCE", "").strip()
    allowed_host = os.getenv("SERVICENOW_ALLOWED_HOST", "").strip().lower()
    username = os.getenv("SERVICENOW_DEV_USERNAME", "").strip()
    password = os.getenv("SERVICENOW_DEV_PASSWORD", "")
    parsed = urlparse(raw_instance)
    allowed = urlparse(f"//{allowed_host}")
    if not allowed_host or allowed.hostname != allowed_host or allowed.port is not None:
        raise ValueError("SERVICENOW_ALLOWED_HOST must be one hostname without a scheme or port.")
    if parsed.scheme != "https" or parsed.hostname != allowed_host or parsed.path not in {"", "/"}:
        raise ValueError(
            "SERVICENOW_DEV_INSTANCE must be HTTPS and match SERVICENOW_ALLOWED_HOST."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("SERVICENOW_DEV_INSTANCE must not contain credentials or query parameters.")
    if not username or not password:
        raise ValueError(
            "SERVICENOW_DEV_USERNAME and SERVICENOW_DEV_PASSWORD must be set in "
            "the Dexter root .env file."
        )
    return f"https://{allowed_host}", username, password


def _validate_hostname(hostname: str) -> str:
    """Validate and normalize a single DNS host label."""
    normalized = (hostname or "").strip().rstrip(".").lower()
    if "." in normalized:
        raise ValueError("--hostname must be a single host label, not an FQDN.")
    if not LABEL_RE.fullmatch(normalized):
        raise ValueError("--hostname must be a valid DNS host label.")
    return normalized


def _validate_domain(domain: str) -> str:
    """Validate and normalize a DNS suffix."""
    normalized = (domain or "").strip().rstrip(".").lower()
    labels = normalized.split(".")
    if len(labels) < 2 or len(normalized) > 253 or any(not LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError("--domain must be a valid DNS domain such as example.com.")
    return normalized


def _validate_ip(ip: str) -> str:
    """Validate and normalize an IPv4 or IPv6 address."""
    try:
        return str(ipaddress.ip_address((ip or "").strip()))
    except ValueError as exc:
        raise ValueError("--ip must be a valid IPv4 or IPv6 address.") from exc


def _fqdn(hostname: str, domain: str) -> str:
    """Build a normalized FQDN from validated components."""
    return f"{_validate_hostname(hostname)}.{_validate_domain(domain)}"


def _request(
    method: str,
    path: str,
    params: Optional[dict[str, Any]] = None,
    payload: Optional[dict[str, Any]] = None,
) -> Any:
    """Perform a request restricted to DNS, IP, and their relationship tables."""
    allowed_base_paths = {
        f"/api/now/table/{DNS_TABLE}",
        f"/api/now/table/{IP_TABLE}",
        f"/api/now/table/{RELATION_TABLE}",
    }
    base_path, separator, sys_id = path.rpartition("/")
    is_collection = path in allowed_base_paths
    is_record = base_path in allowed_base_paths and bool(separator) and bool(SYS_ID_RE.fullmatch(sys_id))
    if not (is_collection or is_record):
        raise ValueError("This skill can access only ServiceNow DNS/IP CMDB tables.")
    if method not in {"GET", "POST", "DELETE"}:
        raise ValueError("This skill permits only GET, POST, and DELETE requests.")
    if method == "POST" and not is_collection:
        raise ValueError("POST is permitted only on an allowed table collection.")
    if method == "DELETE" and not is_record:
        raise ValueError("DELETE requires an allowed table and a valid record sys_id.")
    instance, username, password = _load_configuration()
    response = requests.request(
        method,
        f"{instance}{path}",
        params=params,
        json=payload,
        auth=(username, password),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def _find_records(fqdn: Optional[str] = None, ip: Optional[str] = None, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """Find DNS-name CIs by validated FQDN and/or IP address."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"--limit must be between 1 and {MAX_LIMIT}.")
    query_parts = []
    if fqdn:
        labels = fqdn.rstrip(".").split(".")
        normalized_fqdn = _fqdn(labels[0], ".".join(labels[1:]))
        query_parts.append(f"fqdn={normalized_fqdn}")
    if ip:
        query_parts.append(f"ip_address={_validate_ip(ip)}")
    params: dict[str, Any] = {
        "sysparm_fields": "sys_id,name,fqdn,ip_address,sys_created_on,sys_updated_on",
        "sysparm_limit": limit,
        "sysparm_exclude_reference_link": "true",
        "sysparm_display_value": "false",
        "sysparm_query": "^".join(query_parts + ["ORDERBYfqdn"]),
    }
    payload = _request("GET", f"/api/now/table/{DNS_TABLE}", params=params)
    return payload.get("result", []) if isinstance(payload, dict) else []


def _find_ip_records(ip: str) -> list[dict[str, Any]]:
    """Find IP Address CIs by exact normalized address."""
    normalized_ip = _validate_ip(ip)
    payload = _request(
        "GET",
        f"/api/now/table/{IP_TABLE}",
        params={
            "sysparm_query": f"ip_address={normalized_ip}",
            "sysparm_fields": "sys_id,name,ip_address,sys_created_on,sys_updated_on",
            "sysparm_limit": MAX_LIMIT,
            "sysparm_exclude_reference_link": "true",
        },
    )
    return payload.get("result", []) if isinstance(payload, dict) else []


def _find_relationships(dns_sys_id: str, ip_sys_id: str) -> list[dict[str, Any]]:
    """Find an exact DNS Name to IP Address relationship."""
    payload = _request(
        "GET",
        f"/api/now/table/{RELATION_TABLE}",
        params={
            "sysparm_query": f"dns_name={dns_sys_id}^ip_address={ip_sys_id}",
            "sysparm_fields": "sys_id,dns_name,ip_address,sys_created_on,sys_updated_on",
            "sysparm_limit": MAX_LIMIT,
            "sysparm_exclude_reference_link": "true",
        },
    )
    return payload.get("result", []) if isinstance(payload, dict) else []


def _create_ip_record(ip: str) -> dict[str, Any]:
    """Create an IP Address CI used by the DNS related list."""
    normalized_ip = _validate_ip(ip)
    payload = _request(
        "POST",
        f"/api/now/table/{IP_TABLE}",
        params={
            "sysparm_fields": "sys_id,name,ip_address,sys_created_on,sys_updated_on",
            "sysparm_exclude_reference_link": "true",
        },
        payload={
            "name": normalized_ip,
            "ip_address": normalized_ip,
            "short_description": "IP address imported from Cisco Catalyst Center inventory",
            "discovery_source": "Other Automated",
            "correlation_id": f"dexter:catalyst-center:ip:{normalized_ip}",
        },
    )
    return payload.get("result", {}) if isinstance(payload, dict) else {}


def _create_relationship(dns_sys_id: str, ip_sys_id: str) -> dict[str, Any]:
    """Create the junction record displayed in the DNS Name IP Address tab."""
    payload = _request(
        "POST",
        f"/api/now/table/{RELATION_TABLE}",
        params={
            "sysparm_fields": "sys_id,dns_name,ip_address,sys_created_on,sys_updated_on",
            "sysparm_exclude_reference_link": "true",
        },
        payload={"dns_name": dns_sys_id, "ip_address": ip_sys_id},
    )
    return payload.get("result", {}) if isinstance(payload, dict) else {}


def _delete_record(table: str, sys_id: str) -> None:
    """Delete one validated record from an allowed DNS/IP CMDB table."""
    if table not in {DNS_TABLE, IP_TABLE, RELATION_TABLE}:
        raise ValueError("Deletion is restricted to ServiceNow DNS/IP CMDB tables.")
    if not SYS_ID_RE.fullmatch(sys_id or ""):
        raise ValueError("Cannot delete a record without a valid sys_id.")
    _request("DELETE", f"/api/now/table/{table}/{sys_id.lower()}")


def _normalize_catalyst_inventory(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize current and legacy Catalyst handler inventory responses."""
    inventory = result.get("results", [])
    devices = inventory if isinstance(inventory, list) else inventory.get("devices")
    if result.get("status") != "success" or not isinstance(devices, list):
        details = "; ".join(str(item) for item in result.get("next_steps", []))
        suffix = f": {details}" if details else ""
        raise ValueError(f"Catalyst Center inventory retrieval failed{suffix}")
    return [
        {
            "hostname": device.get("hostname"),
            "managementIpAddress": device.get("managementIpAddress") or device.get("management_ip"),
            "id": device.get("id") or device.get("device_id"),
        }
        for device in devices
    ]


def _catalyst_inventory() -> list[dict[str, Any]]:
    """Retrieve devices through the existing Catalyst Center skill handler."""
    if not CATALYST_HANDLER_PATH.is_file():
        raise ValueError("The Catalyst Center skill handler was not found in this workspace.")
    spec = importlib.util.spec_from_file_location("dexter_catalyst_dns_handler", CATALYST_HANDLER_PATH)
    if spec is None or spec.loader is None:
        raise ValueError("The Catalyst Center skill handler could not be loaded.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.handle_command("list-devices")
    return _normalize_catalyst_inventory(result)


def _dns_inventory_plan(domain: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Build a validated, stable DNS mapping plan from Catalyst inventory."""
    normalized_domain = _validate_domain(domain)
    plan: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    seen_fqdns: set[str] = set()
    seen_ips: set[str] = set()
    devices = _catalyst_inventory()
    if not devices:
        return [], [{"device": "(inventory)", "error": "Catalyst Center returned no devices."}]
    for device in sorted(devices, key=lambda item: str(item.get("hostname") or "").casefold()):
        identity = str(device.get("hostname") or device.get("id") or "(unknown)")
        try:
            raw_hostname = str(device.get("hostname") or "").strip().rstrip(".")
            if not raw_hostname:
                raise ValueError("Catalyst device is missing hostname.")
            hostname = _validate_hostname(raw_hostname.split(".")[0])
            ip = _validate_ip(str(device.get("managementIpAddress") or ""))
            fqdn = _fqdn(hostname, normalized_domain)
            if fqdn in seen_fqdns:
                raise ValueError(f"Duplicate generated FQDN: {fqdn}.")
            if ip in seen_ips:
                raise ValueError(f"Duplicate Catalyst management IP: {ip}.")
            seen_fqdns.add(fqdn)
            seen_ips.add(ip)
            plan.append({"hostname": hostname, "domain": normalized_domain, "fqdn": fqdn, "ip": ip})
        except ValueError as exc:
            errors.append({"device": identity, "error": str(exc)})
    return plan, errors


def _preflight_dns_plan(
    plan: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Classify every mapping and find conflicts before any import write."""
    checked: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for item in plan:
        dns_records = _find_records(fqdn=item["fqdn"])
        exact_dns = [
            record
            for record in dns_records
            if str(record.get("ip_address", "")) == item["ip"]
        ]
        conflicting_dns = [record for record in dns_records if record not in exact_dns]
        if conflicting_dns:
            conflicts.append(
                {
                    "fqdn": item["fqdn"],
                    "requested_ip": item["ip"],
                    "existing_records": conflicting_dns,
                }
            )
            checked.append({**item, "action": "blocked"})
            continue

        ip_records = _find_ip_records(item["ip"])
        relationships = []
        if exact_dns and ip_records:
            relationships = _find_relationships(
                exact_dns[0].get("sys_id", ""), ip_records[0].get("sys_id", "")
            )
        if exact_dns and ip_records and relationships:
            action = "unchanged"
        elif exact_dns or ip_records:
            action = "repair"
        else:
            action = "create"
        checked.append({**item, "action": action})
    return checked, conflicts


def list_dns(**kwargs: Any) -> dict[str, Any]:
    """List DNS-name CIs with optional FQDN or IP filters."""
    records = _find_records(kwargs.get("fqdn"), kwargs.get("ip"), kwargs.get("limit", DEFAULT_LIMIT))
    return _response(
        "success",
        {"table": DNS_TABLE, "count": len(records), "records": records},
        ["These are CMDB records; they do not prove authoritative DNS resolution."],
    )


def verify_dns(**kwargs: Any) -> dict[str, Any]:
    """Verify the DNS CI, IP CI, and exact relationship all exist."""
    fqdn = _fqdn(kwargs.get("hostname"), kwargs.get("domain"))
    ip = _validate_ip(kwargs.get("ip"))
    records = _find_records(fqdn=fqdn, ip=ip)
    exact_dns = [
        record for record in records
        if str(record.get("fqdn", "")).lower().rstrip(".") == fqdn
        and str(record.get("ip_address", "")) == ip
    ]
    ip_records = _find_ip_records(ip)
    relationships = []
    if exact_dns and ip_records:
        relationships = _find_relationships(exact_dns[0].get("sys_id", ""), ip_records[0].get("sys_id", ""))
    complete = bool(exact_dns and ip_records and relationships)
    status = "success" if complete else "warning"
    return _response(
        status,
        {
            "fqdn": fqdn,
            "ip_address": ip,
            "exists": complete,
            "dns_records": exact_dns,
            "ip_records": ip_records,
            "relationships": relationships,
        },
        [
            "The DNS Name CI, IP Address CI, and their relationship exist in ServiceNow."
            if complete
            else "Use create-dns with --confirm to create any missing CI relationship records."
        ],
    )


def create_dns(**kwargs: Any) -> dict[str, Any]:
    """Idempotently create one DNS-name CI after explicit confirmation."""
    fqdn = _fqdn(kwargs.get("hostname"), kwargs.get("domain"))
    ip = _validate_ip(kwargs.get("ip"))
    if not kwargs.get("confirm"):
        return _response(
            "warning",
            {"fqdn": fqdn, "ip_address": ip, "created": False},
            ["Creation requires explicit user approval and the --confirm flag."],
        )

    existing = _find_records(fqdn=fqdn)
    exact = [record for record in existing if str(record.get("ip_address", "")) == ip]
    if existing and not exact:
        return _response(
            "error",
            {"fqdn": fqdn, "requested_ip": ip, "existing_records": existing},
            ["The FQDN already exists with a different IP; resolve the conflict manually."],
        )

    dns_created = not exact
    if exact:
        dns_record = exact[0]
    else:
        response = _request(
            "POST",
            f"/api/now/table/{DNS_TABLE}",
            params={
                "sysparm_fields": "sys_id,name,fqdn,ip_address,sys_created_on,sys_updated_on",
                "sysparm_exclude_reference_link": "true",
            },
            payload={
                "name": fqdn,
                "fqdn": fqdn,
                "ip_address": ip,
                "short_description": "DNS name imported from Cisco Catalyst Center inventory",
                "discovery_source": "Other Automated",
                "correlation_id": f"dexter:catalyst-center:{fqdn}",
            },
        )
        dns_record = response.get("result", {}) if isinstance(response, dict) else {}

    ip_records = _find_ip_records(ip)
    ip_created = not ip_records
    ip_record = _create_ip_record(ip) if ip_created else ip_records[0]
    relationships = _find_relationships(dns_record.get("sys_id", ""), ip_record.get("sys_id", ""))
    relationship_created = not relationships
    relationship = (
        _create_relationship(dns_record.get("sys_id", ""), ip_record.get("sys_id", ""))
        if relationship_created
        else relationships[0]
    )
    return _response(
        "success",
        {
            "fqdn": fqdn,
            "ip_address": ip,
            "created": dns_created or ip_created or relationship_created,
            "dns_created": dns_created,
            "ip_created": ip_created,
            "relationship_created": relationship_created,
            "dns_record": dns_record,
            "ip_record": ip_record,
            "relationship": relationship,
        },
        ["Run verify-dns to confirm all three CMDB records."],
    )


def delete_dns(**kwargs: Any) -> dict[str, Any]:
    """Delete one exact mapping and its now-unreferenced DNS/IP CIs."""
    fqdn = _fqdn(kwargs.get("hostname"), kwargs.get("domain"))
    ip = _validate_ip(kwargs.get("ip"))
    if not kwargs.get("confirm"):
        return _response(
            "warning",
            {"fqdn": fqdn, "ip_address": ip, "deleted": False},
            ["Deletion requires explicit user approval and the --confirm flag."],
        )

    dns_records = [
        record for record in _find_records(fqdn=fqdn, ip=ip)
        if str(record.get("fqdn", "")).lower().rstrip(".") == fqdn
        and str(record.get("ip_address", "")) == ip
    ]
    ip_records = _find_ip_records(ip)
    if not dns_records:
        return _response(
            "success",
            {"fqdn": fqdn, "ip_address": ip, "deleted": False},
            ["No exact DNS mapping exists; no change was made."],
        )

    dns_record = dns_records[0]
    ip_record = ip_records[0] if ip_records else {}
    relationships = (
        _find_relationships(dns_record.get("sys_id", ""), ip_record.get("sys_id", ""))
        if ip_record
        else []
    )
    for relationship in relationships:
        _delete_record(RELATION_TABLE, relationship.get("sys_id", ""))
    _delete_record(DNS_TABLE, dns_record.get("sys_id", ""))

    ip_deleted = False
    remaining_dns_names: list[dict[str, Any]] = []
    if ip_record:
        remaining = _request(
            "GET",
            f"/api/now/table/{RELATION_TABLE}",
            params={
                "sysparm_query": f"ip_address={ip_record.get('sys_id', '')}",
                "sysparm_fields": "sys_id",
                "sysparm_limit": 1,
                "sysparm_exclude_reference_link": "true",
            },
        )
        remaining_records = remaining.get("result", []) if isinstance(remaining, dict) else []
        if not remaining_records:
            _delete_record(IP_TABLE, ip_record.get("sys_id", ""))
            ip_deleted = True
        else:
            remaining_dns_names = _find_records(ip=ip)

    return _response(
        "success",
        {
            "fqdn": fqdn,
            "ip_address": ip,
            "deleted": True,
            "relationships_deleted": len(relationships),
            "dns_deleted": True,
            "ip_deleted": ip_deleted,
            "remaining_dns_count": len(remaining_dns_names),
            "remaining_dns_names": remaining_dns_names,
        },
        [
            "Run verify-dns to confirm the mapping no longer exists."
            if ip_deleted
            else f"The IP CI was retained because {len(remaining_dns_names)} DNS mapping(s) still use it."
        ],
    )


def import_catalyst_dns(**kwargs: Any) -> dict[str, Any]:
    """Plan or idempotently import Catalyst management DNS mappings."""
    domain = _validate_domain(kwargs.get("domain"))
    plan, errors = _dns_inventory_plan(domain)
    if errors:
        return _response(
            "error",
            {"valid_count": len(plan), "errors": errors, "plan": plan, "created": False},
            ["Correct Catalyst inventory errors before creating any ServiceNow DNS mappings."],
        )
    checked_plan, conflicts = _preflight_dns_plan(plan)
    action_counts = {
        action: sum(1 for item in checked_plan if item["action"] == action)
        for action in ("create", "repair", "unchanged", "blocked")
    }
    if conflicts:
        return _response(
            "error",
            {
                "planned_count": len(checked_plan),
                "created": False,
                "action_counts": action_counts,
                "conflicts": conflicts,
                "plan": checked_plan,
            },
            ["Resolve every FQDN conflict before retrying; no records were changed."],
        )
    if not kwargs.get("confirm"):
        next_step = (
            "All mappings are complete; no confirmed import is required."
            if action_counts["unchanged"] == len(checked_plan)
            else "Review the deterministic plan, then rerun import-catalyst-dns with --confirm."
        )
        return _response(
            "warning",
            {
                "planned_count": len(checked_plan),
                "created": False,
                "action_counts": action_counts,
                "plan": checked_plan,
            },
            [next_step],
        )
    outcomes = []
    for item in checked_plan:
        outcome = create_dns(
            hostname=item["hostname"], domain=item["domain"], ip=item["ip"], confirm=True
        )
        outcomes.append({"fqdn": item["fqdn"], **outcome})
        if outcome.get("status") == "error":
            return _response(
                "error",
                {"planned_count": len(checked_plan), "processed_count": len(outcomes), "outcomes": outcomes},
                ["Import stopped on the first conflict; rerunning is safe and idempotent."],
            )
    created_count = sum(1 for item in outcomes if item.get("results", {}).get("created"))
    return _response(
        "success",
        {
            "planned_count": len(checked_plan),
            "processed_count": len(outcomes),
            "created_count": created_count,
            "unchanged_count": len(outcomes) - created_count,
            "outcomes": outcomes,
        },
        ["These are ServiceNow CMDB mappings, not authoritative A or PTR records."],
    )


def handle_command(command: str, **kwargs: Any) -> dict[str, Any]:
    """Normalize and dispatch a constrained ServiceNow DNS command."""
    normalized = (command or "").strip().lower().replace("_", "-")
    handlers = {
        "list-dns": list_dns,
        "verify-dns": verify_dns,
        "create-dns": create_dns,
        "delete-dns": delete_dns,
        "import-catalyst-dns": import_catalyst_dns,
    }
    handler = handlers.get(normalized)
    if handler is None:
        return _response("error", [], [f"Supported commands: {', '.join(handlers)}."])
    try:
        return handler(**kwargs)
    except ValueError as exc:
        return _response("error", [], [str(exc)])
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        return _response("error", [], [f"ServiceNow development API returned HTTP {status_code}."])
    except requests.RequestException as exc:
        return _response("error", [], [f"ServiceNow development API request failed: {type(exc).__name__}."])


def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Manage DNS-name CIs in ServiceNow development")
    parser.add_argument("command", choices=["list-dns", "verify-dns", "create-dns", "delete-dns", "import-catalyst-dns"])
    parser.add_argument("--hostname", help="Single DNS host label")
    parser.add_argument("--domain", help="DNS suffix owned or authorized by the user")
    parser.add_argument("--fqdn", help="Exact FQDN filter for list-dns")
    parser.add_argument("--ip", help="IPv4 or IPv6 address")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--confirm", action="store_true", help="Confirm creation of a DNS-name CI")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    options = vars(args)
    command = options.pop("command")
    pretty = options.pop("pretty")
    result = handle_command(command, **options)
    print(json.dumps(result, indent=2 if pretty else None, sort_keys=False))
    return 0 if result.get("status") in {"success", "warning"} else 1


if __name__ == "__main__":
    sys.exit(_cli())
