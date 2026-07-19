#!/usr/bin/env python3
"""Generic read-only GitHub handler restricted to a configured owner."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Optional
from urllib.parse import quote

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
API_ROOT = "https://api.github.com"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
REF_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
MAX_LIMIT = 100
DEFAULT_LIMIT = 20
DEFAULT_TIMEOUT = 30
MAX_TEXT_BYTES = 250_000


def _response(status: str, results: Any, next_steps: list[str]) -> dict[str, Any]:
    """Build the standard skill response."""
    return {"status": status, "results": results, "next_steps": next_steps}


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


def _load_configuration() -> tuple[str, Optional[str]]:
    """Load the owner and optional token from the Dexter configuration."""
    _load_environment()
    owner = os.getenv("GITHUB_OWNER", "").strip()
    token = os.getenv("GITHUB_TOKEN", "").strip() or None
    if not NAME_RE.fullmatch(owner):
        raise ValueError("GITHUB_OWNER must be a valid GitHub account or organization name.")
    return owner, token


def _validate_repo(repo: str) -> str:
    """Normalize a repository name while enforcing the allowed owner."""
    allowed_owner, _ = _load_configuration()
    value = (repo or "").strip().removesuffix(".git")
    if value.startswith("https://github.com/"):
        value = value.removeprefix("https://github.com/").strip("/")
    if "/" in value:
        parts = value.split("/")
        if len(parts) != 2 or parts[0].lower() != allowed_owner.lower():
            raise ValueError(f"Only repositories owned by {allowed_owner} are permitted.")
        value = parts[1]
    if not NAME_RE.fullmatch(value):
        raise ValueError(f"Invalid repository name: {repo!r}.")
    return value


def _validate_path(path: Optional[str], *, required: bool = False) -> str:
    """Validate a repository-relative POSIX path."""
    value = (path or "").strip().strip("/")
    if required and not value:
        raise ValueError("--path is required for get-file.")
    pure_path = PurePosixPath(value)
    if ".." in pure_path.parts or any("\x00" in part for part in pure_path.parts):
        raise ValueError("Repository paths cannot contain parent traversal or null characters.")
    return value


def _validate_ref(ref: Optional[str]) -> Optional[str]:
    """Validate an optional Git reference."""
    if not ref:
        return None
    value = ref.strip()
    if not REF_RE.fullmatch(value) or ".." in value or value.startswith("/"):
        raise ValueError(f"Invalid Git reference: {ref!r}.")
    return value


def _validate_limit(limit: int) -> int:
    """Validate a bounded GitHub page size."""
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"--limit must be between 1 and {MAX_LIMIT}.")
    return limit


def _request(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    """Perform one read-only GitHub REST API request."""
    _, token = _load_configuration()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "github-customer-showcase",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = requests.get(
        f"{API_ROOT}{path}", params=params, headers=headers, timeout=DEFAULT_TIMEOUT
    )
    response.raise_for_status()
    return response.json()


def _repo_path(repo: str, suffix: str = "") -> str:
    """Build an API path for an allowed repository."""
    repo_name = _validate_repo(repo)
    owner, _ = _load_configuration()
    return f"/repos/{quote(owner, safe='')}/{quote(repo_name, safe='')}{suffix}"


def list_repos(limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """List repositories owned by the configured showcase account."""
    owner, _ = _load_configuration()
    data = _request(
        f"/users/{quote(owner, safe='')}/repos",
        {"per_page": _validate_limit(limit), "sort": "updated", "direction": "desc", "type": "owner"},
    )
    repos = [
        {
            "name": item.get("name"), "full_name": item.get("full_name"),
            "description": item.get("description"), "html_url": item.get("html_url"),
            "private": item.get("private"), "fork": item.get("fork"),
            "language": item.get("language"), "default_branch": item.get("default_branch"),
            "stars": item.get("stargazers_count"), "forks": item.get("forks_count"),
            "open_issues": item.get("open_issues_count"), "updated_at": item.get("updated_at"),
        }
        for item in data
    ]
    return _response("success", {"owner": owner, "count": len(repos), "repositories": repos}, ["Select a repository and run get-repo or get-readme."])


def get_repo(repo: str) -> dict[str, Any]:
    """Return detailed repository metadata."""
    item = _request(_repo_path(repo))
    fields = {
        key: item.get(key)
        for key in (
            "id", "name", "full_name", "description", "html_url", "private", "fork",
            "created_at", "updated_at", "pushed_at", "homepage", "size", "stargazers_count",
            "watchers_count", "forks_count", "open_issues_count", "default_branch", "topics",
            "visibility", "archived", "disabled", "language", "subscribers_count",
        )
    }
    fields["owner"] = (item.get("owner") or {}).get("login")
    fields["license"] = (item.get("license") or {}).get("spdx_id")
    return _response("success", fields, ["Use get-readme and list-contents to explore repository documentation and code."])


def list_contents(repo: str, path: Optional[str] = None, ref: Optional[str] = None) -> dict[str, Any]:
    """List files and directories at a repository path."""
    clean_path = _validate_path(path)
    suffix = "/contents" + (f"/{quote(clean_path, safe='/')}" if clean_path else "")
    data = _request(_repo_path(repo, suffix), {"ref": _validate_ref(ref)} if ref else None)
    entries = data if isinstance(data, list) else [data]
    results = [
        {key: item.get(key) for key in ("name", "path", "type", "size", "sha", "html_url", "download_url")}
        for item in entries
    ]
    return _response("success", {"path": clean_path or "/", "count": len(results), "entries": results}, ["Use get-file for a text file or list-contents for a directory."])


def _decode_content(item: dict[str, Any]) -> dict[str, Any]:
    """Decode a GitHub Contents API text payload with a bounded response size."""
    if item.get("type") != "file":
        raise ValueError("The requested path is not a file.")
    if item.get("encoding") != "base64":
        raise ValueError("GitHub did not return base64 file content.")
    raw = base64.b64decode(item.get("content", ""), validate=False)
    truncated = len(raw) > MAX_TEXT_BYTES
    selected = raw[:MAX_TEXT_BYTES]
    if b"\x00" in selected:
        raise ValueError("Binary files are not returned by this showcase skill.")
    return {
        "name": item.get("name"), "path": item.get("path"), "sha": item.get("sha"),
        "size": item.get("size"), "html_url": item.get("html_url"),
        "content": selected.decode("utf-8", errors="replace"), "truncated": truncated,
    }


def get_file(repo: str, path: str, ref: Optional[str] = None) -> dict[str, Any]:
    """Read one bounded UTF-8 text file without cloning the repository."""
    clean_path = _validate_path(path, required=True)
    item = _request(_repo_path(repo, f"/contents/{quote(clean_path, safe='/')}"), {"ref": _validate_ref(ref)} if ref else None)
    result = _decode_content(item)
    return _response("warning" if result["truncated"] else "success", result, ["The file was truncated to 250 KB." if result["truncated"] else "The complete text file was returned."])


def get_readme(repo: str, ref: Optional[str] = None) -> dict[str, Any]:
    """Read and decode a repository README."""
    item = _request(_repo_path(repo, "/readme"), {"ref": _validate_ref(ref)} if ref else None)
    result = _decode_content(item)
    return _response("warning" if result["truncated"] else "success", result, ["Use list-contents to continue exploring the repository."])


def _list_endpoint(repo: str, suffix: str, limit: int, params: Optional[dict[str, Any]] = None) -> list[Any]:
    """Return one bounded page from a repository list endpoint."""
    request_params = dict(params or {})
    request_params["per_page"] = _validate_limit(limit)
    data = _request(_repo_path(repo, suffix), request_params)
    return data if isinstance(data, list) else data.get("workflow_runs", data.get("workflows", []))


def list_contributors(repo: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """List repository contributors."""
    data = _list_endpoint(repo, "/contributors", limit)
    results = [{"login": item.get("login"), "contributions": item.get("contributions"), "html_url": item.get("html_url")} for item in data]
    return _response("success", results, ["Use list-commits to inspect recent contribution activity."])


def list_commits(repo: str, limit: int = DEFAULT_LIMIT, ref: Optional[str] = None) -> dict[str, Any]:
    """List recent repository commits."""
    data = _list_endpoint(repo, "/commits", limit, {"sha": _validate_ref(ref)} if ref else None)
    results = [{"sha": item.get("sha"), "html_url": item.get("html_url"), "author": (item.get("author") or {}).get("login"), "commit": item.get("commit")} for item in data]
    return _response("success", results, ["Use get-file with a commit SHA as --ref to read content at that revision."])


def list_branches(repo: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """List repository branches."""
    data = _list_endpoint(repo, "/branches", limit)
    results = [{"name": item.get("name"), "protected": item.get("protected"), "commit_sha": (item.get("commit") or {}).get("sha")} for item in data]
    return _response("success", results, ["Use --ref with content commands to inspect a branch."])


def list_releases(repo: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """List repository releases."""
    data = _list_endpoint(repo, "/releases", limit)
    return _response("success", data, ["Review release tags and published dates for delivery history."])


def list_issues(repo: str, limit: int = DEFAULT_LIMIT, state: str = "open") -> dict[str, Any]:
    """List repository issues while excluding pull requests."""
    if state not in {"open", "closed", "all"}:
        raise ValueError("--state must be open, closed, or all.")
    data = _list_endpoint(repo, "/issues", limit, {"state": state})
    results = [item for item in data if "pull_request" not in item]
    return _response("success", results, ["Use the returned issue URL for a detailed browser view."])


def list_pulls(repo: str, limit: int = DEFAULT_LIMIT, state: str = "open") -> dict[str, Any]:
    """List repository pull requests."""
    if state not in {"open", "closed", "all"}:
        raise ValueError("--state must be open, closed, or all.")
    data = _list_endpoint(repo, "/pulls", limit, {"state": state})
    return _response("success", data, ["Use the returned pull request URL to inspect checks and discussion."])


def list_labels(repo: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """List repository labels."""
    data = _list_endpoint(repo, "/labels", limit)
    results = [{key: item.get(key) for key in ("name", "color", "description")} for item in data]
    return _response("success", results, ["Use label names to explain repository triage conventions."])


def list_workflows(repo: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """List GitHub Actions workflows."""
    data = _list_endpoint(repo, "/actions/workflows", limit)
    return _response("success", data, ["Use list-runs to inspect recent automation results."])


def list_runs(repo: str, limit: int = DEFAULT_LIMIT, status: Optional[str] = None, ref: Optional[str] = None) -> dict[str, Any]:
    """List GitHub Actions workflow runs."""
    params: dict[str, Any] = {}
    if status:
        params["status"] = status
    if ref:
        params["branch"] = _validate_ref(ref)
    data = _list_endpoint(repo, "/actions/runs", limit, params)
    return _response("success", data, ["Review conclusion, event, branch, actor, and URL for each run."])


def search_code(repo: str, query: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Search code within one allowed repository."""
    repo_name = _validate_repo(repo)
    owner, _ = _load_configuration()
    normalized_query = (query or "").strip()
    if not normalized_query or any(char in normalized_query for char in "\r\n\x00"):
        raise ValueError("--query must be a non-empty single-line code search.")
    data = _request("/search/code", {"q": f"{normalized_query} repo:{owner}/{repo_name}", "per_page": _validate_limit(limit)})
    return _response("success", {"total_count": data.get("total_count", 0), "items": data.get("items", [])}, ["Use get-file with an item's path to read matching source."])


COMMANDS = {
    "list-repos": list_repos, "get-repo": get_repo, "list-contents": list_contents,
    "get-file": get_file, "get-readme": get_readme, "list-contributors": list_contributors,
    "list-commits": list_commits, "list-branches": list_branches, "list-releases": list_releases,
    "list-issues": list_issues, "list-pulls": list_pulls, "list-labels": list_labels,
    "list-workflows": list_workflows, "list-runs": list_runs, "search-code": search_code,
}


def handle_command(command: str, **kwargs: Any) -> dict[str, Any]:
    """Normalize and dispatch a read-only GitHub showcase command."""
    normalized = (command or "").strip().lower().replace("_", "-")
    handler = COMMANDS.get(normalized)
    if handler is None:
        return _response("error", [], [f"Supported commands: {', '.join(COMMANDS)}."])
    try:
        if normalized == "list-repos":
            return handler(limit=kwargs.get("limit", DEFAULT_LIMIT))
        if normalized == "get-repo":
            return handler(repo=kwargs.get("repo"))
        if normalized in {"list-contents", "get-file", "get-readme"}:
            call_kwargs = {"repo": kwargs.get("repo"), "ref": kwargs.get("ref")}
            if normalized != "get-readme":
                call_kwargs["path"] = kwargs.get("path")
            return handler(**call_kwargs)
        if normalized == "search-code":
            return handler(repo=kwargs.get("repo"), query=kwargs.get("query"), limit=kwargs.get("limit", DEFAULT_LIMIT))
        if normalized in {"list-issues", "list-pulls"}:
            return handler(repo=kwargs.get("repo"), limit=kwargs.get("limit", DEFAULT_LIMIT), state=kwargs.get("state", "open"))
        if normalized == "list-commits":
            return handler(repo=kwargs.get("repo"), limit=kwargs.get("limit", DEFAULT_LIMIT), ref=kwargs.get("ref"))
        if normalized == "list-runs":
            return handler(repo=kwargs.get("repo"), limit=kwargs.get("limit", DEFAULT_LIMIT), status=kwargs.get("status"), ref=kwargs.get("ref"))
        return handler(repo=kwargs.get("repo"), limit=kwargs.get("limit", DEFAULT_LIMIT))
    except ValueError as exc:
        return _response("error", [], [str(exc)])
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "unknown"
        message = "GitHub API request failed."
        if code == 404:
            message = "Repository or resource was not found, or the configured token cannot access it."
        elif code == 403:
            message = "GitHub API access was forbidden or the API rate limit was reached."
        elif code == 401:
            message = "The optional GitHub token is invalid or expired."
        return _response("error", [], [f"{message} HTTP {code}."])
    except requests.RequestException as exc:
        return _response("error", [], [f"GitHub API request failed: {type(exc).__name__}."])
    except (KeyError, TypeError, base64.binascii.Error) as exc:
        return _response("error", [], [f"Invalid GitHub API response: {type(exc).__name__}."])


def _cli(argv: Optional[list[str]] = None) -> int:
    """Run the GitHub showcase CLI."""
    parser = argparse.ArgumentParser(description="Read-only GitHub customer showcase handler")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--repo", help="Repository name or configured-owner/name")
    parser.add_argument("--path", help="Repository-relative file or directory path")
    parser.add_argument("--ref", help="Branch, tag, or commit SHA")
    parser.add_argument("--query", help="Code search terms")
    parser.add_argument("--state", choices=["open", "closed", "all"], default="open", help="Issue or pull request state (default: open)")
    parser.add_argument("--status", help="GitHub Actions workflow run status")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Maximum results (default: 20; maximum: 100)")
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
