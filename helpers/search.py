from fastapi import APIRouter
from pydantic import BaseModel
import spotify_lib
import yt_music_lib
import logging

router = APIRouter(tags=["search"])
logger = logging.getLogger(__name__)


# -----------------------------
# Request models
# -----------------------------
class SearchRequest(BaseModel):
  query: str


# -----------------------------
# Endpoints
# -----------------------------
@router.post("/search/tracks")
def search_tracks(req: SearchRequest) -> list[dict]:
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
def search_playlists(req: SearchRequest) -> dict[str, list[dict]]:
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

  try:
    spotify_results = spotify_lib.search_playlists(req.query)
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
