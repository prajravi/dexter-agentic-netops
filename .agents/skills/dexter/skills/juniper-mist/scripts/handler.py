#!/usr/bin/env python3
"""Guarded Juniper Mist organization, site, WLAN, and inventory workflows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
TIMEZONE_RE = re.compile(r"^[A-Za-z_+-]+(?:/[A-Za-z0-9_+.-]+)+$")
DEXTER_MARKER = "Managed by Dexter Agent Skill"
DEFAULT_TIMEOUT = 30
PAGE_SIZE = 100
MAX_RECORDS = 5_000


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


def _load_configuration(for_write: bool = False) -> tuple[str, str, str]:
    """Load the regional endpoint, organization, and least-privileged available token."""
    _load_environment()
    raw_api = os.getenv("MIST_API_HOST", "").strip().rstrip("/")
    allowed_host = os.getenv("MIST_ALLOWED_HOST", "").strip().lower()
    org_id = os.getenv("MIST_ORG_ID", "").strip().lower()
    fallback_token = os.getenv("MIST_API_TOKEN", "").strip()
    read_token = os.getenv("MIST_READ_TOKEN", "").strip() or fallback_token
    write_token = os.getenv("MIST_WRITE_TOKEN", "").strip() or fallback_token
    token = write_token if for_write else read_token
    parsed = urlparse(raw_api)
    allowed = urlparse(f"//{allowed_host}")
    if not allowed_host or not HOST_RE.fullmatch(allowed_host) or allowed.hostname != allowed_host:
        raise ValueError("MIST_ALLOWED_HOST must be one API hostname without a scheme or port.")
    if (
        parsed.scheme != "https"
        or parsed.hostname != allowed_host
        or parsed.port is not None
        or parsed.path not in {"", "/"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("MIST_API_HOST must be HTTPS and exactly match MIST_ALLOWED_HOST.")
    if not UUID_RE.fullmatch(org_id):
        raise ValueError("MIST_ORG_ID must be a valid organization UUID.")
    if not token or any(char in token for char in "\r\n\x00"):
        variable = "MIST_WRITE_TOKEN or MIST_API_TOKEN" if for_write else "MIST_READ_TOKEN or MIST_API_TOKEN"
        raise ValueError(f"{variable} must be set securely in the Dexter environment.")
    return f"https://{allowed_host}", org_id, token


def _validate_uuid(value: str, label: str) -> str:
    normalized = (value or "").strip().lower()
    if not UUID_RE.fullmatch(normalized):
        raise ValueError(f"{label} must be a valid UUID.")
    return normalized


def _allowed_path(method: str, path: str, org_id: str) -> bool:
    uuid = r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    escaped_org = re.escape(org_id)
    get_patterns = (
        r"/api/v1/self",
        rf"/api/v1/orgs/{escaped_org}",
        rf"/api/v1/orgs/{escaped_org}/sites",
        rf"/api/v1/orgs/{escaped_org}/inventory",
        rf"/api/v1/sites/{uuid}/wlans",
    )
    post_patterns = (
        rf"/api/v1/orgs/{escaped_org}/sites",
        rf"/api/v1/sites/{uuid}/wlans",
    )
    patterns = get_patterns if method == "GET" else post_patterns if method == "POST" else ()
    return any(re.fullmatch(pattern, path, re.I) for pattern in patterns)


def _request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    payload: Optional[dict[str, Any]] = None,
) -> Any:
    normalized_method = method.upper()
    api_host, org_id, token = _load_configuration(for_write=normalized_method == "POST")
    if not _allowed_path(normalized_method, path, org_id):
        raise ValueError("The requested Mist method or API path is not allowed by this skill.")
    response = requests.request(
        normalized_method,
        f"{api_host}{path}",
        params=params,
        json=payload,
        headers={
            "Authorization": f"Token {token}",
            "Accept": "application/json, application/vnd.api+json",
            "Content-Type": "application/json",
            "User-Agent": "dexter-agentic-netops",
        },
        timeout=DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def _paged_get(path: str, max_records: int = MAX_RECORDS) -> tuple[list[dict[str, Any]], bool]:
    if not 1 <= max_records <= MAX_RECORDS:
        raise ValueError(f"--max-records must be between 1 and {MAX_RECORDS}.")
    records: list[dict[str, Any]] = []
    page_fingerprints: set[str] = set()
    page = 1
    while len(records) < max_records:
        limit = min(PAGE_SIZE, max_records - len(records))
        data = _request("GET", path, params={"limit": limit, "page": page})
        batch = data if isinstance(data, list) else data.get("results", [])
        if not isinstance(batch, list):
            raise ValueError("Mist returned an unexpected paginated response.")
        if any(not isinstance(item, dict) for item in batch):
            raise ValueError("Mist returned a non-object item in a paginated response.")
        fingerprint = hashlib.sha256(
            json.dumps(batch, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if batch and fingerprint in page_fingerprints:
            raise ValueError("Mist pagination repeated a page; stopping to prevent duplicate or unbounded results.")
        page_fingerprints.add(fingerprint)
        records.extend(batch)
        if len(batch) < limit:
            return records, True
        page += 1
    return records, False


def _safe_site(site: dict[str, Any]) -> dict[str, Any]:
    return {
        key: site.get(key)
        for key in ("id", "name", "country_code", "timezone", "address", "latlng", "notes")
    }


def show_organization() -> dict[str, Any]:
    _, org_id, _ = _load_configuration()
    account = _request("GET", "/api/v1/self")
    organization = _request("GET", f"/api/v1/orgs/{org_id}")
    privileges = [
        {key: item.get(key) for key in ("scope", "role", "site_id")}
        for item in account.get("privileges", [])
        if isinstance(item, dict) and str(item.get("org_id", "")).lower() == org_id
    ]
    configured_matches = str(organization.get("id", "")).lower() == org_id
    if not configured_matches:
        raise ValueError("Mist returned an organization that does not match MIST_ORG_ID.")
    if not privileges:
        raise ValueError("The token reports no privilege for the configured Mist organization.")
    result = {
        "organization": {
            key: organization.get(key)
            for key in ("id", "name", "created_time", "modified_time")
        },
        "configured_org_id_matches": configured_matches,
        "token_privileges": privileges,
    }
    return _response("success", result, ["Use list-sites or inventory-summary for organization details."])


def list_sites(max_records: int = MAX_RECORDS) -> dict[str, Any]:
    _, org_id, _ = _load_configuration()
    sites, complete = _paged_get(f"/api/v1/orgs/{org_id}/sites", max_records)
    status = "success" if complete else "warning"
    return _response(
        status,
        {"count": len(sites), "complete": complete, "sites": [_safe_site(site) for site in sites]},
        ["All sites were returned." if complete else "The protective record cap was reached."],
    )


def _all_sites() -> list[dict[str, Any]]:
    _, org_id, _ = _load_configuration()
    sites, complete = _paged_get(f"/api/v1/orgs/{org_id}/sites")
    if not complete:
        raise ValueError("Site resolution reached the protective record cap.")
    return sites


def _site_by_name(name: str) -> Optional[dict[str, Any]]:
    normalized = _validate_site_name(name)
    matches = [site for site in _all_sites() if str(site.get("name", "")).casefold() == normalized.casefold()]
    if len(matches) > 1:
        raise ValueError(f"Multiple Mist sites match {normalized!r}; resolve the duplicate names first.")
    return matches[0] if matches else None


def _resolve_site(name: str) -> dict[str, Any]:
    site = _site_by_name(name)
    if site is None:
        raise ValueError(f"Mist site {name!r} was not found.")
    _validate_uuid(str(site.get("id", "")), "Resolved site ID")
    return site


def inventory_summary(max_records: int = MAX_RECORDS) -> dict[str, Any]:
    _, org_id, _ = _load_configuration()
    inventory, complete = _paged_get(f"/api/v1/orgs/{org_id}/inventory", max_records)
    site_names = {str(site.get("id")): str(site.get("name")) for site in _all_sites()}

    def counts(field: str, fallback: str = "unknown") -> list[dict[str, Any]]:
        values = Counter(str(item.get(field) or fallback) for item in inventory)
        return [{"value": value, "count": count} for value, count in sorted(values.items())]

    site_counts = Counter(
        site_names.get(str(item.get("site_id")), "unassigned") for item in inventory
    )
    results = {
        "device_count": len(inventory),
        "complete": complete,
        "by_type": counts("type"),
        "by_model": counts("model"),
        "by_site": [{"site": site, "count": count} for site, count in sorted(site_counts.items())],
        "connected_count": sum(item.get("connected") is True for item in inventory),
        "disconnected_count": sum(item.get("connected") is False for item in inventory),
        "devices": [
            {key: item.get(key) for key in ("id", "name", "type", "model", "serial", "mac", "site_id", "connected")}
            for item in inventory
        ],
    }
    return _response(
        "success" if complete else "warning",
        results,
        ["Inventory is empty." if not inventory else "Inventory summary is ready."],
    )


def list_wlans(site_name: str, max_records: int = MAX_RECORDS) -> dict[str, Any]:
    site = _resolve_site(site_name)
    site_id = _validate_uuid(str(site.get("id", "")), "Resolved site ID")
    wlans, complete = _paged_get(f"/api/v1/sites/{site_id}/wlans", max_records)
    sanitized = [
        {
            "id": item.get("id"),
            "ssid": item.get("ssid"),
            "enabled": item.get("enabled"),
            "hide_ssid": item.get("hide_ssid"),
            "bands": item.get("bands"),
            "auth_type": (item.get("auth") or {}).get("type"),
        }
        for item in wlans
    ]
    return _response(
        "success" if complete else "warning",
        {"site": _safe_site(site), "count": len(sanitized), "complete": complete, "wlans": sanitized},
        ["All WLANs were returned." if complete else "The protective record cap was reached."],
    )


def _validate_site_name(value: str) -> str:
    normalized = (value or "").strip()
    if not 1 <= len(normalized) <= 64 or any(char in normalized for char in "\r\n\x00"):
        raise ValueError("--name must contain 1-64 characters without controls.")
    return normalized


def _validate_site_inputs(name: str, country_code: str, timezone: str, address: Optional[str]) -> dict[str, Any]:
    normalized_country = (country_code or "").strip().upper()
    normalized_timezone = (timezone or "").strip()
    normalized_address = (address or "").strip()
    if not COUNTRY_RE.fullmatch(normalized_country):
        raise ValueError("--country-code must be a two-letter ISO country code.")
    if not TIMEZONE_RE.fullmatch(normalized_timezone):
        raise ValueError("--timezone must be an IANA timezone such as Asia/Kolkata.")
    if len(normalized_address) > 255 or any(char in normalized_address for char in "\r\n\x00"):
        raise ValueError("--address must be at most 255 characters without controls.")
    payload: dict[str, Any] = {
        "name": _validate_site_name(name),
        "country_code": normalized_country,
        "timezone": normalized_timezone,
        "notes": DEXTER_MARKER,
    }
    if normalized_address:
        payload["address"] = normalized_address
    return payload


def _confirmation_token(operation: str, plan: dict[str, Any]) -> str:
    encoded = json.dumps({"operation": operation, "plan": plan}, sort_keys=True, separators=(",", ":"))
    return f"dexter:{operation}:{hashlib.sha256(encoded.encode()).hexdigest()[:24]}"


def _site_matches(site: dict[str, Any], payload: dict[str, Any]) -> bool:
    expected = {key: payload.get(key, "") for key in ("name", "country_code", "timezone", "address")}
    actual = {key: site.get(key, "") or "" for key in expected}
    return str(site.get("notes", "")).strip() == DEXTER_MARKER and expected == actual


def _ambiguous_write(exc: requests.RequestException) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    return isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code >= 500


def _site_plan(name: str, country_code: str, timezone: str, address: Optional[str]) -> dict[str, Any]:
    payload = _validate_site_inputs(name, country_code, timezone, address)
    existing = _site_by_name(payload["name"])
    if existing is None:
        action = "create"
        reason = "No site with this name exists."
    elif str(existing.get("notes", "")).strip() != DEXTER_MARKER:
        action = "blocked"
        reason = "A site with this name exists but is not marked as managed by Dexter."
    else:
        action = "unchanged" if _site_matches(existing, payload) else "blocked"
        reason = "The managed site already matches." if action == "unchanged" else "The managed site exists with different settings."
    public_plan = {**payload, "action": action, "reason": reason}
    public_plan["existing_site_id"] = existing.get("id") if existing else None
    if action == "create":
        public_plan["confirmation_token"] = _confirmation_token("create-site", payload)
    return {"payload": payload, "public": public_plan}


def create_site_plan(name: str, country_code: str, timezone: str, address: Optional[str] = None) -> dict[str, Any]:
    plan = _site_plan(name, country_code, timezone, address)["public"]
    status = "error" if plan["action"] == "blocked" else "success" if plan["action"] == "unchanged" else "warning"
    next_step = {
        "create": "No mutation occurred. Review this exact plan before approval.",
        "unchanged": "No mutation occurred; the managed site already matches.",
        "blocked": "No mutation occurred; resolve the conflict without overwriting the site.",
    }[plan["action"]]
    return _response(status, plan, [next_step])


def create_site(
    name: str,
    country_code: str,
    timezone: str,
    address: Optional[str] = None,
    confirm: bool = False,
    confirm_target: Optional[str] = None,
) -> dict[str, Any]:
    plan = _site_plan(name, country_code, timezone, address)
    public = plan["public"]
    if public["action"] == "blocked":
        return _response("error", public, [public["reason"]])
    if public["action"] == "unchanged":
        return _response("success", {**public, "created": False}, ["The managed site already matches; no write was sent."])
    if not confirm or confirm_target != public["confirmation_token"]:
        return _response("warning", {**public, "created": False}, ["Explicit approval and the exact confirmation token are required."])
    _, org_id, _ = _load_configuration()
    try:
        _request("POST", f"/api/v1/orgs/{org_id}/sites", payload=plan["payload"])
    except requests.RequestException as exc:
        if not _ambiguous_write(exc):
            raise
        try:
            verified = _site_by_name(plan["payload"]["name"])
        except requests.RequestException:
            verified = None
        if verified and _site_matches(verified, plan["payload"]):
            return _response(
                "warning",
                {"created": "unknown", "verified": True, "site": _safe_site(verified)},
                ["The write response was ambiguous, but a fresh read found the exact site. Do not retry; review the Mist audit log."],
            )
        return _response(
            "error",
            {"created": "unknown", "verified": False},
            ["The write response was ambiguous and the exact site was not verified. Do not retry; inspect Mist sites and audit logs first."],
        )
    verified = _site_by_name(plan["payload"]["name"])
    if not verified or not _site_matches(verified, plan["payload"]):
        return _response("error", {"created": True, "verified": False}, ["Mist accepted the create request, but verification did not find the exact managed site configuration."])
    return _response("success", {"created": True, "verified": True, "site": _safe_site(verified)}, ["The site was created and verified."])


def _validate_ssid(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized or len(normalized.encode("utf-8")) > 32 or any(char in normalized for char in "\r\n\x00"):
        raise ValueError("--ssid must contain 1-32 UTF-8 bytes without controls.")
    return normalized


def _wlan_payload(ssid: str, security: str) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized_security = (security or "psk").strip().lower()
    if normalized_security not in {"open", "psk"}:
        raise ValueError("--security must be open or psk.")
    payload: dict[str, Any] = {
        "ssid": _validate_ssid(ssid),
        "enabled": False,
        "hide_ssid": False,
        "bands": ["24", "5"],
        "auth": {"type": normalized_security},
    }
    public: dict[str, Any] = {
        "ssid": payload["ssid"],
        "enabled": False,
        "hide_ssid": False,
        "bands": payload["bands"],
        "security": normalized_security,
    }
    if normalized_security == "psk":
        _load_environment()
        psk = os.getenv("MIST_WLAN_PSK", "")
        if not 8 <= len(psk) <= 63 or any(char in psk for char in "\r\n\x00"):
            raise ValueError("MIST_WLAN_PSK must contain 8-63 characters in the Dexter environment.")
        payload["auth"]["psk"] = psk
        public["psk_configured"] = True
        public["psk_binding"] = hashlib.sha256(psk.encode()).hexdigest()
    return payload, public


def _site_wlans(site_id: str) -> list[dict[str, Any]]:
    wlans, complete = _paged_get(f"/api/v1/sites/{_validate_uuid(site_id, 'Site ID')}/wlans")
    if not complete:
        raise ValueError("WLAN resolution reached the protective record cap.")
    return wlans


def _wlan_matches(existing: dict[str, Any], payload: dict[str, Any]) -> bool:
    actual_auth = (existing.get("auth") or {}).get("type") or "open"
    return (
        existing.get("ssid") == payload["ssid"]
        and existing.get("enabled") is False
        and existing.get("hide_ssid") is False
        and set(existing.get("bands") or []) == set(payload["bands"])
        and actual_auth == payload["auth"]["type"]
    )


def _wlan_plan(site_name: str, ssid: str, security: str) -> dict[str, Any]:
    site = _resolve_site(site_name)
    site_id = _validate_uuid(str(site.get("id", "")), "Resolved site ID")
    payload, public_config = _wlan_payload(ssid, security)
    matches = [item for item in _site_wlans(site_id) if item.get("ssid") == payload["ssid"]]
    if len(matches) > 1:
        action, reason = "blocked", "Multiple WLANs use this SSID at the selected site."
    elif not matches:
        action, reason = "create", "No WLAN with this SSID exists at the selected site."
    elif _wlan_matches(matches[0], payload):
        action, reason = "unchanged", "The existing disabled WLAN matches the requested profile."
    else:
        action, reason = "blocked", "The SSID exists with different settings; Dexter will not overwrite it."
    token_plan = {"site_id": site_id, **public_config}
    public_config.pop("psk_binding", None)
    public = {
        "site": _safe_site(site),
        "configuration": public_config,
        "action": action,
        "reason": reason,
    }
    if action == "create":
        public["confirmation_token"] = _confirmation_token("create-wlan", token_plan)
    return {"site_id": site_id, "payload": payload, "public": public}


def create_wlan_plan(site_name: str, ssid: str, security: str = "psk") -> dict[str, Any]:
    plan = _wlan_plan(site_name, ssid, security)["public"]
    status = "error" if plan["action"] == "blocked" else "success" if plan["action"] == "unchanged" else "warning"
    next_step = {
        "create": "No mutation occurred. Review this exact disabled WLAN plan before approval.",
        "unchanged": "No mutation occurred; the disabled WLAN already matches.",
        "blocked": "No mutation occurred; resolve the WLAN conflict without overwriting it.",
    }[plan["action"]]
    return _response(status, plan, [next_step])


def create_wlan(
    site_name: str,
    ssid: str,
    security: str = "psk",
    confirm: bool = False,
    confirm_target: Optional[str] = None,
) -> dict[str, Any]:
    plan = _wlan_plan(site_name, ssid, security)
    public = plan["public"]
    if public["action"] == "blocked":
        return _response("error", public, [public["reason"]])
    if public["action"] == "unchanged":
        return _response("success", {**public, "created": False}, ["The WLAN already matches; no write was sent."])
    if not confirm or confirm_target != public["confirmation_token"]:
        return _response("warning", {**public, "created": False}, ["Explicit approval and the exact confirmation token are required."])
    try:
        _request("POST", f"/api/v1/sites/{plan['site_id']}/wlans", payload=plan["payload"])
    except requests.RequestException as exc:
        if not _ambiguous_write(exc):
            raise
        try:
            verified = [item for item in _site_wlans(plan["site_id"]) if item.get("ssid") == plan["payload"]["ssid"]]
        except requests.RequestException:
            verified = []
        if len(verified) == 1 and _wlan_matches(verified[0], plan["payload"]):
            return _response(
                "warning",
                {
                    "created": "unknown", "verified": True, "site": public["site"],
                    "wlan": {"id": verified[0].get("id"), **public["configuration"]},
                },
                ["The write response was ambiguous, but a fresh read found the exact disabled WLAN. Do not retry; review the Mist audit log."],
            )
        return _response(
            "error",
            {"created": "unknown", "verified": False, "site": public["site"]},
            ["The write response was ambiguous and the exact WLAN was not verified. Do not retry; inspect Mist WLANs and audit logs first."],
        )
    verified = [item for item in _site_wlans(plan["site_id"]) if item.get("ssid") == plan["payload"]["ssid"]]
    if len(verified) != 1 or not _wlan_matches(verified[0], plan["payload"]):
        return _response("error", {"created": True, "verified": False}, ["Mist accepted the create request, but the disabled WLAN profile could not be verified."])
    return _response(
        "success",
        {
            "created": True,
            "verified": True,
            "site": public["site"],
            "wlan": {"id": verified[0].get("id"), **public["configuration"]},
        },
        ["The WLAN was created disabled and verified. Review production security before enabling it."],
    )


def handle_command(command: str, **kwargs: Any) -> dict[str, Any]:
    normalized = (command or "").strip().lower().replace("_", "-")
    try:
        if normalized == "show-organization":
            return show_organization()
        if normalized == "list-sites":
            return list_sites(kwargs.get("max_records", MAX_RECORDS))
        if normalized == "inventory-summary":
            return inventory_summary(kwargs.get("max_records", MAX_RECORDS))
        if normalized == "list-wlans":
            return list_wlans(kwargs.get("site_name"), kwargs.get("max_records", MAX_RECORDS))
        if normalized == "create-site-plan":
            return create_site_plan(kwargs.get("name"), kwargs.get("country_code"), kwargs.get("timezone"), kwargs.get("address"))
        if normalized == "create-site":
            return create_site(
                kwargs.get("name"), kwargs.get("country_code"), kwargs.get("timezone"), kwargs.get("address"),
                kwargs.get("confirm", False), kwargs.get("confirm_target"),
            )
        if normalized == "create-wlan-plan":
            return create_wlan_plan(kwargs.get("site_name"), kwargs.get("ssid"), kwargs.get("security", "psk"))
        if normalized == "create-wlan":
            return create_wlan(
                kwargs.get("site_name"), kwargs.get("ssid"), kwargs.get("security", "psk"),
                kwargs.get("confirm", False), kwargs.get("confirm_target"),
            )
        return _response("error", [], ["Unsupported Mist command."])
    except ValueError as exc:
        return _response("error", [], [str(exc)])
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "unknown"
        retry_after = exc.response.headers.get("Retry-After") if exc.response is not None else None
        detail = f" Retry after {retry_after} seconds." if code == 429 and retry_after else ""
        return _response("error", [], [f"Mist API returned HTTP {code}.{detail}"])
    except requests.RequestException as exc:
        return _response("error", [], [f"Mist API request failed: {type(exc).__name__}."])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        return _response("error", [], [f"Mist returned an invalid response: {type(exc).__name__}."])


COMMANDS = (
    "show-organization", "list-sites", "inventory-summary", "list-wlans",
    "create-site-plan", "create-site", "create-wlan-plan", "create-wlan",
)


def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded Juniper Mist NetOps handler")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--max-records", type=int, default=MAX_RECORDS)
    parser.add_argument("--name", help="Site name")
    parser.add_argument("--country-code", help="Two-letter ISO country code")
    parser.add_argument("--timezone", help="IANA timezone")
    parser.add_argument("--address", help="Optional site address")
    parser.add_argument("--site-name", help="Exact Mist site name")
    parser.add_argument("--ssid", help="WLAN SSID")
    parser.add_argument("--security", choices=["open", "psk"], default="psk")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--confirm-target", help="Exact token returned by the matching plan")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    values = vars(args).copy()
    command = values.pop("command")
    pretty = values.pop("pretty")
    result = handle_command(command, **values)
    print(json.dumps(result, indent=2 if pretty else None, sort_keys=False))
    return 0 if result.get("status") in {"success", "warning"} else 1


if __name__ == "__main__":
    sys.exit(_cli())
