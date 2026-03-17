# AGENTS

Project guide for contributors and agents working in this repo.

## Project Summary

Discorsair is a Python 3.13 CLI tool for automating Discourse browsing and stream analysis.
Request flow uses `curl_cffi` by default and falls back to FlareSolverr only when Cloudflare challenges are detected.

## Key Decisions

- Language: Python 3.13
- Config: JSON (see `config/app.json`, template with `.json.template`)
- CLI command: `discorsair`
- Auth: config uses single `auth` object with cookie and optional proxy
- FlareSolverr: only used on challenge; UA must match `curl_cffi` impersonate target
  - `cf_clearance` can be cached per proxy IP for reuse

## Repo Structure

- `config/` JSON configuration
- `docs/` usage notes
- `src/` application code
  - `src/core/` request/session/cookie handling
  - `src/discourse/` Discourse API client
  - `src/flows/` high-level flows (watch/like/reply/analysis)
  - `src/utils/` utilities (logging/retry/ua map)
- `tests/` unit tests

## Request Strategy (Summary)

- Default: `curl_cffi`
- On challenge: call FlareSolverr, reuse returned cookies/UA for retry
- UA alignment: single source of truth; FlareSolverr UA must match `curl_cffi` impersonation
- Proxy note: FlareSolverr runs in Docker. If proxy is configured as a loopback URL (e.g. `http://127.0.0.1:port`), pass `http://host.docker.internal:port` to FlareSolverr. `src/core/` should handle this proxy translation.
- FlareSolverr must be deployed in Docker ahead of time (user responsibility).
- `cf_clearance` may be cached by proxy IP and reused before re-solving.
- Storage: SQLite at `storage.path` (default `data/discorsair.db`)

## Coding Conventions

- Keep modules small and focused
- Avoid global state; pass session/context explicitly
- Prefer explicit error handling over silent fallbacks
- Log network decisions (engine used, retries, challenge detection)

## Security / Safety

- Never log full cookies or CSRF tokens
