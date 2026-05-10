import json
import logging
import time
import sqlite3
from collections.abc import Generator
from typing import Dict, List

import requests
import spotify_lib
import yt_music_lib
from helpers.db import DB_PATH, get_db
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from helpers.accounts import require_user
from pydantic import BaseModel
from helpers.setup import PLAYLIST_CACHE_TTL, redis_get_json, redis_set_json

router = APIRouter(tags=["sync"])
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# SYNC
# -------------------------------------------------------------------


class SyncAllRequest(BaseModel):
  force: bool = False


@router.post("/sync-library", response_model=None)
def sync_all(
  req: SyncAllRequest, user_id: str = Depends(require_user), db=Depends(get_db)
) -> object:
  """
  Synchronize all playlists for the user and normalize tracks to canonical YouTube IDs.

  Steps:
    1. Load all playlists for the user.
    2. Normalize tracks via get_playlist_tracks_normalized.
    3. Persist resolved tracks to DB.
    4. Identify tracks not yet available locally.
    5. Stream download progress and flip availability.

  Args:
    req (SyncAllRequest): Whether to force re-downloads.
    user_id (str): Authenticated user ID.
    db: Database connection.

  Returns:
    dict: If no playlists or tracks exist.
    StreamingResponse: Streams JSONL of download progress if tracks need to be fetched.
  """
  # -------------------------------------------------
  # 1. Load all playlists for the user
  # -------------------------------------------------
  playlists = db.execute(
    "SELECT playlist_id, provider, external_id, local_tracks FROM playlists WHERE user_id = ?",
    (user_id,),
  ).fetchall()

  if not playlists:
    return {"ok": True, "message": "No playlists"}

  # -------------------------------------------------
  # 2. Fetch all tracks from each playlist (normalized)
  # -------------------------------------------------
  merged_tracks: dict[str, dict] = {}
  skipped_playlists: list[dict] = []
  for pl in playlists:
    tracks = get_playlist_tracks_normalized(pl, user_id, db, strict_fetch=True)
    if tracks is None:
      skipped_playlists.append(
        {
          "playlist_id": pl["playlist_id"],
          "provider": pl["provider"],
          "reason": "fetch_failed",
        }
      )
      continue
    for t in tracks:
      # Remove duplicates
      merged_tracks[t["youtube_id"]] = t

  if not merged_tracks:
    if skipped_playlists:
      return {
        "ok": True,
        "message": "No tracks found; some playlists could not be fetched",
        "skipped_playlists": skipped_playlists,
      }
    return {"ok": True, "message": "No tracks found"}

  # -------------------------------------------------
  # 3. Persist resolved tracks to DB (authoritative)
  # -------------------------------------------------
  resolved = {
    t["youtube_id"]: {
      "title": t.get("title", ""),
      "artists": t.get("artists", []),
      "album": t.get("album"),
    }
    for t in merged_tracks.values()
  }
  logger.info(
    "event=sync_tracks_resolved user_id=%s track_count=%d",
    user_id,
    len(resolved),
  )

  for yt_id, meta in resolved.items():
    db.execute(
      """
        INSERT INTO tracks (youtube_id, title, artists, album, available)
        VALUES (?, ?, ?, ?, 0)
        ON CONFLICT(youtube_id) DO UPDATE SET
          title = excluded.title,
          artists = excluded.artists,
          album = excluded.album
      """,
      (
        yt_id,
        meta.get("title", ""),
        json.dumps(meta.get("artists", [])),
        meta.get("album"),
      ),
    )
    db.execute(
      "INSERT OR IGNORE INTO user_library (user_id, youtube_id) VALUES (?, ?)",
      (user_id, yt_id),
    )

  db.commit()

  # -------------------------------------------------
  # 4. Find tracks that are not yet available locally
  # -------------------------------------------------
  rows = db.execute(
    "SELECT t.youtube_id FROM tracks t JOIN user_library ul ON ul.youtube_id=t.youtube_id WHERE ul.user_id=? AND t.available=0",
    (user_id,),
  ).fetchall()
  to_download = {r["youtube_id"] for r in rows}

  if not to_download:
    return {
      "ok": True,
      "message": "Library already complete",
      "skipped_playlists": skipped_playlists,
    }

  # -------------------------------------------------
  # 5. Stream download progress and flip availability
  # -------------------------------------------------
  return StreamingResponse(
    stream_progress(to_download, skipped_playlists),
    media_type="application/jsonl",
  )


def stream_progress(
  to_download: set[str], skipped_playlists: list[dict] | None = None
) -> Generator[str, None, None]:
  """
  Generator that streams JSON lines of download progress for tracks.

  Updates 'available' in tracks table for successful downloads.

  Args:
    to_download (set[str]): Set of YouTube IDs to download.

  Yields:
    str: JSON-encoded line with track ID, status, and progress metadata.
  """
  total = len(to_download)
  downloaded = 0
  failed = 0
  conn: sqlite3.Connection | None = None
  resp = None

  try:
    if skipped_playlists:
      yield (
        json.dumps(
          {
            "status": "warning",
            "skipped_playlists": skipped_playlists,
          }
        )
        + "\n"
      )

    # Use a dedicated DB connection for streaming updates
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    for attempt in range(3):
      try:
        meta_rows = conn.execute(
          f"""
          SELECT youtube_id, title, artists, album
          FROM tracks
          WHERE youtube_id IN ({",".join("?" for _ in to_download)})
          """,
          tuple(to_download),
        ).fetchall()
        meta_by_id = {r["youtube_id"]: r for r in meta_rows}
        track_payload = []
        for yt_id in to_download:
          row = meta_by_id.get(yt_id)
          artist = None
          if row and row["artists"]:
            try:
              artists = json.loads(row["artists"])
              if isinstance(artists, list) and artists:
                artist = ", ".join([a for a in artists if isinstance(a, str)])
            except Exception:
              artist = None
          track_payload.append(
            {
              "youtube_id": yt_id,
              "song_name": (row["title"] if row else None),
              "artist": artist,
              "album": (row["album"] if row else None),
            }
          )

        resp = requests.post(
          "http://localhost:7000/ensure-media",
          json={"tracks": track_payload},
          stream=True,
          timeout=600,
        )
        resp.raise_for_status()
        logger.info(
          "event=media_service_request_ok attempt=%d track_count=%d",
          attempt + 1,
          total,
        )
        break
      except requests.RequestException as exc:
        if attempt == 2:
          raise
        backoff_seconds = 2 ** attempt
        logger.warning(
          "event=media_service_request_retry attempt=%d backoff_s=%d error=%s",
          attempt + 1,
          backoff_seconds,
          exc,
        )
        time.sleep(backoff_seconds)

    for line in resp.iter_lines():
      if not line:
        continue

      result = json.loads(line.decode())
      yt_id = result.get("youtube_id")
      status = result.get("status")

      # Track downloaded / failed counts
      if status == "ok":
        downloaded += 1
        # Flip availability
        conn.execute("UPDATE tracks SET available = 1 WHERE youtube_id = ?", (yt_id,))
        conn.commit()
      elif status == "failed":
        failed += 1
        logger.warning(
          "event=media_download_failed youtube_id=%s error=%s",
          yt_id,
          result.get("error"),
        )

      # Add progress metadata
      result["progress"] = {
        "total": total,
        "downloaded": downloaded,
        "failed": failed,
      }

      yield json.dumps(result) + "\n"

  except Exception as e:
    logger.exception("event=stream_progress_error error=%s", e)
    yield (
      json.dumps(
        {
          "error": str(e),
          "progress": {"total": total, "downloaded": downloaded, "failed": failed},
        }
      )
      + "\n"
    )
  finally:
    if conn is not None:
      conn.close()


def get_playlist_tracks_normalized(
  playlist: Dict, user_id: str | None = None, db=None, strict_fetch: bool = False
) -> list[dict] | None:
  """
  Return a list of canonical YouTube track objects for a playlist.

  Handles local playlists, YouTube, and Spotify, applying per-user whitelist.

  Args:
    playlist (dict): {
      "playlist_id": str,
      "provider": "local" | "youtube" | "spotify",
      "local_tracks": Optional[str],  # JSON string
      "external_id": Optional[str]
    }
    user_id (str | None): ID of current user, used for whitelist.
    db: Database connection, required if user_id is given.

  Returns:
    list[dict]: Each track dict includes:
      - youtube_id (str)
      - title (str)
      - artists (list[str])
    None: If strict_fetch=True and provider fetch fails.
  """
  # Load whitelisted tracks
  playlist = dict(playlist)

  whitelist = set()
  if user_id and db:
    rows = db.execute(
      "SELECT youtube_id FROM user_whitelist WHERE user_id = ?",
      (user_id,),
    ).fetchall()
    whitelist = {r["youtube_id"] for r in rows}

  provider = playlist["provider"]
  if provider == "local":
    # Local playlist: return as-is
    return json.loads(playlist.get("local_tracks") or "[]")

  # External playlist (YouTube or Spotify) — check Redis cache first
  cache_key = f"playlist:{provider}:{playlist['playlist_id']}"
  tracks = redis_get_json(cache_key)

  if tracks is None:
    try:
      if provider == "youtube":
        if playlist.get("external_id"):
          tracks = yt_music_lib.get_playlist_songs(
            playlist["external_id"], playlist["playlist_id"]
          )
        else:
          tracks = yt_music_lib.get_playlist_video_ids_public(playlist["playlist_id"])
      else:  # spotify
        tracks = spotify_lib.get_playlist_songs(
          playlist["external_id"], playlist["playlist_id"]
        )
    except Exception as exc:
      logger.warning(
        "event=playlist_tracks_provider_error provider=%s playlist_id=%s external_id=%s error=%s",
        provider,
        playlist["playlist_id"],
        playlist.get("external_id"),
        exc,
      )
      if strict_fetch:
        return None
      tracks = []
    if tracks is not None:
      redis_set_json(cache_key, tracks, ttl=PLAYLIST_CACHE_TTL)

  normalized: List[dict] = []
  if provider == "youtube":
    for video_id in tracks:
      if not video_id:
        continue
      # Skip normalization if whitelisted
      if video_id in whitelist:
        # Include full metadata for whitelisted tracks (fallback to empty title/artists if needed)
        normalized.append(
          {
            "youtube_id": video_id,
            "title": "",  # could load from DB if available
            "artists": [],
            "album": None,
          }
        )
        continue

      query = yt_music_lib.map_video_to_query(video_id)
      if not query:
        continue
      song = yt_music_lib.map_query_to_song(query)
      if not song:
        continue
      normalized.append(
        {
          "youtube_id": song["youtube_id"],
          "title": song.get("title", ""),
          "artists": song.get("artists", []),
          "album": song.get("album"),
        }
      )
  else:  # spotify
    for t in tracks:
      title = t.get("title")
      spotify_artists = t.get("artists")
      if not title or not spotify_artists:
        continue
      query = f"{title} by {', '.join(spotify_artists)}"
      song = yt_music_lib.map_query_to_song(query)
      if song:
        yt_artists = song.get("artists", [])
        # Merge Spotify + YouTube artists case-insensitively, preferring Spotify casing
        merged_artists = {a.lower(): a for a in yt_artists}  # start with YouTube casing
        for a in spotify_artists:
          merged_artists[a.lower()] = a  # Spotify casing overwrites YouTube
        normalized.append(
          {
            "youtube_id": song["youtube_id"],
            "title": song.get("title") or title,
            "artists": list(merged_artists.values()),
            "album": song.get("album") or t.get("album"),
          }
        )

  return normalized
