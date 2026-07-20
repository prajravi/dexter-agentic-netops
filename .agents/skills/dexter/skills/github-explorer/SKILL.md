---
name: github-explorer
description: Explores repositories belonging to the configured GitHub owner through a generic read-only API workflow. Use when users ask for repository metadata, files, README content, contributors, commits, branches, releases, issues, pull requests, labels, workflows, runs, or code search.
---

# GitHub Explorer

Use the GitHub REST API to inspect public or token-accessible repositories for `GITHUB_OWNER`. Return consistent JSON without cloning or executing repository code.

## Safety boundaries

- Restrict every operation to the owner configured by `GITHUB_OWNER`.
- Use only read-only GitHub REST API `GET` requests.
- Never create, edit, merge, close, delete, dispatch, rerun, or modify GitHub resources.
- Never clone repositories or execute retrieved code.
- Read authentication only from the Dexter environment; `GITHUB_TOKEN` is optional for public repositories.
- Never expose a token or include it in command arguments.

## Runtime requirements

Use the GitHub owner and optional token configured through the variables documented in `.env.example`.

## Commands

Run from the repository root:

```bash
./scripts/dexter github <command> [options] --pretty
```

| Command | Required options | Purpose |
| --- | --- | --- |
| `list-repos` | — | List repositories owned by the configured owner. |
| `get-repo` | `--repo` | Return repository metadata and statistics. |
| `list-contents` | `--repo` | List a directory; optionally use `--path` and `--ref`. |
| `get-file` | `--repo`, `--path` | Read bounded text content; optionally use `--ref`. |
| `get-readme` | `--repo` | Read the README; optionally use `--ref`. |
| `list-contributors` | `--repo` | List contributors. |
| `list-commits` | `--repo` | List recent commits; optionally use `--ref`. |
| `list-branches` | `--repo` | List branches and protection status. |
| `list-releases` | `--repo` | List releases. |
| `list-issues` | `--repo` | List issues; optionally use `--state`. Pull requests are excluded. |
| `list-pulls` | `--repo` | List pull requests; optionally use `--state`. |
| `list-labels` | `--repo` | List labels. |
| `list-workflows` | `--repo` | List GitHub Actions workflows. |
| `list-runs` | `--repo` | List workflow runs; optionally use `--status` and `--ref`. |
| `search-code` | `--repo`, `--query` | Search code within one allowed repository. Authentication may be required. |

Accept a repository name or `configured-owner/name` for `--repo`; reject every other owner. Bound `--limit` to 1–100. Treat `--path` as repository-relative and reject traversal.

## Recommended workflow

1. Run `list-repos` and select a repository.
2. Run `get-repo`, `get-readme`, and `list-contents` to explain its metadata and structure.
3. Run collaboration or automation commands only when relevant.
4. Use `search-code` or `get-file` for focused inspection. Never execute fetched code.

## Output contract

Every command prints JSON containing `status`, `results`, and `next_steps`. `success` and `warning` use exit code `0`; `error` uses exit code `1`.
