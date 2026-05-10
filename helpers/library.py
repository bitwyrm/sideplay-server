from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException
import json
import sqlite3

from helpers.db import get_db
from helpers.accounts import require_user
from helpers.sync import get_playlist_tracks_normalized
import yt_music_lib
import spotify_lib
from helpers.setup import PLAYLIST_CACHE_TTL, redis_get_json, redis_set_json


class AddTrackRequest(BaseModel):
  """Request body for manually adding a track to the user's library."""

  provider: str  # 'spotify' or 'youtube'
  track_id: str


class RemoveTrackRequest(BaseModel):
  """Request body for manually removing a YouTube track from the user's library."""

  youtube_id: str


router = APIRouter(tags=["library"])

# -------------------------------------------------------------------
# MANUAL TRACK ADD
# -------------------------------------------------------------------


@router.post("/library/add-track")
def add_track_individually(
  data: AddTrackRequest, user_id: str = Depends(require_user), db=Depends(get_db)
) -> dict:
  """
  Add a single track to the user's library, normalizing to YouTube if needed.

  Args:
    data (AddTrackRequest): Provider and track ID.
    user_id (str): Authenticated user ID from require_user dependency.
    db: Database connection.

  Returns:
    dict: {
      "ok": True/False,
      "youtube_id": str (if successful),
      "title": str (if successful),
      "artists": list[str] (if successful),
      "error": str (if failed)
    }
  """
  try:
    provider = data.provider.lower()
    if provider not in ("spotify", "youtube"):
      return {"ok": False, "error": "Invalid provider, must be 'spotify' or 'youtube'"}

    source_id = data.track_id
    if not source_id:
      return {"ok": False, "error": f"{provider.capitalize()} track ID required"}

    # -------------------------------------------------
    # Fetch source details (provider-specific)
    # -------------------------------------------------
    if provider == "spotify":
      details = spotify_lib.get_song_details(source_id)
      if not details:
        return {"ok": False, "error": "Spotify track not found"}

      title = details.get("title")
      artists = details.get("artists", [])
      if not title or not artists:
        return {"ok": False, "error": "Invalid Spotify track metadata"}

      query = f"{title} by {', '.join(artists)}"
      song = yt_music_lib.map_query_to_song(query)

    else:  # YouTube
      query = yt_music_lib.map_video_to_query(source_id)
      if not query:
        return {"ok": False, "error": "YouTube video not found"}

      song = yt_music_lib.map_query_to_song(query)

    if not song:
      return {"ok": False, "error": "Could not normalize track to YouTube"}

    # -------------------------------------------------
    # Persist (all columns)
    # -------------------------------------------------
    db.execute(
      """
        INSERT OR IGNORE INTO tracks (youtube_id, title, artists, album, available)
        VALUES (?, ?, ?, ?, ?)
      """,
      (
        song["youtube_id"],
        song["title"],
        json.dumps(song["artists"], ensure_ascii=False),
        song.get("album"),
        0,
      ),
    )

    db.execute(
      "INSERT OR IGNORE INTO user_library (user_id, youtube_id) VALUES (?, ?)",
      (user_id, song["youtube_id"]),
    )
    db.commit()

    return {
      "ok": True,
      "youtube_id": song["youtube_id"],
      "title": song["title"],
      "artists": song["artists"],
      "album": song.get("album"),
    }

  except Exception as e:
    # Catch unexpected errors
    return {"ok": False, "error": str(e)}


# -------------------------------------------------------------------
# REMOVE TRACK
# -------------------------------------------------------------------


@router.post("/library/remove-track")
def remove_track(
  data: RemoveTrackRequest, user_id: str = Depends(require_user), db=Depends(get_db)
) -> dict:
  """
  Remove a YouTube track from the user's library if it is not in any playlists.

  Args:
    data (RemoveTrackRequest): YouTube ID to remove.
    user_id (str): Authenticated user ID from require_user dependency.
    db: Database connection.

  Returns:
    dict: {
      "ok": True/False,
      "error": str (if blocked or not found),
      "blocking_playlists": list (if track is in playlists)
    }
  """
  yt_id = data.youtube_id
  blocking = []

  # --- Local playlists ---
  rows = db.execute(
    """
      SELECT playlist_id, name, local_tracks
      FROM playlists
      WHERE user_id = ? AND provider = 'local' AND local_tracks IS NOT NULL
    """,
    (user_id,),
  ).fetchall()

  for r in rows:
    if yt_id in json.loads(r["local_tracks"]):
      blocking.append(
        {"playlist_id": r["playlist_id"], "name": r["name"], "type": "local"}
      )

  # --- Linked playlists (cache only) ---
  rows = db.execute(
    """
      SELECT playlist_id, provider, name
      FROM playlists
      WHERE user_id = ? AND provider IN ('spotify', 'youtube')
    """,
    (user_id,),
  ).fetchall()

  for r in rows:
    cache_key = f"playlist:{r['provider']}:{r['playlist_id']}"
    tracks = redis_get_json(cache_key) or []
    if yt_id in tracks:
      blocking.append(
        {"playlist_id": r["playlist_id"], "name": r["name"], "type": r["provider"]}
      )

  if blocking:
    return {
      "ok": False,
      "error": "Track is used in one or more playlists",
      "blocking_playlists": blocking,
    }

  # --- Safe to remove ---
  deleted = db.execute(
    "DELETE FROM user_library WHERE user_id = ? AND youtube_id = ?",
    (user_id, yt_id),
  ).rowcount

  if not deleted:
    return {"ok": False, "error": "Track not in library"}

  db.commit()
  return {"ok": True}


# -------------------------------------------------------------------
# GET METADATA
# -------------------------------------------------------------------


@router.get("/library/metadata")
def get_metadata(user_id: str = Depends(require_user), db=Depends(get_db)) -> dict:
  """
  Fetch the user's library and linked playlist metadata.

  Library tracks are the source of truth; playlists include cached tracks
  and provider-specific normalization to YouTube IDs.

  Args:
    user_id (str): Authenticated user ID from require_user dependency.
    db: Database connection.

  Returns:
    dict: {
      "library": list of tracks with youtube_id, title, artists, available,
      "playlists": {
        "spotify": [...],
        "youtube": [...]
      }
    }
  """
  # -------------------------------------------------
  # 1. USER LIBRARY (single source of truth)
  # -------------------------------------------------
  library_rows = db.execute(
    """
      SELECT
        t.youtube_id,
        t.title,
        t.artists,
        t.album,
        t.available
      FROM tracks t
      JOIN user_library ul
        ON ul.youtube_id = t.youtube_id
      WHERE ul.user_id = ?
    """,
    (user_id,),
  ).fetchall()

  library = [
    {
      "youtube_id": r["youtube_id"],
      "title": r["title"],
      "artists": json.loads(r["artists"]),
      "album": r["album"],
      "available": bool(r["available"]),
    }
    for r in library_rows
  ]

  # -------------------------------------------------
  # 2. LOAD LINKED PLAYLISTS
  # -------------------------------------------------
  playlists = db.execute(
    """
      SELECT
        playlist_id,
        provider,
        external_id,
        name
      FROM playlists
      WHERE user_id = ?
    """,
    (user_id,),
  ).fetchall()

  playlists_by_provider: dict[str, list] = {"spotify": [], "youtube": [], "local": []}

  # -------------------------------------------------
  # 3. FETCH PLAYLIST TRACKS (Redis → provider)
  # -------------------------------------------------
  for pl in playlists:
    cache_key = f"playlist:{pl['provider']}:{pl['playlist_id']}"
    tracks = redis_get_json(cache_key)

    if tracks is None:
      if pl["provider"] == "youtube":
        if pl["external_id"]:
          tracks = yt_music_lib.get_playlist_songs(pl["external_id"], pl["playlist_id"])
        else:
          tracks = yt_music_lib.get_playlist_video_ids_public(pl["playlist_id"])
      elif pl["provider"] == "spotify":
        tracks = spotify_lib.get_playlist_songs(pl["external_id"], pl["playlist_id"])
      else:
        tracks = []

      redis_set_json(cache_key, tracks, ttl=PLAYLIST_CACHE_TTL)

    # -------------------------------------------------
    # 4. REDUCE → ORDERED CANONICAL YOUTUBE IDS ONLY
    # -------------------------------------------------
    youtube_ids = get_playlist_tracks_normalized(pl, user_id, db)

    provider = pl["provider"]
    playlists_by_provider.setdefault(provider, []).append(
      {
        "playlist_id": pl["playlist_id"],
        "name": pl["name"],
        "tracks": youtube_ids,
      }
    )

  # -------------------------------------------------
  # 5. FINAL PAYLOAD
  # -------------------------------------------------
  return {"library": library, "playlists": playlists_by_provider}
