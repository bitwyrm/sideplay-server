# Agents Guide

This document is for humans/agents making changes in this repo.

## Scope

- Main API service: `core.py`
- Media worker service: `media_service.py`
- Router modules: `helpers/*.py`
- Provider libraries: `spotify_lib.py`, `yt_music_lib.py`

## Hard Safety Rules

- Never read, print, or commit credential secrets.
- Treat all files like `*-credentials.json`, token cache files under `<SIDEPLAY_DATA_ROOT>/oauth/`, and cookie files as sensitive.
- Never log OAuth tokens, session tokens, client secrets, or refresh tokens.

## Architecture

- `core.py` is the source-of-truth service for identity + DB state.
- `media_service.py` is a worker that ensures media bytes exist.
- SQLite is the authoritative state store.
- Redis is cache only (safe to rebuild).

## Local Run Checklist

1. Redis running on `localhost:6379`.
2. `ffmpeg` and `yt-dlp` in PATH.
3. Python deps installed from `requirements.txt`.
4. Start `core.py` and `media_service.py` in separate terminals.

## Persistent Data Root

- Runtime outputs are rooted at `SIDEPLAY_DATA_ROOT` (default `./data`).
- DB/media/thumbs/oauth token files must be treated as persistent state.
- For container deployments, always mount this path to a persistent volume.

## Data Ownership

- DB schema and writes live in `helpers/db.py` and router flows.
- OAuth linking/unlinking and token bootstrap live in `helpers/external.py`.
- Track normalization lives in `helpers/sync.py` + `yt_music_lib.py`.
- Media download/validation lives in `media_service.py`.

## Change Guidelines

- Keep endpoint contracts backward compatible unless explicitly doing a breaking change.
- Prefer narrow, testable edits in a single helper module.
- For DB changes, update `init_db()` and all affected query paths together.
- Keep Redis cache keys stable unless intentionally migrating.
- Do not couple user identity logic into `media_service.py`.
- Any new runtime artifact path must be derived from `helpers.setup` path constants, not hardcoded paths.

## High-Risk Areas

- `helpers/sync.py`: normalization and streaming lifecycle.
- `helpers/playlists.py`: cascade delete behavior and local/external playlist handling.
- `helpers/external.py`: OAuth flows and account linkage.
- `spotify_lib.py` and `yt_music_lib.py`: provider API assumptions + token refresh paths.

## Recommended Next Refactors

- Add startup validation checks for required runtime files and env.
- Replace wildcard imports (`from helpers.setup import *`) with explicit imports.
- Standardize API error style (avoid mixed `{"ok": False}` and HTTP exceptions).
- Add integration tests for sync + playlist delete cascade + media availability transitions.
