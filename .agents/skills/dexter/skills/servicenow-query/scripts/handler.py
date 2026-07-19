#!/usr/bin/env python3
"""Generic, read-only ServiceNow development-instance Table API handler."""

from __future__ import annotations

import argparse
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
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
SYS_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
MAX_LIMIT = 100
DEFAULT_LIMIT = 20
DEFAULT_MAX_RECORDS = 10_000
DEFAULT_TIMEOUT = 30
RECORD_PRESETS: dict[str, dict[str, str]] = {
    "incidents": {
        "table": "incident",
        "fields": "sys_id,number,short_description,state,priority,assigned_to,caller_id,opened_at,active",
        "order_by": "number",
        "group_by": "state",
        "description": "Service incidents",
    },
    "changes": {
        "table": "change_request",
        "fields": "sys_id,number,short_description,state,risk,assigned_to,start_date,end_date,active",
        "order_by": "number",
        "group_by": "state",
        "description": "Change requests",
    },
    "problems": {
        "table": "problem",
        "fields": "sys_id,number,short_description,state,priority,assigned_to,active",
        "order_by": "number",
        "group_by": "state",
        "description": "Problem records",
    },
    "tasks": {
        "table": "task",
        "fields": "sys_id,number,short_description,sys_class_name,state,priority,assigned_to,active",
        "order_by": "number",
        "group_by": "sys_class_name",
        "description": "All task-derived records, including incidents, changes, and problems",
    },
    "network-gear": {
        "table": "cmdb_ci_netgear",
        "fields": "sys_id,name,ip_address,serial_number,model_number,manufacturer,mac_address,firmware_version,install_status,operational_status",
        "order_by": "name",
        "group_by": "operational_status",
        "description": "Network Gear configuration items",
    },
    "hardware-assets": {
        "table": "alm_hardware",
        "fields": "sys_id,display_name,asset_tag,serial_number,model,model_category,manufacturer,install_status,substatus,ci",
        "order_by": "asset_tag",
        "group_by": "install_status",
        "description": "Hardware asset records",
    },
}


def _response(status: str, results: Any, next_steps: list[str]) -> dict[str, Any]:
    """Build the standard skill response."""
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


def _validate_identifier(value: str, label: str, pattern: re.Pattern[str] = IDENTIFIER_RE) -> str:
    """Validate a ServiceNow table or field identifier."""
    normalized = (value or "").strip()
    if not pattern.fullmatch(normalized):
        raise ValueError(f"Invalid {label}: {value!r}.")
    return normalized


def _validate_fields(fields: Optional[str]) -> Optional[str]:
    """Validate and normalize a comma-separated field list."""
    if not fields:
        return None
    values = [_validate_identifier(item.strip(), "field", FIELD_RE) for item in fields.split(",")]
    return ",".join(values)


def _validate_query(query: Optional[str]) -> Optional[str]:
    """Reject encoded queries capable of invoking server-side JavaScript."""
    if query is None:
        return None
    normalized = query.strip()
    lowered = normalized.lower()
    if "\x00" in normalized or "\r" in normalized or "\n" in normalized:
        raise ValueError("Encoded queries cannot contain control characters.")
    if "javascript:" in lowered or "javascript%3a" in lowered:
        raise ValueError("Script-bearing ServiceNow encoded queries are not permitted.")
    return normalized or None


def _request(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    """Perform one authenticated, read-only GET request."""
    instance, username, password = _load_configuration()
    response = requests.get(
        f"{instance}{path}",
        params=params,
        auth=(username, password),
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def query_table(
    table: str,
    query: Optional[str] = None,
    fields: Optional[str] = None,
    order_by: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    display_values: bool = False,
) -> dict[str, Any]:
    """Query any readable table in the configured development instance."""
    table = _validate_identifier(table, "table")
    query = _validate_query(query)
    fields = _validate_fields(fields)
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"--limit must be between 1 and {MAX_LIMIT}.")
    if offset < 0:
        raise ValueError("--offset must be zero or greater.")
    params: dict[str, Any] = {
        "sysparm_limit": limit,
        "sysparm_offset": offset,
        "sysparm_exclude_reference_link": "true",
        "sysparm_display_value": "true" if display_values else "false",
    }
    if query:
        params["sysparm_query"] = query
    if fields:
        params["sysparm_fields"] = fields
    if order_by:
        params["sysparm_query"] = "^".join(
            part for part in [params.get("sysparm_query"), f"ORDERBY{_validate_identifier(order_by, 'order-by field', FIELD_RE)}"] if part
        )
    payload = _request(f"/api/now/table/{table}", params)
    records = payload.get("result", []) if isinstance(payload, dict) else []
    return _response(
        "success",
        {"table": table, "count": len(records), "limit": limit, "offset": offset, "records": records},
        ["Increase --offset to retrieve the next page." if len(records) == limit else "The returned page is complete."],
    )


def list_all_records(
    table: str,
    query: Optional[str] = None,
    fields: Optional[str] = None,
    order_by: Optional[str] = None,
    page_size: int = MAX_LIMIT,
    max_records: int = DEFAULT_MAX_RECORDS,
    display_values: bool = False,
) -> dict[str, Any]:
    """Retrieve matching records across all pages, up to a protective maximum."""
    if not 1 <= page_size <= MAX_LIMIT:
        raise ValueError(f"--limit must be between 1 and {MAX_LIMIT}.")
    if max_records < 1:
        raise ValueError("--max-records must be one or greater.")

    records: list[dict[str, Any]] = []
    offset = 0
    complete = False
    stable_order = order_by or "sys_id"

    while len(records) < max_records:
        current_limit = min(page_size, max_records - len(records))
        page = query_table(
            table=table,
            query=query,
            fields=fields,
            order_by=stable_order,
            limit=current_limit,
            offset=offset,
            display_values=display_values,
        )
        page_records = page["results"]["records"]
        records.extend(page_records)
        offset += len(page_records)
        if len(page_records) < current_limit:
            complete = True
            break

    status = "success" if complete else "warning"
    next_steps = (
        ["All matching records were returned."]
        if complete
        else [
            f"Stopped at --max-records={max_records}; increase the cap to continue.",
            f"Use query-table with --offset {offset} to retrieve the next page.",
        ]
    )
    return _response(
        status,
        {
            "table": _validate_identifier(table, "table"),
            "count": len(records),
            "complete": complete,
            "page_size": page_size,
            "max_records": max_records,
            "order_by": stable_order,
            "records": records,
            "continuation_offset": None if complete else offset,
        },
        next_steps,
    )


def describe_record_types() -> dict[str, Any]:
    """Return deterministic record-type metadata for command selection."""
    record_types = [
        {"record_type": name, **preset}
        for name, preset in RECORD_PRESETS.items()
    ]
    return _response(
        "success",
        {"record_types": record_types, "count": len(record_types)},
        ["Use list-records --record-type <record_type> without choosing a table or fields."],
    )


def _preset(record_type: str) -> dict[str, str]:
    """Resolve one exact, documented record type."""
    normalized = (record_type or "").strip().lower().replace("_", "-")
    preset = RECORD_PRESETS.get(normalized)
    if preset is None:
        raise ValueError(
            f"--record-type must be one of: {', '.join(RECORD_PRESETS)}."
        )
    return preset


def list_records(
    record_type: str,
    query: Optional[str] = None,
    max_records: int = DEFAULT_MAX_RECORDS,
    display_values: bool = True,
) -> dict[str, Any]:
    """List a common ServiceNow record type without table or field inference."""
    preset = _preset(record_type)
    result = list_all_records(
        table=preset["table"],
        query=query,
        fields=preset["fields"],
        order_by=preset["order_by"],
        page_size=MAX_LIMIT,
        max_records=max_records,
        display_values=display_values,
    )
    result["results"]["record_type"] = record_type.strip().lower().replace("_", "-")
    return result


def summarize_records(
    record_type: str,
    query: Optional[str] = None,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> dict[str, Any]:
    """Summarize a common record type using its deterministic grouping field."""
    preset = _preset(record_type)
    result = summarize_table(
        table=preset["table"],
        group_by=preset["group_by"],
        query=query,
        page_size=MAX_LIMIT,
        max_records=max_records,
        display_values=True,
    )
    result["results"]["record_type"] = record_type.strip().lower().replace("_", "-")
    return result


def summarize_table(
    table: str,
    group_by: str,
    query: Optional[str] = None,
    page_size: int = MAX_LIMIT,
    max_records: int = DEFAULT_MAX_RECORDS,
    display_values: bool = True,
) -> dict[str, Any]:
    """Count matching records by one field using safe automatic pagination."""
    group_by = _validate_identifier(group_by, "group-by field", FIELD_RE)
    result = list_all_records(
        table=table,
        query=query,
        fields=group_by,
        page_size=page_size,
        max_records=max_records,
        display_values=display_values,
    )
    counts: dict[str, int] = {}
    for record in result["results"]["records"]:
        value = str(record.get(group_by) or "(blank)")
        counts[value] = counts.get(value, 0) + 1
    groups = [
        {"value": value, "count": count}
        for value, count in sorted(counts.items(), key=lambda item: item[0].casefold())
    ]
    return _response(
        result["status"],
        {
            "table": result["results"]["table"],
            "group_by": group_by,
            "scanned_count": result["results"]["count"],
            "complete": result["results"]["complete"],
            "groups": groups,
        },
        result["next_steps"],
    )


def get_record(
    table: str,
    sys_id: str,
    fields: Optional[str] = None,
    display_values: bool = False,
) -> dict[str, Any]:
    """Retrieve one record by table and sys_id."""
    table = _validate_identifier(table, "table")
    if not SYS_ID_RE.fullmatch((sys_id or "").strip()):
        raise ValueError("--sys-id must be a 32-character hexadecimal ServiceNow sys_id.")
    params: dict[str, Any] = {
        "sysparm_exclude_reference_link": "true",
        "sysparm_display_value": "true" if display_values else "false",
    }
    normalized_fields = _validate_fields(fields)
    if normalized_fields:
        params["sysparm_fields"] = normalized_fields
    payload = _request(f"/api/now/table/{table}/{sys_id.lower()}", params)
    record = payload.get("result", {}) if isinstance(payload, dict) else {}
    return _response("success", {"table": table, "record": record}, ["Use query-table to inspect related records."])


def count_records(table: str, query: Optional[str] = None) -> dict[str, Any]:
    """Count records matching an optional encoded query via the Aggregate API."""
    table = _validate_identifier(table, "table")
    params: dict[str, Any] = {"sysparm_count": "true"}
    normalized_query = _validate_query(query)
    if normalized_query:
        params["sysparm_query"] = normalized_query
    payload = _request(f"/api/now/stats/{table}", params)
    result = payload.get("result", {}) if isinstance(payload, dict) else {}
    return _response("success", {"table": table, "aggregate": result}, ["Use query-table with explicit fields to inspect matching records."])


def handle_command(command: str, **kwargs: Any) -> dict[str, Any]:
    """Normalize and dispatch a generic read-only ServiceNow command."""
    normalized = (command or "").strip().lower().replace("_", "-")
    handlers = {
        "describe-record-types": describe_record_types,
        "list-records": list_records,
        "summarize-records": summarize_records,
        "query-table": query_table,
        "list-all": list_all_records,
        "summarize-table": summarize_table,
        "get-record": get_record,
        "count-records": count_records,
    }
    handler = handlers.get(normalized)
    if handler is None:
        return _response("error", [], [f"Supported commands: {', '.join(handlers)}."])
    try:
        if normalized == "describe-record-types":
            return handler()
        if normalized == "list-records":
            return handler(
                record_type=kwargs.get("record_type"), query=kwargs.get("query"),
                max_records=kwargs.get("max_records", DEFAULT_MAX_RECORDS),
                display_values=kwargs.get("display_values", True),
            )
        if normalized == "summarize-records":
            return handler(
                record_type=kwargs.get("record_type"), query=kwargs.get("query"),
                max_records=kwargs.get("max_records", DEFAULT_MAX_RECORDS),
            )
        if normalized == "query-table":
            return handler(
                table=kwargs.get("table"), query=kwargs.get("query"), fields=kwargs.get("fields"),
                order_by=kwargs.get("order_by"), limit=kwargs.get("limit", DEFAULT_LIMIT),
                offset=kwargs.get("offset", 0), display_values=kwargs.get("display_values", False),
            )
        if normalized == "list-all":
            return handler(
                table=kwargs.get("table"), query=kwargs.get("query"), fields=kwargs.get("fields"),
                order_by=kwargs.get("order_by"), page_size=kwargs.get("limit", MAX_LIMIT),
                max_records=kwargs.get("max_records", DEFAULT_MAX_RECORDS),
                display_values=kwargs.get("display_values", False),
            )
        if normalized == "summarize-table":
            return handler(
                table=kwargs.get("table"), group_by=kwargs.get("group_by"),
                query=kwargs.get("query"), page_size=kwargs.get("limit", MAX_LIMIT),
                max_records=kwargs.get("max_records", DEFAULT_MAX_RECORDS),
                display_values=kwargs.get("display_values", True),
            )
        if normalized == "get-record":
            return handler(
                table=kwargs.get("table"), sys_id=kwargs.get("sys_id"), fields=kwargs.get("fields"),
                display_values=kwargs.get("display_values", False),
            )
        return handler(table=kwargs.get("table"), query=kwargs.get("query"))
    except ValueError as exc:
        return _response("error", [], [str(exc)])
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        return _response("error", [], [f"ServiceNow development API returned HTTP {status_code}."])
    except requests.RequestException as exc:
        return _response("error", [], [f"ServiceNow development API request failed: {type(exc).__name__}."])
    except (KeyError, TypeError) as exc:
        return _response("error", [], [f"Invalid ServiceNow development response: {type(exc).__name__}."])


def _cli(argv: Optional[list[str]] = None) -> int:
    """Run the generic ServiceNow development CLI."""
    parser = argparse.ArgumentParser(description="Read-only ServiceNow development Table API handler")
    parser.add_argument(
        "command",
        choices=[
            "describe-record-types", "list-records", "summarize-records",
            "query-table", "list-all", "summarize-table", "get-record", "count-records",
        ],
    )
    parser.add_argument("--table", help="ServiceNow table name for generic table commands")
    parser.add_argument("--query", help="ServiceNow encoded query")
    parser.add_argument("--fields", help="Comma-separated fields to return")
    parser.add_argument("--order-by", dest="order_by", help="Field used for ascending ordering")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Records per page (default: 20; maximum: 100)")
    parser.add_argument("--offset", type=int, default=0, help="Zero-based result offset (default: 0)")
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS, help="Protective cap for bulk commands")
    parser.add_argument("--group-by", dest="group_by", help="Field used by summarize-table")
    parser.add_argument("--record-type", dest="record_type", help="Documented preset used by list-records and summarize-records")
    parser.add_argument("--sys-id", dest="sys_id", help="32-character record sys_id")
    parser.add_argument("--display-values", action="store_true", help="Return display values")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args(argv)
    command_args = vars(args).copy()
    command = command_args.pop("command")
    pretty = command_args.pop("pretty")
    result = handle_command(command, **command_args)
    print(json.dumps(result, indent=2 if pretty else None, sort_keys=False))
    return 0 if result.get("status") in {"success", "warning"} else 1


if __name__ == "__main__":
    sys.exit(_cli())
