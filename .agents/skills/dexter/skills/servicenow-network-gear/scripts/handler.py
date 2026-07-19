#!/usr/bin/env python3
"""Guarded CRUD for Catalyst Center network gear in ServiceNow development."""

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
CI_TABLE = "cmdb_ci_netgear"
ASSET_TABLE = "alm_hardware"
COMPANY_TABLE = "core_company"
CATEGORY_TABLE = "cmdb_model_category"
MANAGED_PREFIX = "dexter:catalyst-center:network-gear:"
ASSET_MARKER = "Managed by Dexter from Cisco Catalyst Center inventory"
CATALYST_HANDLER_PATH = SKILLS_ROOT / "catalyst-center" / "scripts" / "handler.py"
DEFAULT_TIMEOUT = 30
MAX_LIMIT = 100
SYS_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,253}[A-Za-z0-9])?$")
MAC_RE = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def _response(status: str, results: Any, next_steps: list[str]) -> dict[str, Any]:
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
    _load_environment()
    parsed = urlparse(os.getenv("SERVICENOW_DEV_INSTANCE", "").strip())
    allowed_host = os.getenv("SERVICENOW_ALLOWED_HOST", "").strip().lower()
    username = os.getenv("SERVICENOW_DEV_USERNAME", "").strip()
    password = os.getenv("SERVICENOW_DEV_PASSWORD", "")
    allowed = urlparse(f"//{allowed_host}")
    if not allowed_host or allowed.hostname != allowed_host or allowed.port is not None:
        raise ValueError("SERVICENOW_ALLOWED_HOST must be one hostname without a scheme or port.")
    if parsed.scheme != "https" or parsed.hostname != allowed_host or parsed.path not in {"", "/"}:
        raise ValueError("SERVICENOW_DEV_INSTANCE must be HTTPS and match SERVICENOW_ALLOWED_HOST.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("SERVICENOW_DEV_INSTANCE must not include credentials or query parameters.")
    if not username or not password:
        raise ValueError("ServiceNow development credentials must be set in the Dexter root .env file.")
    return f"https://{allowed_host}", username, password


def _validate_text(value: str, label: str, maximum: int = 255) -> str:
    normalized = (value or "").strip()
    if not normalized or len(normalized) > maximum or any(char in normalized for char in "\r\n\x00"):
        raise ValueError(f"--{label} must be between 1 and {maximum} characters without controls.")
    return normalized


def _validate_hostname(value: str) -> str:
    hostname = _validate_text(value, "hostname")
    if not HOSTNAME_RE.fullmatch(hostname):
        raise ValueError("--hostname must be a valid device hostname.")
    return hostname.lower()


def _validate_serial(value: str) -> str:
    serial = _validate_text(value, "serial")
    if not SERIAL_RE.fullmatch(serial):
        raise ValueError("--serial contains unsupported characters.")
    return serial


def _validate_ip(value: str) -> str:
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError as exc:
        raise ValueError("--ip must be a valid IPv4 or IPv6 address.") from exc


def _validate_mac(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = value.strip().lower().replace("-", ":")
    if not MAC_RE.fullmatch(normalized):
        raise ValueError("--mac must contain six colon-separated octets.")
    return normalized


def _record_path(table: str, sys_id: Optional[str] = None) -> str:
    if table not in {CI_TABLE, ASSET_TABLE, COMPANY_TABLE, CATEGORY_TABLE}:
        raise ValueError("This skill is restricted to approved hardware-support tables.")
    if sys_id is None:
        return f"/api/now/table/{table}"
    if not SYS_ID_RE.fullmatch(sys_id):
        raise ValueError("A valid 32-character sys_id is required.")
    return f"/api/now/table/{table}/{sys_id.lower()}"


def _request(
    method: str,
    table: str,
    sys_id: Optional[str] = None,
    params: Optional[dict[str, Any]] = None,
    payload: Optional[dict[str, Any]] = None,
) -> Any:
    if method not in {"GET", "POST", "PATCH", "DELETE"}:
        raise ValueError("Only GET, POST, PATCH, and DELETE are supported.")
    if method in {"POST"} and sys_id is not None:
        raise ValueError("POST is permitted only on a table collection.")
    if method in {"PATCH", "DELETE"} and sys_id is None:
        raise ValueError(f"{method} requires a record sys_id.")
    instance, username, password = _load_configuration()
    response = requests.request(
        method,
        f"{instance}{_record_path(table, sys_id)}",
        params=params,
        json=payload,
        auth=(username, password),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def _query(table: str, query: str, fields: str, limit: int = MAX_LIMIT, display: bool = False) -> list[dict[str, Any]]:
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"--limit must be between 1 and {MAX_LIMIT}.")
    payload = _request(
        "GET",
        table,
        params={
            "sysparm_query": query,
            "sysparm_fields": fields,
            "sysparm_limit": limit,
            "sysparm_exclude_reference_link": "true",
            "sysparm_display_value": "true" if display else "false",
        },
    )
    return payload.get("result", []) if isinstance(payload, dict) else []


def _find_ci(serial: Optional[str] = None, sys_id: Optional[str] = None) -> list[dict[str, Any]]:
    fields = "sys_id,name,ip_address,serial_number,model_number,manufacturer,mac_address,firmware_version,short_description,discovery_source,correlation_id,asset,install_status,operational_status,sys_created_on,sys_updated_on"
    if sys_id:
        payload = _request("GET", CI_TABLE, sys_id, params={"sysparm_fields": fields, "sysparm_exclude_reference_link": "true"})
        record = payload.get("result", {}) if isinstance(payload, dict) else {}
        return [record] if record else []
    query = f"serial_number={_validate_serial(serial or '')}" if serial else f"correlation_idSTARTSWITH{MANAGED_PREFIX}"
    return _query(CI_TABLE, f"{query}^ORDERBYname", fields)


def _find_assets(ci_sys_id: str) -> list[dict[str, Any]]:
    return _query(
        ASSET_TABLE,
        f"ci={ci_sys_id}",
        "sys_id,display_name,asset_tag,serial_number,manufacturer,model_category,install_status,substatus,ci,comments,sys_created_on,sys_updated_on",
    )


def _reference_sys_id(table: str, query: str, label: str) -> str:
    records = _query(table, query, "sys_id,name", limit=2)
    if len(records) != 1:
        raise ValueError(f"Expected exactly one ServiceNow {label} reference; found {len(records)}.")
    return str(records[0]["sys_id"])


def _assert_managed(ci: dict[str, Any]) -> None:
    if not str(ci.get("correlation_id", "")).startswith(MANAGED_PREFIX):
        raise ValueError("Updates and deletes are allowed only for records created by this skill.")


def _catalyst_inventory() -> list[dict[str, Any]]:
    """Load inventory through the existing Catalyst Center skill handler."""
    if not CATALYST_HANDLER_PATH.is_file():
        raise ValueError("The Catalyst Center skill handler was not found in this workspace.")
    spec = importlib.util.spec_from_file_location("dexter_catalyst_center_handler", CATALYST_HANDLER_PATH)
    if spec is None or spec.loader is None:
        raise ValueError("The Catalyst Center skill handler could not be loaded.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.handle_command("list-devices")
    inventory = result.get("results", {})
    devices = inventory.get("devices") if isinstance(inventory, dict) else None
    if result.get("status") != "success" or not isinstance(devices, list):
        raise ValueError("Catalyst Center inventory retrieval failed.")
    return [
        {
            "hostname": device.get("hostname"),
            "managementIpAddress": device.get("management_ip"),
            "serialNumber": device.get("serial_number"),
            "platformId": device.get("platform"),
            "softwareVersion": device.get("software_version"),
            "macAddress": device.get("mac_address"),
            "id": device.get("device_id"),
            "type": device.get("role"),
        }
        for device in devices
    ]


def _inventory_payload(device: dict[str, Any]) -> dict[str, str]:
    """Convert one Catalyst inventory record into validated create-gear inputs."""
    missing = [
        field for field in (
            "hostname", "managementIpAddress", "serialNumber", "platformId",
            "softwareVersion", "id",
        )
        if not device.get(field)
    ]
    if missing:
        raise ValueError(
            f"Catalyst device {device.get('hostname') or device.get('id') or '(unknown)'} "
            f"is missing required fields: {', '.join(missing)}."
        )
    return {
        "hostname": _validate_hostname(str(device["hostname"])),
        "ip": _validate_ip(str(device["managementIpAddress"])),
        "serial": _validate_serial(str(device["serialNumber"])),
        "platform": _validate_text(str(device["platformId"]), "platform"),
        "software": _validate_text(str(device["softwareVersion"]), "software", 40),
        "mac": _validate_mac(str(device.get("macAddress") or "")) or "",
        "catalyst_id": _validate_text(str(device["id"]), "catalyst-id", 255),
        "description": _validate_text(
            str(device.get("type") or "Cisco network gear imported from Catalyst Center"),
            "description",
            1000,
        ),
    }


def list_gear(**kwargs: Any) -> dict[str, Any]:
    records = _find_ci(serial=kwargs.get("serial"), sys_id=kwargs.get("sys_id"))
    results = []
    for ci in records:
        results.append({"ci": ci, "assets": _find_assets(str(ci.get("sys_id", "")))})
    return _response("success", {"count": len(results), "records": results}, ["Use get-gear for one known serial or sys_id."])


def get_gear(**kwargs: Any) -> dict[str, Any]:
    serial, sys_id = kwargs.get("serial"), kwargs.get("sys_id")
    if not serial and not sys_id:
        raise ValueError("--serial or --sys-id is required.")
    records = _find_ci(serial=serial, sys_id=sys_id)
    if not records:
        return _response("warning", {"found": False}, ["No matching Network Gear CI exists."])
    if len(records) > 1:
        return _response("error", {"records": records}, ["Serial number is not unique; use --sys-id."])
    ci = records[0]
    return _response("success", {"found": True, "ci": ci, "assets": _find_assets(str(ci["sys_id"]))}, ["The CI and linked hardware asset are shown."])


def create_gear(**kwargs: Any) -> dict[str, Any]:
    hostname = _validate_hostname(kwargs.get("hostname"))
    ip = _validate_ip(kwargs.get("ip"))
    serial = _validate_serial(kwargs.get("serial"))
    platform = _validate_text(kwargs.get("platform"), "platform")
    software = _validate_text(kwargs.get("software"), "software", 40)
    description = _validate_text(kwargs.get("description") or "Cisco network gear imported from Catalyst Center", "description", 1000)
    mac = _validate_mac(kwargs.get("mac"))
    catalyst_id = _validate_text(kwargs.get("catalyst_id"), "catalyst-id", 255)
    if not kwargs.get("confirm"):
        return _response("warning", {"hostname": hostname, "serial_number": serial, "created": False}, ["Creation requires explicit approval and --confirm."])

    existing = _find_ci(serial=serial)
    ci_created = False
    manufacturer_id: Optional[str] = None
    if existing:
        if str(existing[0].get("correlation_id", "")) == f"{MANAGED_PREFIX}{catalyst_id}":
            ci = existing[0]
            assets = _find_assets(str(ci["sys_id"]))
            if assets:
                return _response("success", {"created": False, "ci": ci, "assets": assets}, ["No duplicate was created."])
        else:
            return _response("error", {"existing_records": existing}, ["The serial number already exists; resolve the conflict manually."])
    else:
        manufacturer_id = _reference_sys_id(COMPANY_TABLE, "name=Cisco^manufacturer=true", "Cisco manufacturer")
        ci_payload = {
            "name": hostname,
            "ip_address": ip,
            "serial_number": serial,
            "model_number": platform,
            "manufacturer": manufacturer_id,
            "mac_address": mac or "",
            "firmware_version": software,
            "short_description": description,
            "discovery_source": "Other Automated",
            "correlation_id": f"{MANAGED_PREFIX}{catalyst_id}",
            "install_status": "1",
            "operational_status": "1",
        }
        response = _request("POST", CI_TABLE, params={"sysparm_fields": "sys_id,name,ip_address,serial_number,model_number,manufacturer,mac_address,firmware_version,correlation_id,asset", "sysparm_exclude_reference_link": "true"}, payload=ci_payload)
        ci = response.get("result", {}) if isinstance(response, dict) else {}
        if not ci.get("sys_id"):
            raise ValueError("ServiceNow created no identifiable Network Gear CI.")
        ci_created = True

    manufacturer_id = manufacturer_id or _reference_sys_id(COMPANY_TABLE, "name=Cisco^manufacturer=true", "Cisco manufacturer")
    category_id = _reference_sys_id(CATEGORY_TABLE, "name=Network Gear^asset_class=alm_hardware", "Network Gear category")

    asset_payload = {
        "asset_tag": f"CATC-{serial}",
        "serial_number": serial,
        "manufacturer": manufacturer_id,
        "model_category": category_id,
        "install_status": "1",
        "ci": ci["sys_id"],
        "comments": f"{ASSET_MARKER}; platform={platform}; catalyst_id={catalyst_id}",
    }
    try:
        asset_response = _request("POST", ASSET_TABLE, params={"sysparm_fields": "sys_id,display_name,asset_tag,serial_number,manufacturer,model_category,install_status,ci,comments", "sysparm_exclude_reference_link": "true"}, payload=asset_payload)
    except Exception:
        if ci_created:
            _request("DELETE", CI_TABLE, str(ci["sys_id"]))
        raise
    asset = asset_response.get("result", {}) if isinstance(asset_response, dict) else {}
    return _response(
        "success",
        {"created": True, "ci_created": ci_created, "asset_created": True, "ci": ci, "asset": asset},
        ["Run get-gear by serial to verify the linked records."],
    )


def update_gear(**kwargs: Any) -> dict[str, Any]:
    sys_id = kwargs.get("sys_id")
    if not sys_id:
        raise ValueError("--sys-id is required for update-gear.")
    records = _find_ci(sys_id=sys_id)
    if not records:
        return _response("warning", {"updated": False}, ["No matching Network Gear CI exists."])
    ci = records[0]
    _assert_managed(ci)
    supplied_fields = [
        name for name in ("hostname", "ip", "platform", "software", "mac", "description")
        if kwargs.get(name)
    ]
    if len(supplied_fields) != 1:
        raise ValueError(
            f"Provide exactly one mutable field per update; received {len(supplied_fields)}."
        )
    payload: dict[str, Any] = {}
    if kwargs.get("hostname"):
        payload["name"] = _validate_hostname(kwargs["hostname"])
    if kwargs.get("ip"):
        payload["ip_address"] = _validate_ip(kwargs["ip"])
    if kwargs.get("platform"):
        payload["model_number"] = _validate_text(kwargs["platform"], "platform")
    if kwargs.get("software"):
        payload["firmware_version"] = _validate_text(kwargs["software"], "software", 40)
    if kwargs.get("mac"):
        payload["mac_address"] = _validate_mac(kwargs["mac"])
    if kwargs.get("description"):
        payload["short_description"] = _validate_text(kwargs["description"], "description", 1000)
    if not kwargs.get("confirm"):
        return _response("warning", {"sys_id": sys_id, "proposed_changes": payload, "updated": False}, ["Update requires explicit approval and --confirm."])
    response = _request("PATCH", CI_TABLE, sys_id, params={"sysparm_exclude_reference_link": "true"}, payload=payload)
    return _response("success", {"updated": True, "record": response.get("result", {})}, ["Run get-gear to verify the update."])


def delete_gear(**kwargs: Any) -> dict[str, Any]:
    sys_id = kwargs.get("sys_id")
    if not sys_id:
        raise ValueError("--sys-id is required for delete-gear.")
    records = _find_ci(sys_id=sys_id)
    if not records:
        return _response("success", {"deleted": False}, ["No matching record exists; no change was made."])
    ci = records[0]
    _assert_managed(ci)
    assets = _find_assets(sys_id)
    for asset in assets:
        if ASSET_MARKER not in str(asset.get("comments", "")):
            raise ValueError("A linked asset is not managed by this skill; deletion was stopped.")
    if not kwargs.get("confirm"):
        return _response("warning", {"deleted": False, "ci": ci, "assets": assets}, ["Deletion requires explicit approval and --confirm."])
    for asset in assets:
        _request("DELETE", ASSET_TABLE, str(asset["sys_id"]))
    _request("DELETE", CI_TABLE, sys_id)
    return _response("success", {"deleted": True, "ci_sys_id": sys_id, "assets_deleted": len(assets)}, ["Run get-gear to confirm removal."])


def import_catalyst(**kwargs: Any) -> dict[str, Any]:
    """Deterministically plan or import every valid Catalyst Center device."""
    devices = _catalyst_inventory()
    plan: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    seen_serials: set[str] = set()
    seen_ids: set[str] = set()
    for device in sorted(devices, key=lambda item: str(item.get("hostname") or "").casefold()):
        try:
            payload = _inventory_payload(device)
            if payload["serial"] in seen_serials:
                raise ValueError(f"Duplicate Catalyst serial number: {payload['serial']}.")
            if payload["catalyst_id"] in seen_ids:
                raise ValueError(f"Duplicate Catalyst device ID: {payload['catalyst_id']}.")
            seen_serials.add(payload["serial"])
            seen_ids.add(payload["catalyst_id"])
            plan.append(payload)
        except ValueError as exc:
            errors.append({"device": str(device.get("hostname") or device.get("id") or "(unknown)"), "error": str(exc)})

    if errors:
        return _response(
            "error",
            {"inventory_count": len(devices), "valid_count": len(plan), "errors": errors, "plan": plan},
            ["Correct incomplete or duplicate Catalyst inventory before importing anything."],
        )
    if not kwargs.get("confirm"):
        return _response(
            "warning",
            {"inventory_count": len(devices), "planned_count": len(plan), "created": False, "plan": plan},
            ["Review the deterministic plan, then rerun import-catalyst with --confirm."],
        )

    outcomes = []
    for payload in plan:
        outcome = create_gear(**payload, confirm=True)
        outcomes.append({"hostname": payload["hostname"], **outcome})
        if outcome.get("status") == "error":
            return _response(
                "error",
                {"inventory_count": len(devices), "processed_count": len(outcomes), "outcomes": outcomes},
                ["Import stopped on the first conflict or API error; rerunning is safe and idempotent."],
            )
    created_count = sum(1 for item in outcomes if item.get("results", {}).get("created"))
    return _response(
        "success",
        {
            "inventory_count": len(devices),
            "processed_count": len(outcomes),
            "created_count": created_count,
            "unchanged_count": len(outcomes) - created_count,
            "outcomes": outcomes,
        },
        ["Rerunning import-catalyst is safe: exact existing records are returned unchanged."],
    )


def handle_command(command: str, **kwargs: Any) -> dict[str, Any]:
    normalized = (command or "").strip().lower().replace("_", "-")
    handlers = {
        "list-gear": list_gear,
        "get-gear": get_gear,
        "create-gear": create_gear,
        "update-gear": update_gear,
        "delete-gear": delete_gear,
        "import-catalyst": import_catalyst,
    }
    handler = handlers.get(normalized)
    if handler is None:
        return _response("error", [], [f"Supported commands: {', '.join(handlers)}."])
    try:
        return handler(**kwargs)
    except ValueError as exc:
        return _response("error", [], [str(exc)])
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "unknown"
        return _response("error", [], [f"ServiceNow development API returned HTTP {code}."])
    except requests.RequestException as exc:
        return _response("error", [], [f"ServiceNow request failed: {type(exc).__name__}."])


def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded ServiceNow Network Gear CRUD")
    parser.add_argument("command", choices=["list-gear", "get-gear", "create-gear", "update-gear", "delete-gear", "import-catalyst"])
    parser.add_argument("--sys-id")
    parser.add_argument("--hostname")
    parser.add_argument("--ip")
    parser.add_argument("--serial")
    parser.add_argument("--platform")
    parser.add_argument("--software")
    parser.add_argument("--mac")
    parser.add_argument("--description")
    parser.add_argument("--catalyst-id")
    parser.add_argument("--confirm", action="store_true")
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
