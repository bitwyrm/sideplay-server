# Sideplay API and Data Flow

This document describes the current API surface and how data moves between the API service and media worker.

## Services

- API service (`core.py`): authentication, account linking, playlist/library state, and sync orchestration.
- Media service (`media_service.py`): downloads and validates media files for YouTube IDs.

## Source of Truth

- SQLite is authoritative for users, sessions, accounts, playlists, tracks, and user library membership.
- Redis is cache-only (search results and playlist track payloads).
- Filesystem stores media blobs, thumbnails, and OAuth token cache files.

## Endpoint Groups

### Auth and Account

- `POST /sign-up`
- `POST /login`
- `POST /logout`
- `POST /account/reset-username`
- `POST /account/reset-password`
- `DELETE /account`

`POST /sign-up` validation defaults:
- `username`: 3-64 chars (trimmed)
- `password`: 8-128 chars

### OAuth and External Accounts

- `GET /oauth/spotify/init`
- `GET /oauth/spotify/callback`
- `GET /oauth/youtube/init`
- `GET /oauth/youtube/callback`
- `GET /external-accounts`
- `POST /external-accounts/unlink`

### Playlists

- `POST /playlists/link`
- `GET /playlists` (returns both local and linked playlists)
- `GET /playlists/{playlist_id}`
- `POST /playlists/local/create`
- `POST /playlists/local/{playlist_id}/update`
- `DELETE /playlists/delete/{playlist_id}`

### Library

- `GET /library`
- `POST /library/add-track`
- `POST /library/remove-track`

### Search

- `POST /search/tracks`
- `POST /search/playlists/spotify`
- `POST /search/playlists/youtube`
- `GET /capabilities`

### Sync and Media

- `POST /sync-library` (JSONL streaming progress)
- `GET /tracks/{youtube_id}/media`
- `GET /tracks/{youtube_id}/thumbnail`
- `POST http://127.0.0.1:7000/ensure-media` (internal worker endpoint)

## Sync Flow (`POST /sync-library`)

1. Load all playlists for the authenticated user (local + linked).
2. Normalize playlist tracks to canonical YouTube IDs.
3. Upsert tracks and user library membership into SQLite.
4. Identify unavailable tracks.
5. Call media worker and stream progress lines (`application/jsonl`).

## Auth Model

Protected endpoints require `session_token` header unless `OPEN_ACCESS` is enabled for that endpoint path. Tokens are issued by `POST /login` and validated against the `sessions` table with expiration.

## Open Access Flag

`OPEN_ACCESS` controls whether selected read/search endpoints allow anonymous access:
- env var: `OPEN_ACCESS`
- accepted true values: `1`, `true`, `yes`, `on`
- default when unset: `false`
