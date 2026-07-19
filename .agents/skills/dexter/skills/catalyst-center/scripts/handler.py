#!/usr/bin/env python3
"""Cisco Catalyst Center skill handler with guarded access-port bounce support."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import urllib3
from dnacentersdk import DNACenterAPI
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SCRIPT_DIR = Path(__file__).resolve().parent


def _load_environment() -> None:
    """Load an explicit or repository-local environment file when present."""
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


def _response(status: str, results: Any, next_steps: list[str]) -> dict[str, Any]:
    """Build the standard, machine-readable skill response."""
    return {"status": status, "results": results, "next_steps": next_steps}


def _error(code: str, results: Any, message: str) -> dict[str, Any]:
    """Build a structured error with a stable machine-readable code."""
    return {**_response("error", results, [message]), "error_code": code}


def _load_configuration() -> tuple[str, str, str]:
    """Load and validate the Catalyst Center connection configuration."""
    _load_environment()
    controller = os.getenv("CATC_CONTROLLER", "").strip()
    username = os.getenv("CATC_USERNAME", "").strip()
    password = os.getenv("CATC_PASSWORD", "")
    controller_host = urlparse(f"https://{controller}").hostname
    if not controller_host:
        raise ValueError("CATC_CONTROLLER must be a valid Catalyst Center hostname or IP address.")
    if not username or not password:
        raise ValueError(
            "CATC_USERNAME and CATC_PASSWORD must be set in the Dexter root .env file."
        )
    return controller_host, username, password


def _api() -> DNACenterAPI:
    """Create an authenticated SDK client for the configured controller."""
    controller, username, password = _load_configuration()
    return DNACenterAPI(
        base_url=f"https://{controller}",
        username=username,
        password=password,
        verify=False,
    )


def _inventory(api: DNACenterAPI, hostname: Optional[str] = None, **filters: Any) -> list[dict[str, Any]]:
    """Return inventory records, safely delegating filtering to the SDK."""
    params: dict[str, Any] = {"limit": 500}
    if hostname:
        params["hostname"] = hostname if "*" in hostname or "." in hostname else f"{hostname}.*"
    params.update({key: value for key, value in filters.items() if value is not None})
    return (api.devices.get_device_list(**params) or {}).get("response", [])


def _resolve_device_id(api: DNACenterAPI, hostname: Optional[str], device_id: Optional[str]) -> str:
    """Resolve a device UUID from either a supplied ID or a hostname."""
    if device_id:
        if hostname:
            device = (api.devices.get_device_by_id(id=device_id) or {}).get("response", {})
            actual = str(device.get("hostname", "")).split(".")[0].lower()
            expected = hostname.split(".")[0].lower()
            if not actual or actual != expected:
                raise ValueError(f"Device ID '{device_id}' does not match hostname '{hostname}'.")
        return device_id
    if not hostname:
        raise ValueError("--hostname or --device-id is required")
    records = _inventory(api, hostname)
    target = hostname.split(".")[0].lower()
    matches = [
        record
        for record in records
        if str(record.get("hostname", "")).split(".")[0].lower() == target
    ]
    if len(matches) > 1:
        raise ValueError(f"Hostname '{hostname}' is ambiguous; use --device-id.")
    record = matches[0] if matches else records[0] if len(records) == 1 else None
    if not record or not record.get("id"):
        raise ValueError(f"No Catalyst Center device matches hostname '{hostname}'.")
    return str(record["id"])


def _get(api: DNACenterAPI, path: str, params: Optional[dict[str, Any]] = None) -> Any:
    """Call a read-only Intent API endpoint through the authenticated SDK."""
    return api.custom_caller.call_api(
        "GET", f"/dna/intent/api/{path}", params={k: v for k, v in (params or {}).items() if v is not None} or None
    )


def _put(api: DNACenterAPI, path: str, body: Any) -> Any:
    """Call an Intent API PUT endpoint through the authenticated SDK."""
    return api.custom_caller.call_api("PUT", f"/dna/intent/api/{path}", json=body)


def _poll_task(api: DNACenterAPI, task_id: str, timeout: int = 60) -> dict[str, Any]:
    """Wait for a Catalyst Center task to complete, fail, or time out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        data = api.task.get_task_by_id(task_id=task_id)
        task = (data or {}).get("response", {})
        if task.get("isError"):
            return {"status": "error", "task_id": task_id, "progress": task.get("progress", "")}
        if task.get("endTime"):
            return {"status": "success", "task_id": task_id, "progress": task.get("progress", "")}
    return {"status": "error", "task_id": task_id, "progress": f"Task timed out after {timeout} seconds."}


def _interfaces(api: DNACenterAPI, device_id: str) -> list[dict[str, Any]]:
    """Return all discovered interfaces for one device."""
    return (
        api.devices.get_device_interfaces_by_specified_range(
            device_id=device_id, records_to_return=500, start_index=1
        )
        or {}
    ).get("response", [])


def _canonical_interface_name(name: str) -> str:
    """Normalize common IOS interface abbreviations for exact matching."""
    normalized = re.sub(r"\s+", "", str(name or "")).lower()
    prefixes = {
        "gi": "gigabitethernet",
        "gig": "gigabitethernet",
        "te": "tengigabitethernet",
        "twe": "twentyfivegige",
        "fo": "fortygigabitethernet",
        "hu": "hundredgige",
        "fa": "fastethernet",
        "po": "port-channel",
        "lo": "loopback",
    }
    match = re.match(r"([a-z-]+)(.*)", normalized)
    if not match:
        return normalized
    prefix, suffix = match.groups()
    return f"{prefixes.get(prefix, prefix)}{suffix}"


def _find_interface(interfaces: list[dict[str, Any]], name: str) -> Optional[dict[str, Any]]:
    """Find an interface by full name or a common IOS abbreviation."""
    target = _canonical_interface_name(name)
    return next(
        (
            interface
            for interface in interfaces
            if _canonical_interface_name(str(interface.get("portName", ""))) == target
        ),
        None,
    )


def _device_detail(api: DNACenterAPI, hostname: str) -> dict[str, Any]:
    """Return device-detail data after resolving short names and FQDNs."""
    records = _inventory(api, hostname)
    candidates = [str(records[0].get("hostname"))] if records else []
    candidates.extend([hostname, hostname.split(".")[0]])
    for candidate in dict.fromkeys(item for item in candidates if item):
        data = api.devices.get_device_detail(search_by=candidate, identifier="nwDeviceName")
        detail = (data or {}).get("response", {})
        if detail:
            return detail
    return {}


def _extract_command_output(data: Any) -> str:
    """Flatten Catalyst Center command-runner file output into text."""
    entries = data if isinstance(data, list) else [data]
    output: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for bucket in entry.get("commandResponses", {}).values():
            if isinstance(bucket, dict):
                output.extend(str(value) for value in bucket.values())
            elif bucket is not None:
                output.append(str(bucket))
    return "\n".join(output)


def _parse_cdp_neighbors(raw_text: str) -> list[dict[str, Any]]:
    """Parse show cdp neighbors detail output into structured records."""
    neighbors = []
    for block in re.split(r"-{10,}", raw_text):
        device = re.search(r"Device ID:\s*(\S+)", block)
        if not device:
            continue
        ip = re.search(r"IP address:\s*([\d.]+)", block)
        platform = re.search(r"Platform:\s*([^,\n]+)", block)
        ports = re.search(r"Interface:\s*(\S+?),.*?Port ID.*?:\s*(\S+)", block, re.DOTALL)
        neighbors.append(
            {
                "hostname": device.group(1),
                "management_ip": ip.group(1) if ip else None,
                "platform": platform.group(1).strip() if platform else None,
                "local_interface": ports.group(1).rstrip(",") if ports else None,
                "remote_interface": ports.group(2) if ports else None,
            }
        )
    return neighbors


def _unique_health_entries(data: Any) -> list[dict[str, Any]]:
    """Normalize and deduplicate device-health entries by device identity."""
    entries = data.get("response", []) if isinstance(data, dict) else []
    unique: dict[str, dict[str, Any]] = {}
    for entry in entries:
        key = str(entry.get("uuid") or entry.get("id") or entry.get("ipAddress") or entry.get("name"))
        if key and key != "None":
            unique.setdefault(key, entry)
    return list(unique.values())


def _site_health(api: DNACenterAPI, site_id: str, device_role: Optional[str] = None) -> tuple[list[dict[str, Any]], str]:
    """Query site health, falling back for controllers that reject siteId."""
    params = {"siteId": site_id, "deviceRole": device_role, "limit": 500}
    try:
        return _unique_health_entries(_get(api, "v1/device-health", params)), "site"
    except Exception:
        fallback = _get(api, "v1/device-health", {"deviceRole": device_role, "limit": 500})
        return _unique_health_entries(fallback), "controller-fallback"


def list_devices(**kwargs: Any) -> dict[str, Any]:
    """List Catalyst Center devices."""
    devices = _inventory(
        _api(), kwargs.get("hostname"), management_ip_address=kwargs.get("ip"), reachability_status=kwargs.get("reachability")
    )
    return _response("success", devices, ["Use get-device or get-interfaces with a returned device ID for more detail."])


def get_device(**kwargs: Any) -> dict[str, Any]:
    """Return one Catalyst Center device."""
    api = _api()
    device_id = kwargs.get("device_id")
    if device_id:
        device = (api.devices.get_device_by_id(id=device_id) or {}).get("response", {})
    else:
        records = _inventory(api, kwargs.get("hostname"))
        device = records[0] if records else {}
    if not device:
        return _response("warning", [], ["Confirm the hostname or device ID against list-devices."])
    return _response("success", device, ["Use get-interfaces or device-health for deeper inspection."])


def device_detail(**kwargs: Any) -> dict[str, Any]:
    """Return rich inventory, role, location, and assurance data for a device."""
    hostname = kwargs.get("hostname")
    if not hostname:
        raise ValueError("--hostname is required for device-detail")
    detail = _device_detail(_api(), hostname)
    if not detail:
        return _response("warning", [], ["Confirm the hostname with list-devices."])
    return _response("success", detail, ["Use device-health or site-device-health for health metrics."])


def redundancy_info(**kwargs: Any) -> dict[str, Any]:
    """Return controller redundancy information for a device."""
    api = _api()
    device_id = _resolve_device_id(api, kwargs.get("hostname"), kwargs.get("device_id"))
    data = _get(api, f"v1/network-device/{device_id}/redundancy-info")
    result = data.get("response", {}) if isinstance(data, dict) else {}
    status = "success" if result else "warning"
    return _response(status, result or [], ["Redundancy data is available only for supported device types."])


def get_interfaces(**kwargs: Any) -> dict[str, Any]:
    """List interfaces for a Catalyst Center device."""
    api = _api()
    device_id = _resolve_device_id(api, kwargs.get("hostname"), kwargs.get("device_id"))
    interfaces = _interfaces(api, device_id)
    return _response("success", {"device_id": device_id, "interfaces": interfaces}, ["Use get-interface to inspect one port."])


def get_interface(**kwargs: Any) -> dict[str, Any]:
    """Return one named interface for a Catalyst Center device."""
    interface_name = kwargs.get("interface")
    if not interface_name:
        raise ValueError("--interface is required for get-interface")
    result = get_interfaces(**kwargs)
    interfaces = result["results"]["interfaces"]
    match = _find_interface(interfaces, interface_name)
    if not match:
        return _response("warning", [], ["Check the exact port name with get-interfaces."])
    return _response("success", match, ["This skill is read-only; no port change was made."])


def _set_access_port_admin_state(
    api: DNACenterAPI,
    interface: dict[str, Any],
    desired_state: str,
) -> dict[str, Any]:
    """Set a validated physical access port's administrative state."""
    interface_id = interface.get("id")
    if not interface_id:
        return {"status": "error", "message": "The interface UUID is unavailable."}
    response = _put(api, f"v1/interface/{interface_id}", {"adminStatus": desired_state})
    task_id = (response or {}).get("response", {}).get("taskId")
    if not task_id:
        return {"status": "error", "message": "Catalyst Center did not return a task ID.", "response": response}
    task = _poll_task(api, task_id)
    return {**task, "admin_status": desired_state}


def _resolve_bounce_target(api: DNACenterAPI, **kwargs: Any) -> tuple[str, dict[str, Any]]:
    """Resolve and validate the exact physical access port targeted for a bounce."""
    interface_name = kwargs.get("interface")
    if not interface_name:
        raise ValueError("--interface is required for port-bounce")
    device_id = _resolve_device_id(api, kwargs.get("hostname"), kwargs.get("device_id"))
    interface = _find_interface(_interfaces(api, device_id), interface_name)
    if not interface:
        raise ValueError(f"Interface '{interface_name}' was not found; confirm it with get-interfaces.")

    interface_type = str(interface.get("interfaceType", ""))
    port_mode = str(interface.get("portMode", "")).lower()
    if interface_type.lower() != "physical":
        raise ValueError("Port bounce is allowed only on physical interfaces.")
    if port_mode not in {"access", "static-access", "dynamic_access"}:
        raise ValueError("Port bounce is allowed only on access-mode ports; trunk and routed ports are rejected.")
    return device_id, interface


def port_bounce_plan(**kwargs: Any) -> dict[str, Any]:
    """Return the validated target and token required to confirm a port bounce."""
    device_id, interface = _resolve_bounce_target(_api(), **kwargs)
    port_name = str(interface.get("portName"))
    token = f"{device_id}:{port_name}"
    return _response(
        "warning",
        {
            "device_id": device_id,
            "interface": port_name,
            "description": interface.get("description"),
            "admin_status": interface.get("adminStatus"),
            "oper_status": interface.get("status") or interface.get("operStatus"),
            "port_mode": interface.get("portMode"),
            "confirmation_token": token,
        },
        [f"Confirm this exact target by running port-bounce with --confirm-target '{token}'."],
    )


def port_bounce(**kwargs: Any) -> dict[str, Any]:
    """Bounce a validated physical access port using target-bound confirmation."""
    api = _api()
    device_id, interface = _resolve_bounce_target(api, **kwargs)
    interface_name = str(interface.get("portName"))
    expected_token = f"{device_id}:{interface_name}"
    if kwargs.get("confirm_target") != expected_token:
        return _error(
            "CONFIRMATION_REQUIRED",
            {"device_id": device_id, "interface": interface_name},
            "Run port-bounce-plan first, then supply its exact token with --confirm-target.",
        )

    delay = kwargs.get("bounce_delay", 5)
    if not isinstance(delay, int) or not 0 <= delay <= 60:
        raise ValueError("--bounce-delay must be an integer from 0 to 60 seconds")

    before_state = str(interface.get("adminStatus", "")).upper()
    if before_state == "DOWN":
        return _error(
            "PORT_ALREADY_DOWN",
            {"device_id": device_id, "interface": interface_name, "admin_status": before_state},
            "The port is already administratively down; no bounce was attempted.",
        )
    down = _set_access_port_admin_state(api, interface, "DOWN")
    if down.get("status") != "success":
        return _error(
            "PORT_DISABLE_FAILED",
            {"phase": "down", "before_state": before_state, "down": down},
            "The port was not bounced.",
        )

    time.sleep(delay)
    up = _set_access_port_admin_state(api, interface, "UP")
    if up.get("status") != "success":
        return _error(
            "PORT_RESTORE_FAILED",
            {"phase": "up", "critical": True, "before_state": before_state, "down": down, "up": up},
            "The port was disabled but could not be restored. Manual intervention is required immediately.",
        )
    return _response(
        "success",
        {
            "device_id": device_id,
            "interface": interface_name,
            "before_admin_status": before_state,
            "after_admin_status": "UP",
            "bounce_delay_seconds": delay,
            "down_task_id": down.get("task_id"),
            "up_task_id": up.get("task_id"),
        },
        ["Verify endpoint connectivity after the bounce."],
    )


def list_sites(**kwargs: Any) -> dict[str, Any]:
    """List sites, optionally filtered by name."""
    data = _get(_api(), "v2/site")
    sites = data.get("response", []) if isinstance(data, dict) else []
    name = kwargs.get("name")
    if name:
        sites = [site for site in sites if name.lower() in str(site.get("name", "")).lower()]
    return _response("success", sites, ["Use a returned site ID to scope list-issues."])


def list_issues(**kwargs: Any) -> dict[str, Any]:
    """List assurance issues scoped by optional SDK-supported query fields."""
    data = _get(_api(), "v1/issues", {"deviceId": kwargs.get("device_id"), "siteId": kwargs.get("site_id"), "priority": kwargs.get("priority")})
    issues = data if isinstance(data, list) else data.get("response", []) if isinstance(data, dict) else []
    status = "warning" if issues else "success"
    return _response(status, issues, ["Use get-issue with an issueId to review suggested actions."])


def get_issue(**kwargs: Any) -> dict[str, Any]:
    """Return a single assurance issue and its suggested actions."""
    issue_id = kwargs.get("issue_id")
    if not issue_id:
        raise ValueError("--issue-id is required for get-issue")
    data = _api().issues.get_all_the_details_and_suggested_actions_of_an_issue_for_the_given_issue_id(id=issue_id)
    issue = (data or {}).get("response", {})
    if not issue:
        return _response("warning", [], ["Confirm the issue ID with list-issues."])
    return _response("success", issue, ["Review the suggested actions before making a network change."])


def device_health(**kwargs: Any) -> dict[str, Any]:
    """Return assurance health for a device, scoped through its leaf site."""
    hostname = kwargs.get("hostname")
    if not hostname:
        raise ValueError("--hostname is required for device-health")
    api = _api()
    detail = _device_detail(api, hostname)
    hierarchy = str(detail.get("siteHierarchyGraphId", ""))
    site_id = next((segment for segment in reversed(hierarchy.split("/")) if segment), None)
    if not site_id:
        return _response("warning", [], ["Device detail did not provide a leaf site ID."])
    entries, scope = _site_health(api, site_id, kwargs.get("device_role"))
    target = str(detail.get("nwDeviceName", hostname)).split(".")[0].lower()
    health = next((entry for entry in entries if str(entry.get("name", "")).split(".")[0].lower() == target), None)
    if not health:
        return _response("warning", [], ["No matching health record is currently available."])
    next_steps = ["Review issueCount and use list-issues if issues are reported."]
    if scope != "site":
        next_steps.append("The controller rejected site scoping, so Dexter matched the device from controller-wide health data.")
    return _response("success", health, next_steps)


def site_device_health(**kwargs: Any) -> dict[str, Any]:
    """Return assurance health for all devices at a device's leaf site."""
    hostname = kwargs.get("hostname")
    if not hostname:
        raise ValueError("--hostname is required for site-device-health")
    api = _api()
    detail = _device_detail(api, hostname)
    hierarchy = str(detail.get("siteHierarchyGraphId", ""))
    site_id = next((segment for segment in reversed(hierarchy.split("/")) if segment), None)
    if not site_id:
        return _response("warning", [], ["Device detail did not provide a leaf site ID."])
    devices, scope = _site_health(api, site_id)
    status = "success" if scope == "site" else "warning"
    next_steps = ["Use list-issues with site_id to inspect assurance issues."]
    if scope != "site":
        next_steps.append("This controller rejected site scoping; returned health is controller-wide and is labeled as a fallback.")
    return _response(status, {"site_id": site_id, "scope": scope, "devices": devices}, next_steps)


def client_detail(**kwargs: Any) -> dict[str, Any]:
    """Return client details for a MAC address."""
    mac = kwargs.get("mac")
    if not mac:
        raise ValueError("--mac is required for client-detail")
    data = _api().clients.get_client_detail(mac_address=mac, timestamp=kwargs.get("timestamp"))
    detail = (data or {}).get("detail", {})
    return _response("success" if detail else "warning", detail or [], ["Verify the client MAC address and timestamp."])


def client_health(**kwargs: Any) -> dict[str, Any]:
    """Return the overall client-health response."""
    data = _api().clients.get_overall_client_health(timestamp=kwargs.get("timestamp"))
    return _response("success", (data or {}).get("response", []), ["Use client-detail for a specific MAC address."])


def physical_topology(**kwargs: Any) -> dict[str, Any]:
    """Return the current Catalyst Center physical topology graph."""
    data = _get(_api(), "v1/topology/physical-topology")
    return _response("success", (data or {}).get("response", {}), ["Use device IDs from topology nodes for inventory lookups."])


def list_neighbors(**kwargs: Any) -> dict[str, Any]:
    """Return a device's topology neighbors in a show-CDP-like format."""
    api = _api()
    device_id = _resolve_device_id(api, kwargs.get("hostname"), kwargs.get("device_id"))
    topology = (_get(api, "v1/topology/physical-topology") or {}).get("response", {})
    nodes = {str(node.get("id")): node for node in topology.get("nodes", []) if node.get("id")}
    if device_id not in nodes:
        return _response("warning", [], ["The device is not present in the current physical topology."])

    neighbors = []
    for link in topology.get("links", []):
        if link.get("source") == device_id:
            remote = nodes.get(str(link.get("target")), {})
            local_port, remote_port = link.get("startPortName"), link.get("endPortName")
        elif link.get("target") == device_id:
            remote = nodes.get(str(link.get("source")), {})
            local_port, remote_port = link.get("endPortName"), link.get("startPortName")
        else:
            continue
        neighbors.append(
            {
                "local_hostname": nodes[device_id].get("label"),
                "local_interface": local_port,
                "neighbor_hostname": remote.get("label"),
                "neighbor_management_ip": remote.get("ip"),
                "neighbor_interface": remote_port,
                "link_status": link.get("linkStatus"),
                "link_speed_kbps": link.get("startPortSpeed") or link.get("endPortSpeed"),
            }
        )
    neighbors.sort(key=lambda item: (str(item["local_interface"]), str(item["neighbor_hostname"])))
    status = "success" if neighbors else "warning"
    return _response(
        status,
        neighbors,
        ["This is derived from Catalyst Center physical topology, not direct device CLI or CDP-table output."],
    )


def command_runner(**kwargs: Any) -> dict[str, Any]:
    """Run one or more read-only show commands through Catalyst Center."""
    api = _api()
    device_id = _resolve_device_id(api, kwargs.get("hostname"), kwargs.get("device_id"))
    commands_value = kwargs.get("commands")
    commands = (
        [item.strip() for item in commands_value.split(",") if item.strip()]
        if isinstance(commands_value, str)
        else list(commands_value or [])
    )
    if not commands:
        raise ValueError("--commands is required for command-runner")
    unsafe = [command for command in commands if not command.lower().startswith("show ")]
    if unsafe:
        raise ValueError(f"Only read-only 'show' commands are allowed: {', '.join(unsafe)}")

    submitted = api.custom_caller.call_api(
        "POST",
        "/dna/intent/api/v1/network-device-poller/cli/read-request",
        json={"commands": commands, "deviceUuids": [device_id]},
    )
    task_id = (submitted or {}).get("response", {}).get("taskId")
    if not task_id:
        return _response("error", submitted, ["Catalyst Center did not return a task ID."])

    deadline = time.time() + 60
    file_id = None
    while time.time() < deadline:
        time.sleep(2)
        task_data = api.task.get_task_by_id(task_id=task_id)
        task = (task_data or {}).get("response", {})
        if task.get("isError"):
            return _response("error", task, ["Review the task progress message."])
        progress = str(task.get("progress", ""))
        try:
            file_id = json.loads(progress).get("fileId")
        except (json.JSONDecodeError, AttributeError):
            match = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", progress)
            file_id = match.group(0) if match else None
        if file_id or task.get("endTime"):
            break
    if not file_id:
        return _response("error", {"task_id": task_id}, ["The command task completed without a file ID."])
    output = api.custom_caller.call_api("GET", f"/dna/intent/api/v1/file/{file_id}")
    return _response(
        "success",
        {"device_id": device_id, "task_id": task_id, "file_id": file_id, "output": output},
        ["Command execution was restricted to read-only show commands."],
    )


def cdp_neighbors(**kwargs: Any) -> dict[str, Any]:
    """Return structured CDP neighbors from direct device command output."""
    result = command_runner(**{**kwargs, "commands": "show cdp neighbors detail"})
    if result["status"] != "success":
        return result
    raw_text = _extract_command_output(result["results"].get("output", []))
    neighbors = _parse_cdp_neighbors(raw_text)
    status = "success" if neighbors else "warning"
    return _response(status, neighbors, ["Use list-neighbors for the controller's topology-derived view."])


def get_templates(**kwargs: Any) -> dict[str, Any]:
    """Resolve configuration template UUIDs to template details."""
    template_ids_value = kwargs.get("template_ids")
    template_ids = (
        [item.strip() for item in template_ids_value.split(",") if item.strip()]
        if isinstance(template_ids_value, str)
        else list(template_ids_value or [])
    )
    if not template_ids:
        raise ValueError("--template-ids is required for get-templates")
    api = _api()
    templates = []
    for template_id in template_ids:
        data = api.configuration_templates.get_template_details(template_id=template_id)
        templates.append(data.get("response", data) if isinstance(data, dict) else data)
    return _response("success", templates, ["Template retrieval is read-only; no deployment was performed."])


COMMANDS: dict[str, Callable[..., dict[str, Any]]] = {
    "list-devices": list_devices, "get-device": get_device, "get-interfaces": get_interfaces,
    "device-detail": device_detail, "redundancy-info": redundancy_info, "get-interface": get_interface,
    "list-sites": list_sites, "list-issues": list_issues, "get-issue": get_issue,
    "device-health": device_health, "site-device-health": site_device_health,
    "client-detail": client_detail, "client-health": client_health,
    "physical-topology": physical_topology, "list-neighbors": list_neighbors,
    "command-runner": command_runner, "cdp-neighbors": cdp_neighbors, "get-templates": get_templates,
    "port-bounce-plan": port_bounce_plan, "port-bounce": port_bounce,
}


def handle_command(command: str, **kwargs: Any) -> dict[str, Any]:
    """Normalize and dispatch a Catalyst Center command."""
    normalized = (command or "").strip().lower().replace("_", "-")
    handler = COMMANDS.get(normalized)
    if not handler:
        return _response("error", [], [f"Supported commands: {', '.join(COMMANDS)}."])
    try:
        return handler(**kwargs)
    except ValueError as exc:
        return _response("error", [], [str(exc)])
    except Exception as exc:
        return _response("error", [], [f"{type(exc).__name__}: {exc}"])


def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Catalyst Center skill handler")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--hostname")
    parser.add_argument("--device-id")
    parser.add_argument("--interface")
    parser.add_argument("--ip")
    parser.add_argument("--reachability")
    parser.add_argument("--name")
    parser.add_argument("--site-id")
    parser.add_argument("--priority")
    parser.add_argument("--device-role")
    parser.add_argument("--issue-id")
    parser.add_argument("--commands")
    parser.add_argument("--template-ids")
    parser.add_argument("--bounce-delay", type=int, default=5)
    parser.add_argument("--confirm-target")
    parser.add_argument("--mac")
    parser.add_argument("--timestamp", type=int)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    options = vars(args)
    command = options.pop("command")
    result = handle_command(command, **options)
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=False))
    return 0 if result["status"] in {"success", "warning"} else 1


if __name__ == "__main__":
    sys.exit(_cli())
