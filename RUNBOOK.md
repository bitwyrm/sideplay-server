# Runbook

## Prerequisites
- Python 3.11+
- Redis running on `localhost:6379`
- `ffmpeg` and `yt-dlp` in `PATH`
- Required credential/token files present locally

## Start sequence
1. Start Redis.
2. Start media service:
   - `python media_service.py`
3. Start API service:
   - `python core.py`

## Health checks
- API root page: `GET /`
- API docs: `GET /docs`
- Media ensure endpoint (internal): `POST http://127.0.0.1:7000/ensure-media`

## Common failures
- `Missing session_token`: protected endpoint called without auth header.
- `tracks required`: media ensure request body missing `tracks`.
- `Connection refused` to media service from sync: media worker not running.
- OAuth callback failures: redirect URIs or credential files mismatched.

## Streaming search contract
- `POST /search/playlists/stream` returns JSONL with two provider chunks:
  - first line: `{"youtube":[...]}`
  - second line: `{"spotify":[...]}`

## Access mode
- `OPEN_ACCESS` (default `false`) controls anonymous access for selected read/search endpoints.

## Logs and diagnostics
- API and media services log structured events to stdout.
- For sync issues, watch events with prefixes:
  - `event=sync_tracks_resolved`
  - `event=media_service_request_retry`
  - `event=stream_progress_error`

## Recovery actions
- Stale cache behavior: restart Redis or delete specific keys.
- Corrupt local media/thumbnail files: remove file(s) and run sync again.
- Schema drift concerns: restart API to run startup migrations in `helpers/db.py`.
