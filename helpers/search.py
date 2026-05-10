from fastapi import APIRouter, Request
from pydantic import BaseModel
import spotify_lib
import yt_music_lib
import logging
from helpers.accounts import get_user_from_session, require_user_or_open_access
from helpers.db import get_db
from fastapi import Depends
from helpers.setup import (
  SPOTIFY_ENABLED,
  GOOGLE_CREDENTIALS_ACCESSIBLE,
)

router = APIRouter(tags=["search"])
logger = logging.getLogger(__name__)


# -----------------------------
# Request models
# -----------------------------
class SearchRequest(BaseModel):
  query: str


def _get_spotify_external_id(request: Request, db) -> str | None:
  spotify_external_id = None
  token = request.headers.get("session_token")
  if token:
    user_id = get_user_from_session(token, db)
    if user_id:
      row = db.execute(
        "SELECT external_id FROM accounts WHERE user_id = ? AND provider = 'spotify' LIMIT 1",
        (user_id,),
      ).fetchone()
      if row:
        spotify_external_id = row["external_id"]
  return spotify_external_id


# -----------------------------
# Endpoints
# -----------------------------
@router.post("/search/tracks")
def search_tracks(
  req: SearchRequest, _auth: str | None = Depends(require_user_or_open_access)
) -> list[dict]:
  """
  Search for tracks on YouTube using the query string.

  Args:
    req (SearchRequest): Request containing the search query.

  Returns:
    list[dict]: List of normalized track objects from YouTube.
  """
  try:
    return yt_music_lib.search_songs(req.query)
  except Exception as exc:
    logger.warning("event=search_tracks_provider_error provider=youtube error=%s", exc)
    return []


@router.post("/search/playlists/spotify")
def search_playlists_spotify(
  req: SearchRequest,
  request: Request,
  db=Depends(get_db),
  _auth: str | None = Depends(require_user_or_open_access),
) -> list[dict]:
  """
  Search for playlists on Spotify using the query string.

  Args:
    req (SearchRequest): Request containing the search query.

  Returns:
    list[dict]: Spotify playlist objects.
  """
  spotify_external_id = _get_spotify_external_id(request, db)

  try:
    return spotify_lib.search_playlists(
      req.query, spotify_user_id=spotify_external_id
    )
  except Exception as exc:
    logger.warning(
      "event=search_playlists_provider_error provider=spotify error=%s", exc
    )
    return []


@router.post("/search/playlists/youtube")
def search_playlists_youtube(
  req: SearchRequest, _auth: str | None = Depends(require_user_or_open_access)
) -> list[dict]:
  """
  Search for playlists on YouTube using the query string.

  Args:
    req (SearchRequest): Request containing the search query.

  Returns:
    list[dict]: YouTube playlist objects.
  """
  try:
    return yt_music_lib.search_playlists(req.query)
  except Exception as exc:
    logger.warning(
      "event=search_playlists_provider_error provider=youtube error=%s", exc
    )
    return []


@router.get("/capabilities", tags=["capabilities"])
def capabilities() -> dict:
  """
  Return credential/capability state so clients can adapt UI behavior.
  """
  return {
    "spotify": bool(SPOTIFY_ENABLED),
    "youtube": bool(GOOGLE_CREDENTIALS_ACCESSIBLE),
  }
