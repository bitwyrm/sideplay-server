from fastapi import APIRouter
from pydantic import BaseModel
import spotify_lib
import yt_music_lib

router = APIRouter(tags=["search"])


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
  return yt_music_lib.search_songs(req.query)


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
  return {
    "spotify": spotify_lib.search_playlists(req.query),
    "youtube": yt_music_lib.search_playlists(req.query),
  }
