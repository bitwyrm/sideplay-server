from fastapi import APIRouter, Request
from pydantic import BaseModel
import spotify_lib
import yt_music_lib
import logging
import json
import asyncio
from helpers.accounts import get_user_from_session, require_user_or_open_access
from helpers.db import get_db
from fastapi import Depends
from fastapi.responses import StreamingResponse
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


@router.post("/search/playlists")
def search_playlists(
  req: SearchRequest,
  request: Request,
  db=Depends(get_db),
  _auth: str | None = Depends(require_user_or_open_access),
) -> dict[str, list[dict]]:
  """
  Search for playlists on both Spotify and YouTube using the query string.

  Args:
    req (SearchRequest): Request containing the search query.

  Returns:
    dict: {
      "spotify": list of Spotify playlist objects,
      "youtube": list of YouTube playlist objects
    }
  """
  spotify_results: list[dict] = []
  youtube_results: list[dict] = []
  spotify_external_id = _get_spotify_external_id(request, db)

  try:
    spotify_results = spotify_lib.search_playlists(
      req.query, spotify_user_id=spotify_external_id
    )
  except Exception as exc:
    logger.warning(
      "event=search_playlists_provider_error provider=spotify error=%s", exc
    )

  try:
    youtube_results = yt_music_lib.search_playlists(req.query)
  except Exception as exc:
    logger.warning(
      "event=search_playlists_provider_error provider=youtube error=%s", exc
    )

  return {"spotify": spotify_results, "youtube": youtube_results}


@router.post("/search/playlists/stream")
def search_playlists_stream(
  req: SearchRequest,
  request: Request,
  db=Depends(get_db),
  _auth: str | None = Depends(require_user_or_open_access),
) -> StreamingResponse:
  """
  Stream playlist search results as JSONL with exactly two provider chunks.

  Request body:
    {"query": "<string>"}

  Response (application/x-ndjson):
    Line 1: {"youtube": [ { "playlist_id": str, "title": str }, ... ]}
    Line 2: {"spotify": [ { "playlist_id": str, "title": str }, ... ]}

  Notes:
    - If provider search fails, that provider line is emitted as an empty list.
    - Ordering is always YouTube first, Spotify second.
  """
  spotify_external_id = _get_spotify_external_id(request, db)

  async def gen():
    youtube_results: list[dict] = []
    try:
      youtube_results = yt_music_lib.search_playlists(req.query)
      yield json.dumps({"youtube": youtube_results}, indent=2, ensure_ascii=False) + "\n"
    except Exception as exc:
      logger.warning(
        "event=search_playlists_provider_error provider=youtube error=%s", exc
      )
      yield json.dumps({"youtube": []}, indent=2, ensure_ascii=False) + "\n"

    spotify_results: list[dict] = []
    try:
      spotify_results = spotify_lib.search_playlists(
        req.query, spotify_user_id=spotify_external_id
      )
      yield json.dumps({"spotify": spotify_results}, indent=2, ensure_ascii=False) + "\n"
    except Exception as exc:
      logger.warning(
        "event=search_playlists_provider_error provider=spotify error=%s", exc
      )
      yield json.dumps({"spotify": []}, indent=2, ensure_ascii=False) + "\n"

  return StreamingResponse(
    gen(),
    media_type="application/x-ndjson",
    headers={
      "Cache-Control": "no-cache",
      "X-Accel-Buffering": "no",
    },
  )


@router.get("/capabilities")
def capabilities() -> dict:
  """
  Return credential/capability state so clients can adapt UI behavior.

  Response shape:
    {
      "spotify": bool,
      "youtube": bool
    }
  """
  return {
    "spotify": bool(SPOTIFY_ENABLED),
    "youtube": bool(GOOGLE_CREDENTIALS_ACCESSIBLE),
  }
