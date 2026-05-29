import json
import secrets
import logging
from typing import Optional

import spotify_lib
import yt_music_lib
from helpers.db import get_db
from fastapi import APIRouter, Depends, HTTPException
from helpers.accounts import require_user
from pydantic import BaseModel
from helpers.setup import now_iso
from helpers.sync import get_playlist_tracks_normalized

# ----------------------------
# External playlist linking
# ----------------------------


class LinkPlaylistRequest(BaseModel):
  """Request body for linking an external playlist to the current user."""

  provider: str  # 'spotify' or 'youtube'
  playlist_id: str
  sub: Optional[str] = None  # YouTube user sub
  account_id: Optional[str] = None  # Spotify user id


# ----------------------------
# Local playlists
# ----------------------------


class CreateLocalPlaylistRequest(BaseModel):
  """Request body for creating a new local playlist."""

  name: str
  tracks: list[str] = []


class UpdateLocalPlaylistRequest(BaseModel):
  """Request body for updating tracks in an existing local playlist."""

  tracks: list[str]


router = APIRouter(tags=["playlists"])
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# LINK PLAYLIST
# -------------------------------------------------------------------


@router.post("/playlists/link")
def link_playlist(
  data: LinkPlaylistRequest, user_id: str = Depends(require_user), db=Depends(get_db)
) -> dict:
  """
  Link an external Spotify or YouTube playlist to the current user.

  Args:
    data (LinkPlaylistRequest): Provider, playlist ID, and account identifier.
    user_id (str): Authenticated user ID.
    db: Database connection.

  Raises:
    HTTPException 400: Invalid provider.
    HTTPException 404: Playlist not found for the given account.

  Returns:
    dict: {"ok": True} on success.
  """
  provider = data.provider.lower()
  if provider not in ("spotify", "youtube"):
    raise HTTPException(400, "Invalid provider")

  external_id = data.sub if provider == "youtube" else data.account_id
  playlist_info = None

  # If caller provides account identifier, prefer user-owned playlist discovery.
  if external_id:
    try:
      if provider == "spotify":
        playlists = spotify_lib.get_user_playlists(external_id)
        playlist_info = next((p for p in playlists if p["id"] == data.playlist_id), None)
      else:
        playlists = yt_music_lib.get_user_playlists(external_id)
        playlist_info = next((p for p in playlists if p["id"] == data.playlist_id), None)
    except Exception as exc:
      logger.warning(
        "event=link_playlist_provider_error provider=%s external_id=%s error=%s",
        provider,
        external_id,
        exc,
      )
      raise HTTPException(502, f"{provider} provider unavailable")
  else:
    # Public link mode: no account identifier needed.
    if provider == "spotify":
      playlist_info = spotify_lib.get_playlist_info_public(data.playlist_id)
    else:
      playlist_info = yt_music_lib.get_playlist_info_public(data.playlist_id)

  if not playlist_info:
    if external_id:
      raise HTTPException(404, "Playlist not found for this account")
    raise HTTPException(404, "Public playlist not found")

  playlist_name = playlist_info.get("title", "Unnamed Playlist")

  # Insert playlist into DB
  db.execute(
    """
      INSERT INTO playlists (playlist_id, user_id, provider, external_id, name, last_synced_at)
      VALUES (?, ?, ?, ?, ?, ?)
      ON CONFLICT(playlist_id) DO UPDATE SET
        user_id = excluded.user_id,
        external_id = excluded.external_id,
        name = excluded.name,
        last_synced_at = excluded.last_synced_at
    """,
    (data.playlist_id, user_id, provider, external_id, playlist_name, now_iso()),
  )

  db.commit()
  return {"ok": True}


# -------------------------------------------------------------------
# LIST PLAYLISTS
# -------------------------------------------------------------------


@router.get("/playlists")
def playlists(user_id: str = Depends(require_user), db=Depends(get_db)) -> list[dict]:
  """
  List all playlists for the current user (local + linked).
  """
  rows = db.execute(
    """
      SELECT playlist_id, provider, external_id, name, local_tracks, last_synced_at
      FROM playlists
      WHERE user_id = ?
      ORDER BY name COLLATE NOCASE ASC
    """,
    (user_id,),
  ).fetchall()

  out = []
  for row in rows:
    normalized_tracks = get_playlist_tracks_normalized(dict(row), user_id, db)
    out.append(
      {
        "playlist_id": row["playlist_id"],
        "provider": row["provider"],
        "external_id": row["external_id"],
        "name": row["name"],
        "last_synced_at": row["last_synced_at"],
        "tracks": normalized_tracks,
      }
    )
  return out


@router.get("/playlists/{playlist_id}")
def get_playlist(
  playlist_id: str, user_id: str = Depends(require_user), db=Depends(get_db)
) -> dict:
  """
  Return a single playlist (local or linked) for the current user.
  """
  row = db.execute(
    """
      SELECT playlist_id, provider, external_id, name, local_tracks, last_synced_at
      FROM playlists
      WHERE playlist_id = ? AND user_id = ?
    """,
    (playlist_id, user_id),
  ).fetchone()
  if not row:
    raise HTTPException(404, "Playlist not found")

  normalized_tracks = get_playlist_tracks_normalized(dict(row), user_id, db)
  return {
    "playlist_id": row["playlist_id"],
    "provider": row["provider"],
    "external_id": row["external_id"],
    "name": row["name"],
    "last_synced_at": row["last_synced_at"],
    "tracks": normalized_tracks,
  }


# -------------------------------------------------------------------
# LOCAL PLAYLISTS CRUD
# -------------------------------------------------------------------


def assert_tracks_in_library(*, user_id: str, tracks: list[str], db) -> None:
  """
  Validate that all provided YouTube IDs exist in the user's library.

  Args:
    user_id (str): Authenticated user ID.
    tracks (list[str]): Candidate YouTube IDs.
    db: Database connection.

  Returns:
    None

  Raises:
    HTTPException: If any track is missing from the user library.
  """
  if not tracks:
    return
  placeholders = ",".join("?" * len(tracks))
  rows = db.execute(
    f"""
      SELECT youtube_id
      FROM user_library
      WHERE user_id = ? AND youtube_id IN ({placeholders})
    """,
    (user_id, *tracks),
  ).fetchall()
  if len(rows) != len(set(tracks)):
    raise HTTPException(
      400, "One or more YouTube IDs are not present in the user library"
    )


@router.post("/playlists/local/create")
def create_local_playlist(
  data: CreateLocalPlaylistRequest,
  user_id: str = Depends(require_user),
  db=Depends(get_db),
) -> dict:
  """
  Create a new local playlist for the user with selected tracks.

  Args:
    data (CreateLocalPlaylistRequest): Playlist name and list of YouTube IDs.
    user_id (str): Authenticated user ID.
    db: Database connection.

  Raises:
    HTTPException 400: If any track is not in the user's library.

  Returns:
    dict: {"ok": True, "playlist_id": str} on success.
  """
  assert_tracks_in_library(user_id=user_id, tracks=data.tracks, db=db)
  playlist_id = secrets.token_hex(63)
  db.execute(
    """
      INSERT INTO playlists
      (playlist_id, user_id, provider, external_id, name, local_tracks, last_synced_at)
      VALUES (?, ?, 'local', NULL, ?, ?, ?)
    """,
    (
      playlist_id,
      user_id,
      data.name,
      json.dumps(data.tracks, ensure_ascii=False),
      now_iso(),
    ),
  )
  db.commit()
  return {"ok": True, "playlist_id": playlist_id}


@router.post("/playlists/local/{playlist_id}/update")
def update_local_playlist(
  playlist_id: str,
  data: UpdateLocalPlaylistRequest,
  user_id: str = Depends(require_user),
  db=Depends(get_db),
) -> dict:
  """
  Update an existing local playlist's tracks.

  Args:
    playlist_id (str): ID of the local playlist.
    data (UpdateLocalPlaylistRequest): List of YouTube IDs to set.
    user_id (str): Authenticated user ID.
    db: Database connection.

  Raises:
    HTTPException 404: Playlist not found.
    HTTPException 400: If any track is not in the user's library.

  Returns:
    dict: {"ok": True} on success.
  """
  logger.info(
    "event=update_local_playlist_request user_id=%s playlist_id=%s track_count=%d",
    user_id,
    playlist_id,
    len(data.tracks),
  )

  row = db.execute(
    """
      SELECT playlist_id
      FROM playlists
      WHERE playlist_id = ? AND user_id = ? AND provider = 'local' AND external_id IS NULL
    """,
    (playlist_id, user_id),
  ).fetchone()

  if not row:
    raise HTTPException(404, "Local playlist not found")

  assert_tracks_in_library(user_id=user_id, tracks=data.tracks, db=db)

  db.execute(
    """
      UPDATE playlists
      SET local_tracks = ?, last_synced_at = ?
      WHERE playlist_id = ?
    """,
    (json.dumps(data.tracks, ensure_ascii=False), now_iso(), playlist_id),
  )
  db.commit()
  return {"ok": True}


# -------------------------------------------------------------------
# DELETE PLAYLIST
# -------------------------------------------------------------------
@router.delete("/playlists/delete/{playlist_id}")
def delete_playlist(
  playlist_id: str,
  cascade: bool = False,
  user_id: str = Depends(require_user),
  db=Depends(get_db),
) -> dict:
  """
  Delete a playlist (local or external). Optionally cascade remove tracks
  from user_library if not used elsewhere.

  Args:
    playlist_id (str): Playlist ID to delete.
    cascade (bool): Whether to remove tracks from library if unused elsewhere.
    user_id (str): Authenticated user ID.
    db: Database connection.

  Raises:
    HTTPException 404: Playlist not found.

  Returns:
    dict: {
      "ok": True,
      "deleted_tracks": list of track objects removed from library
    }
  """
  # Fetch playlist info
  row = db.execute(
    "SELECT provider, local_tracks, name FROM playlists WHERE playlist_id = ? AND user_id = ?",
    (playlist_id, user_id),
  ).fetchone()

  if not row:
    raise HTTPException(404, "Playlist not found")

  deleted_tracks = []

  if cascade:
    # Remove tracks from user_library if they are not used elsewhere
    deleted_tracks = cascade_remove_tracks(user_id, playlist_id, db)

  # Delete the playlist itself
  db.execute("DELETE FROM playlists WHERE playlist_id = ?", (playlist_id,))
  db.commit()

  return {"ok": True, "deleted_tracks": deleted_tracks}


def cascade_remove_tracks(user_id: str, playlist_id: str, db) -> list[dict]:
  """
  Remove tracks from the user's library if they are no longer used in
  any other playlist.

  Args:
    user_id (str): Authenticated user ID.
    playlist_id (str): Playlist to check.
    db: Database connection.

  Returns:
    list[dict]: Deleted track objects with youtube_id, title, artists.
  """
  playlist_row = db.execute(
    """
      SELECT playlist_id, provider, external_id, local_tracks
      FROM playlists
      WHERE user_id = ? AND playlist_id = ?
    """,
    (user_id, playlist_id),
  ).fetchone()
  if not playlist_row:
    return []

  playlist = dict(playlist_row)

  # Tracks in this playlist (normalized objects)
  tracks = get_playlist_tracks_normalized(playlist, user_id, db)
  if not tracks:
    return []

  # Gather all tracks from other playlists into a single set of IDs
  other_rows = db.execute(
    """
      SELECT playlist_id, provider, external_id, local_tracks
      FROM playlists
      WHERE user_id = ? AND playlist_id != ?
    """,
    (user_id, playlist_id),
  ).fetchall()

  other_track_ids = set()
  for pl in other_rows:
    other_tracks = get_playlist_tracks_normalized(dict(pl), user_id, db)
    other_track_ids.update(t["youtube_id"] for t in other_tracks)

  # Delete from user_library if not used in other playlists
  deleted_tracks = []
  for t in tracks:
    yt_id = t["youtube_id"]
    if yt_id not in other_track_ids:
      db.execute(
        "DELETE FROM user_library WHERE user_id = ? AND youtube_id = ?",
        (user_id, yt_id),
      )
      deleted_tracks.append(t)

  return deleted_tracks
