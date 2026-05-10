# Architecture

This project is organized into four logical layers while keeping current file layout intact.

## 1) API Layer (`core.py`, `helpers/*.py`)
- `core.py` wires FastAPI, static files, global error handlers, and routers.
- `helpers/*.py` contains request handlers grouped by domain (`accounts`, `external`, `playlists`, `library`, `search`, `sync`, `media`, `whitelist`).

## 2) Service Layer (inside router modules)
- Cross-provider orchestration and workflow logic is primarily in `helpers/sync.py` and playlist/library endpoints.
- Media orchestration is split: API triggers `media_service.py` worker over HTTP.

## 3) Provider Layer (`spotify_lib.py`, `yt_music_lib.py`)
- Encapsulates Spotify and YouTube API access.
- Handles provider pagination, lookups, and token refresh/use.

## 4) Data/Infra Layer (`helpers/db.py`, `helpers/setup.py`)
- `helpers/db.py`: SQLite schema + migrations + connection lifecycle.
- `helpers/setup.py`: shared config constants and Redis JSON helpers.

## Runtime topology
- API service (`core.py`) is source of truth for identity + metadata.
- Media service (`media_service.py`) is responsible for ensuring audio and thumbnail files exist.
- Redis is cache only; SQLite is authoritative.

