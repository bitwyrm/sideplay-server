Good — at this point you’re no longer “experimenting”, you’re designing a real system. I’ll give you a **clean, end-to-end specification** that ties together:

* Your **two existing libraries** (YouTube Music + Spotify)
* The **API service**
* The **media (yt-dlp) service**
* **What is stored where**
* **How data flows**
* **Public-facing library documentation** (so you can use them without source)

I’ll be explicit, opinionated, and consistent with everything you’ve already decided.

---

# High-Level Architecture

You have **two services** and **two internal libraries**.

```
┌──────────────────────────────┐
│        API Service            │
│  (Auth + Sync + Metadata)     │
│                               │
│  ┌─────────────────────────┐ │
│  │ SQLite (source of truth)│ │
│  └─────────────────────────┘ │
│                               │
│  Uses:                        │
│   - yt_music_lib              │
│   - spotify_lib               │
└───────────────┬───────────────┘
                │
                │ jobs / RPC / HTTP
                ▼
┌──────────────────────────────┐
│       Media Service           │
│   (yt-dlp, storage)           │
│                               │
│  Filesystem (blobs only)      │
└──────────────────────────────┘
```

**Core principle**

* API service = *truth, identity, structure*
* Media service = *bytes*

---

# Services

## 1. API Service (Primary)

### Responsibilities

* User authentication
* OAuth account linking / unlinking
* Playlist discovery
* Track normalization
* Library & playlist state
* Sync orchestration
* Session management

### Owns

* SQLite database
* OAuth credential files
* Normalization logic
* Sync rules

### Does NOT own

* Media downloading
* Audio/video files

---

## 2. Media Service (Worker)

### Responsibilities

* Given YouTube IDs, ensure media exists
* Download via `yt-dlp`
* Validate availability
* Return success/failure

### Owns

* Media filesystem
* Temporary download state

### Does NOT own

* User identity
* Playlists
* Libraries
* OAuth

---

# Storage Specification

## SQLite (Authoritative State)

**If this is lost, the app is broken.**

### Tables (conceptual)

### Users

* `user_id`
* `password_hash`

### Sessions

* `session_token`
* `user_id`
* `expires_at`

### Accounts (linked identities)

* `user_id`
* `provider` (`spotify`, `youtube`)
* `external_id` (Spotify user ID or Google `sub`)

### Tracks (global, deduplicated)

* `youtube_id` (PK)
* `artist`
* `title`

### User Library

* `user_id`
* `youtube_id`
* `source` (`playlist`, `manual`)

### Playlists

* `playlist_id` (internal)
* `user_id`
* `provider`
* `external_id`
* `name`
* `last_synced_at`

### Playlist Tracks

* `playlist_id`
* `youtube_id`

---

## Filesystem (Cache / Blobs)

**If this is lost, you re-download.**

```
data/
├── media/
│   ├── <youtube_id>.mp3
│   └── <youtube_id>.webm
├── oauth/
│   ├── spotify/<external_id>.json
│   └── youtube/<sub>.json
├── temp/
└── logs/
```

OAuth files live on disk because:

* They are opaque
* Rotated externally
* Not queried relationally

---

# Internal Libraries (Public Documentation)

This is the documentation you asked for earlier, consolidated and cleaned.

---

## `yt_music_lib`

### Purpose

YouTube Music access and deterministic resolution of tracks to YouTube video IDs.

### Public API

### `search_songs(query: str, limit: int = 20) -> List[SongResult]`

Search public YouTube Music catalog.

Returns:

```python
{
  "artist": str,
  "title": str,
  "video_id": str | None
}
```

Notes:

* No auth required
* Used for public search endpoint

---

### `search_playlists(query: str, limit: int = 20) -> List[PlaylistResult]`

Search public YouTube playlists.

---

### `get_user_playlists(sub: str) -> List[Playlist]`

Get all playlists for an authenticated YouTube account.

Requires:

* OAuth headers stored at `cache/youtube/<sub>.json`

---

### `get_playlist_tracks(playlist_id: str) -> List[RawTrack]`

Retrieve tracks from a YouTube playlist.

---

### `normalize_track(artist: str, title: str) -> str | None`

Deterministically resolves `(artist, title)` to a YouTube video ID.

This is **the normalization backbone** of the entire system.

---

## `spotify_lib`

### Purpose

Spotify account access, playlist discovery, and track extraction.

### Public API

### `search_tracks(query: str, limit: int = 20) -> List[TrackResult]`

Search Spotify tracks.

---

### `search_playlists(query: str, limit: int = 20) -> List[PlaylistResult]`

Search public Spotify playlists.

---

### `get_user_playlists(account_id: str) -> List[Playlist]`

Get playlists for a linked Spotify account.

---

### `get_playlist_tracks(playlist_id: str) -> List[SpotifyTrack]`

Get tracks from a Spotify playlist.

Returns:

```python
{
  "artist": str,
  "title": str
}
```

Must be normalized via `yt_music_lib`.

---

# API Behavior Specification

## Authentication

* User logs in
* Session token issued
* Token required for all endpoints

---

## Account Linking

* OAuth flow (Google / Spotify)
* On success:

  * Store `external_id` in DB
  * Store credentials on disk
* Unlink:

  * Remove DB record
  * Delete credential file

---

## Playlist Listing

* Aggregates playlists from **all linked accounts**
* Response includes:

  * Provider
  * Account ID
  * Playlist ID
  * Name

If no accounts → empty list

---

## Search

* **Songs**: YouTube Music only
* **Playlists**: Spotify + YouTube

---

## Playlist Track Fetching

* Fetch raw tracks
* Normalize `(artist, title)` → `youtube_id`
* Drop unresolved tracks
* Return valid YouTube IDs (basically should not return unresolved tracks)

---

## Sync Flow (Critical Path)

1. User logs in
2. Fetch all linked playlists
3. Fetch all tracks in playlists
4. Normalize to YouTube IDs
5. Subtract out user's library to get the "new media"
6. Update user library to include tracks in the global library
7. Download the tracks not in the global library with the media service
8. Call media service:

   ```
   ensure_media(youtube_ids)
   ```
9. Stream progress to API (songs downloaded, songs failed, and current song being worked on)
10. Forward streamed progress to user (how many downloaded, how many failed, how many total)
11. Whenever a song downloads successfully:

   * Update `tracks`
   * Update `user_library`
   * Update `playlist_tracks`

**DB update is atomic. Media is eventual.**

---

# Media Service Contract

### Input

```json
{
  "youtube_ids": ["id1", "id2", ...]
}
```

### Output

```json
{
  "id1": "ok",
  "id2": "failed"
}
```

The API service decides what to do with failures.

---

# Design Decisions (Why this works)

* Tracks are **owned by the user library**, not playlists
* Playlists are **views**, not containers
* Removing a song from a playlist does **not** remove it from the library
* SQLite handles all unbounded growth safely
* Filesystem stores only replaceable data
* Media service is stateless and restartable

---

# What you have now

You now have:

* A clean separation of concerns
* A deterministic data model
* A scalable sync story
* A future-proof API surface
* Documentation you can hand to “future you” and still understand

This is a solid system.
