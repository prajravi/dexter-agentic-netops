#!/usr/bin/env python3
"""Deterministically import GitHub-hosted CSV DNS mappings into ServiceNow CMDB."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import ipaddress
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
SKILLS_ROOT = SCRIPT_DIR.parents[1]
DNS_HANDLER_PATH = SKILLS_ROOT / "servicenow-dns" / "scripts" / "handler.py"
GITHUB_HANDLER_PATH = SKILLS_ROOT / "github-explorer" / "scripts" / "handler.py"
CSV_COLUMNS = {"DNS", "A", "AAAA"}
MISSING_VALUES = {"", "n/a", "na", "none", "null", "-"}
LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


def _response(status: str, results: Any, next_steps: list[str]) -> dict[str, Any]:
    return {"status": status, "results": results, "next_steps": next_steps}


def _load_module(name: str, path: Path) -> ModuleType:
    if not path.is_file():
        raise ValueError(f"Required skill handler was not found: {path.parent.name}.")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Required skill handler could not be loaded: {path.parent.name}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _github_handler_path() -> Path:
    if GITHUB_HANDLER_PATH.is_file():
        return GITHUB_HANDLER_PATH
    raise ValueError("The bundled GitHub handler was not found.")


def _github_file(repo: str, path: str, ref: Optional[str] = None) -> dict[str, Any]:
    github = _load_module("dexter_github_csv_reader", _github_handler_path())
    result = github.handle_command("get-file", repo=repo, path=path, ref=ref)
    if result.get("status") not in {"success", "warning"}:
        steps = result.get("next_steps") or ["GitHub file retrieval failed."]
        raise ValueError(str(steps[0]))
    payload = result.get("results")
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
        raise ValueError("GitHub returned no readable CSV content.")
    if payload.get("truncated"):
        raise ValueError("The GitHub CSV exceeds the 250 KB read limit and cannot be imported safely.")
    if not str(payload.get("path", "")).lower().endswith(".csv"):
        raise ValueError("The GitHub source path must identify a .csv file.")
    return payload


def _validate_domain(domain: str) -> str:
    normalized = (domain or "").strip().rstrip(".").lower()
    labels = normalized.split(".")
    if len(labels) < 2 or len(normalized) > 253 or any(not LABEL_RE.fullmatch(label) for label in labels):
        raise ValueError("--domain must be a valid DNS domain such as example.com.")
    return normalized


def _validate_hostname(value: str) -> str:
    normalized = (value or "").strip().rstrip(".").lower()
    if "." in normalized:
        normalized = normalized.split(".")[0]
    if not LABEL_RE.fullmatch(normalized):
        raise ValueError("DNS must contain a valid host label.")
    return normalized


def _normalize_ip(value: str, version: int) -> str:
    try:
        address = ipaddress.ip_address((value or "").strip())
    except ValueError as exc:
        raise ValueError(f"Invalid IPv{version} address: {value!r}.") from exc
    if address.version != version:
        raise ValueError(f"Expected IPv{version}, received IPv{address.version}: {value!r}.")
    return str(address)


def _selected_types(record_type: str) -> tuple[str, ...]:
    normalized = (record_type or "A").strip().upper()
    if normalized not in {"A", "AAAA", "BOTH"}:
        raise ValueError("--record-type must be A, AAAA, or both.")
    return ("A", "AAAA") if normalized == "BOTH" else (normalized,)


def _build_plan(
    content: str,
    domain: str,
    record_type: str = "A",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int]:
    normalized_domain = _validate_domain(domain)
    selected = _selected_types(record_type)
    try:
        reader = csv.DictReader(io.StringIO(content, newline=""))
        headers = {str(name or "").strip().upper() for name in (reader.fieldnames or [])}
    except csv.Error as exc:
        raise ValueError(f"CSV parsing failed: {exc}.") from exc
    missing = CSV_COLUMNS - headers
    if missing:
        raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}.")

    plan: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_fqdns: set[str] = set()
    seen_ips: set[str] = set()
    total_rows = 0

    try:
        for row_number, raw in enumerate(reader, start=2):
            total_rows += 1
            row = {str(key or "").strip().upper(): str(value or "").strip() for key, value in raw.items()}
            hostname_value = row.get("DNS", "")
            selected_values = [row.get(kind, "") for kind in selected]
            if not hostname_value and all(value.lower() in MISSING_VALUES for value in selected_values):
                skipped.append({"row": row_number, "reason": "No DNS label or selected address."})
                continue
            if not hostname_value:
                errors.append({"row": row_number, "error": "Address is present but DNS label is missing."})
                continue
            try:
                hostname = _validate_hostname(hostname_value)
                fqdn = f"{hostname}.{normalized_domain}"
                candidates = []
                for kind in selected:
                    raw_ip = row.get(kind, "")
                    if raw_ip.lower() in MISSING_VALUES:
                        continue
                    candidates.append((kind, _normalize_ip(raw_ip, 4 if kind == "A" else 6)))
                if not candidates:
                    skipped.append({"row": row_number, "dns": hostname_value, "reason": "No selected address value."})
                    continue
                if len(candidates) > 1:
                    raise ValueError(
                        "A and AAAA for one FQDN cannot both be modeled by the current ServiceNow DNS skill; select one record type."
                    )
                kind, ip = candidates[0]
                if fqdn in seen_fqdns:
                    raise ValueError(f"Duplicate generated FQDN: {fqdn}.")
                if ip in seen_ips:
                    raise ValueError(f"Duplicate selected IP address: {ip}.")
                seen_fqdns.add(fqdn)
                seen_ips.add(ip)
                plan.append(
                    {
                        "row": row_number,
                        "record_type": kind,
                        "hostname": hostname,
                        "domain": normalized_domain,
                        "fqdn": fqdn,
                        "ip": ip,
                    }
                )
            except ValueError as exc:
                errors.append({"row": row_number, "dns": hostname_value, "error": str(exc)})
    except csv.Error as exc:
        errors.append({"row": total_rows + 2, "error": f"CSV parsing failed: {exc}."})

    plan.sort(key=lambda item: (item["fqdn"].casefold(), item["record_type"]))
    return plan, errors, skipped, total_rows


def _source(repo: str, path: str, ref: Optional[str], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "repository": repo,
        "path": payload.get("path") or path,
        "requested_ref": ref,
        "revision": payload.get("sha"),
        "url": payload.get("html_url"),
    }


def _prepare(**kwargs: Any) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int]:
    repo = kwargs.get("repo")
    path = kwargs.get("path")
    if not repo or not path:
        raise ValueError("--repo and --path are required.")
    payload = _github_file(repo, path, kwargs.get("ref"))
    plan, errors, skipped, total_rows = _build_plan(
        payload["content"], kwargs.get("domain"), kwargs.get("record_type", "A")
    )
    return _source(repo, path, kwargs.get("ref"), payload), plan, errors, skipped, total_rows


def preview_github_csv_dns(**kwargs: Any) -> dict[str, Any]:
    source, plan, errors, skipped, total_rows = _prepare(**kwargs)
    results = {
        "source": source,
        "domain": _validate_domain(kwargs.get("domain")),
        "record_type": (kwargs.get("record_type") or "A").upper(),
        "total_rows": total_rows,
        "planned_count": len(plan),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "created": False,
        "plan": plan,
        "skipped": skipped,
        "errors": errors,
    }
    if errors:
        return _response("error", results, ["Correct every CSV validation error before importing anything."])
    return _response(
        "warning",
        results,
        ["Review the deterministic plan, then run import-github-csv-dns with --confirm."],
    )


def import_github_csv_dns(**kwargs: Any) -> dict[str, Any]:
    if not kwargs.get("confirm"):
        return preview_github_csv_dns(**kwargs)
    source, plan, errors, skipped, total_rows = _prepare(**kwargs)
    if errors:
        return _response(
            "error",
            {
                "source": source,
                "total_rows": total_rows,
                "planned_count": len(plan),
                "skipped_count": len(skipped),
                "error_count": len(errors),
                "created": False,
                "plan": plan,
                "skipped": skipped,
                "errors": errors,
            },
            ["No ServiceNow mutation occurred; correct every CSV validation error first."],
        )

    dns = _load_module("dexter_servicenow_csv_dns_writer", DNS_HANDLER_PATH)
    outcomes: list[dict[str, Any]] = []
    for item in plan:
        outcome = dns.handle_command(
            "create-dns",
            hostname=item["hostname"],
            domain=item["domain"],
            ip=item["ip"],
            confirm=True,
        )
        outcomes.append({"fqdn": item["fqdn"], "ip": item["ip"], **outcome})
        if outcome.get("status") != "success":
            return _response(
                "error",
                {
                    "source": source,
                    "planned_count": len(plan),
                    "processed_count": len(outcomes),
                    "skipped_count": len(skipped),
                    "outcomes": outcomes,
                },
                ["Import stopped on the first ServiceNow error; an approved rerun is idempotent."],
            )
    created_count = sum(1 for item in outcomes if item.get("results", {}).get("created"))
    return _response(
        "success",
        {
            "source": source,
            "total_rows": total_rows,
            "planned_count": len(plan),
            "processed_count": len(outcomes),
            "created_count": created_count,
            "unchanged_count": len(outcomes) - created_count,
            "skipped_count": len(skipped),
            "error_count": 0,
            "outcomes": outcomes,
        },
        ["Run verify-github-csv-dns with the same source options to verify every CMDB mapping."],
    )


def delete_github_csv_dns(**kwargs: Any) -> dict[str, Any]:
    """Preview or delete the exact CMDB mappings represented by a GitHub CSV."""
    source, plan, errors, skipped, total_rows = _prepare(**kwargs)
    base_results = {
        "source": source,
        "total_rows": total_rows,
        "planned_count": len(plan),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "plan": plan,
        "skipped": skipped,
        "errors": errors,
    }
    if errors:
        return _response("error", base_results, ["No ServiceNow deletion occurred; correct every CSV validation error first."])
    if not kwargs.get("confirm"):
        return _response("warning", {**base_results, "deleted": False}, ["Review the deterministic deletion plan, then rerun with --confirm."])

    dns = _load_module("dexter_servicenow_csv_dns_deleter", DNS_HANDLER_PATH)
    outcomes: list[dict[str, Any]] = []
    for item in plan:
        outcome = dns.handle_command("delete-dns", hostname=item["hostname"], domain=item["domain"], ip=item["ip"], confirm=True)
        outcomes.append({"fqdn": item["fqdn"], "ip": item["ip"], **outcome})
        if outcome.get("status") != "success":
            return _response("error", {"source": source, "planned_count": len(plan), "processed_count": len(outcomes), "outcomes": outcomes}, ["Deletion stopped on the first ServiceNow error; review outcomes before retrying."])
    deleted_count = sum(1 for item in outcomes if item.get("results", {}).get("deleted"))
    return _response("success", {"source": source, "total_rows": total_rows, "planned_count": len(plan), "processed_count": len(outcomes), "deleted_count": deleted_count, "unchanged_count": len(outcomes) - deleted_count, "skipped_count": len(skipped), "error_count": 0, "outcomes": outcomes}, ["Run verify-github-csv-dns to confirm the mappings no longer exist."])


def verify_github_csv_dns(**kwargs: Any) -> dict[str, Any]:
    source, plan, errors, skipped, total_rows = _prepare(**kwargs)
    if errors:
        return _response(
            "error",
            {"source": source, "total_rows": total_rows, "errors": errors, "plan": plan},
            ["Verification stopped because the CSV source is invalid."],
        )
    dns = _load_module("dexter_servicenow_csv_dns_verifier", DNS_HANDLER_PATH)
    outcomes = []
    for item in plan:
        outcome = dns.handle_command(
            "verify-dns", hostname=item["hostname"], domain=item["domain"], ip=item["ip"]
        )
        outcomes.append({"fqdn": item["fqdn"], "ip": item["ip"], **outcome})
    verified_count = sum(1 for item in outcomes if item.get("results", {}).get("exists"))
    status = "success" if verified_count == len(plan) else "warning"
    return _response(
        status,
        {
            "source": source,
            "planned_count": len(plan),
            "verified_count": verified_count,
            "missing_count": len(plan) - verified_count,
            "skipped_count": len(skipped),
            "outcomes": outcomes,
        },
        ["All CSV mappings exist in ServiceNow." if status == "success" else "Import or repair the missing mappings."],
    )


def list_dns(**kwargs: Any) -> dict[str, Any]:
    dns = _load_module("dexter_servicenow_csv_dns_lister", DNS_HANDLER_PATH)
    return dns.handle_command("list-dns", fqdn=kwargs.get("fqdn"), ip=kwargs.get("ip"), limit=kwargs.get("limit", 100))


COMMANDS = {
    "preview-github-csv-dns": preview_github_csv_dns,
    "import-github-csv-dns": import_github_csv_dns,
    "delete-github-csv-dns": delete_github_csv_dns,
    "verify-github-csv-dns": verify_github_csv_dns,
    "list-dns": list_dns,
}


def handle_command(command: str, **kwargs: Any) -> dict[str, Any]:
    normalized = (command or "").strip().lower().replace("_", "-")
    handler = COMMANDS.get(normalized)
    if handler is None:
        return _response("error", [], [f"Supported commands: {', '.join(COMMANDS)}."])
    try:
        return handler(**kwargs)
    except ValueError as exc:
        return _response("error", [], [str(exc)])
    except Exception as exc:
        return _response("error", [], [f"CSV DNS workflow failed: {type(exc).__name__}."])


def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic GitHub CSV to ServiceNow DNS importer")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--repo")
    parser.add_argument("--path")
    parser.add_argument("--ref")
    parser.add_argument("--domain")
    parser.add_argument("--record-type", choices=["A", "AAAA", "both"], default="A")
    parser.add_argument("--fqdn")
    parser.add_argument("--ip")
    parser.add_argument("--limit", type=int, default=100)
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
