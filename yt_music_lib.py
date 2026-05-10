"""
yt_music_lib.py

A YouTube Music access and resolution library built on ytmusicapi.

It provides:
- Public song search (YT Music-only filters)
- Authenticated access to user playlists (via Google OAuth Web tokens)
- Retrieval of playlist tracks
- Deterministic normalization of (artist, title) pairs to YouTube video IDs

All persistent state is stored on disk and refreshed via external scheduling.
"""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import redis
import requests
from ytmusicapi import YTMusic
from helpers.setup import GOOGLE_OAUTH_DIR, NORMALIZE_MAP_PATH

# -------------------------------------------------------------------
# Paths and configuration
# -------------------------------------------------------------------

headers_path = "headers.json"
google_creds_path = "google-credentials.json"

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

cache_dir = Path(GOOGLE_OAUTH_DIR)
cache_dir.mkdir(parents=True, exist_ok=True)

normalize_map_path = Path(NORMALIZE_MAP_PATH)

YOUTUBE_CACHE_TTL = 30 * 24 * 3600  # 30 days
SEARCH_SONGS_TTL = 6 * 3600  # 6 hours

r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

# -------------------------------------------------------------------
# Public client cache (ytmusicapi)
# -------------------------------------------------------------------

_public_client: Optional[YTMusic] = None


def get_public_client() -> YTMusic:
  """
  Return a lazily initialized public YTMusic client.
  """
  global _public_client
  if _public_client is None:
    _public_client = YTMusic(headers_path)
  return _public_client


# -------------------------------------------------------------------
# Persistence helpers
# -------------------------------------------------------------------


def load_json(path: Path) -> dict:
  """
  Load a JSON file into memory.
  """
  if path.exists():
    with path.open("r", encoding="utf-8") as f:
      return json.load(f)
  return {}


def save_json(path: Path, data: dict) -> None:
  """
  Persist a dictionary as formatted JSON.
  """
  with path.open("w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)


def redis_set_json(key: str, value: dict, ttl: Optional[int] = None):
  r.set(key, json.dumps(value), ex=ttl if ttl else 0)


def redis_get_json(key: str) -> Optional[dict]:
  data = r.get(key)
  if not data:
    return None
  return json.loads(data)


def normalize_text(value: str) -> str:
  """
  Normalize text for dictionary keys using Unicode-aware case folding.
  """
  return value.casefold().strip()


# -------------------------------------------------------------------
# Time helpers
# -------------------------------------------------------------------


def now_iso() -> str:
  """
  Return the current UTC timestamp in ISO 8601 format.
  """
  return datetime.now(timezone.utc).isoformat()


def needs_reresolve(resolved_at: str, days: int) -> bool:
  """
  Determine whether a resolution timestamp is older than the allowed window.
  """
  try:
    ts = datetime.fromisoformat(resolved_at)
  except Exception:
    return True
  return datetime.now(timezone.utc) - ts > timedelta(days=days)


# -------------------------------------------------------------------
# OAuth helpers (Google Web Application tokens)
# -------------------------------------------------------------------


def refresh_access_token(sub: str) -> dict:
  token_path = cache_dir / f"{sub}.json"
  token_data = load_json(token_path)

  refresh_token = token_data.get("refresh_token")
  if not refresh_token:
    raise RuntimeError(f"Missing refresh_token for sub={sub}")

  creds = load_json(Path(google_creds_path))["web"]

  resp = requests.post(
    OAUTH_TOKEN_URL,
    data={
      "client_id": creds["client_id"],
      "client_secret": creds["client_secret"],
      "refresh_token": refresh_token,
      "grant_type": "refresh_token",
    },
    timeout=10,
  )
  resp.raise_for_status()

  payload = resp.json()
  token_data["access_token"] = payload["access_token"]
  token_data["expires_at"] = (
    datetime.now(timezone.utc) + timedelta(seconds=payload["expires_in"])
  ).isoformat()

  save_json(token_path, token_data)
  return token_data


def get_valid_access_token(sub: str) -> str:
  token_path = cache_dir / f"{sub}.json"
  token_data = load_json(token_path)

  expires_at = token_data.get("expires_at")
  if not expires_at or needs_reresolve(expires_at, 0):
    token_data = refresh_access_token(sub)

  return token_data["access_token"]


def auth_headers(sub: str) -> dict:
  return {"Authorization": f"Bearer {get_valid_access_token(sub)}"}


# -------------------------------------------------------------------
# Public catalog API (ytmusicapi)
# -------------------------------------------------------------------


def search_songs(query: str) -> List[Dict]:
  key = f"ytmusic:search:{query.lower().strip()}"
  cached = redis_get_json(key)
  if cached:
    return cached

  client = get_public_client()
  results = client.search(query=query, filter="songs")

  songs = [
    {
      "title": item.get("title"),
      "youtube_id": item.get("videoId"),
      "artists": [a.get("name") for a in item.get("artists", []) if a.get("name")],
    }
    for item in results
    if item and item.get("videoId")
  ]

  redis_set_json(key, songs, ttl=SEARCH_SONGS_TTL)
  return songs


def search_playlists(query: str) -> List[Dict]:
  """
  Search YouTube Music for public playlists matching a query.
  """
  results = get_public_client().search(query=query, filter="playlists")

  return [
    {"title": item.get("title"), "playlist_id": item.get("browseId")}
    for item in results
    if item and item.get("browseId")
  ]


def map_video_to_query(video_id: str) -> Optional[str]:
  """
  Map a YouTube video ID to a 'title by author' query string.
  Cache in Redis.
  """
  key = f"ytmusic:video:{video_id}"
  cached = redis_get_json(key)
  if cached and not needs_reresolve(cached.get("cached_at", ""), 30):
    return cached.get("query")

  client = get_public_client()
  try:
    song_data = client.get_song(video_id)
  except Exception:
    return None

  if not song_data:
    return None

  vd = song_data.get("videoDetails", {})
  title = vd.get("title")
  author = vd.get("author")

  if not title:
    return None

  query = f"{title} by {author}" if author else title

  # Store in Redis
  redis_set_json(key, {"query": query, "cached_at": now_iso()}, ttl=YOUTUBE_CACHE_TTL)
  return query


def map_query_to_song(query: str) -> Optional[dict]:
  """
  Map a query string to a YouTube Music song.
  Returns: { youtube_id, title, artists }
  """
  key = f"ytmusic:query:{query.lower().strip()}"
  cached = redis_get_json(key)
  if cached and not needs_reresolve(cached.get("cached_at", ""), 30):
    return cached.get("song")

  client = get_public_client()
  results = client.search(query=query, filter="songs", limit=1)
  if not results:
    song = None
  else:
    r = results[0]
    song = {
      "youtube_id": r.get("videoId"),
      "title": r.get("title"),
      "artists": [a.get("name") for a in r.get("artists", []) if a.get("name")],
    }

  redis_set_json(
    key,
    {
      "song": song,
      "cached_at": now_iso(),
    },
    ttl=YOUTUBE_CACHE_TTL,
  )
  return song


# -------------------------------------------------------------------
# Authenticated user API
# -------------------------------------------------------------------


def get_user_playlists(sub: str) -> List[Dict]:
  """
  Retrieve all playlists created by the authenticated user (by sub).
  """
  playlists: List[Dict] = []
  page_token: Optional[str] = None

  while True:
    params = {
      "part": "snippet,contentDetails",
      "mine": "true",
      "maxResults": 50,
    }
    if page_token:
      params["pageToken"] = page_token

    resp = requests.get(
      f"{YOUTUBE_API_BASE}/playlists",
      headers=auth_headers(sub),
      params=params,
      timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    playlists.extend(data.get("items", []))
    page_token = data.get("nextPageToken")

    if not page_token:
      break

  for i in range(len(playlists)):
    playlists[i] = {"id": playlists[i]["id"], "title": playlists[i]["snippet"]["title"]}
  return playlists


def get_playlist_songs(sub: str, playlist_id: str) -> List[str]:
  """
  Retrieve all YouTube video IDs from a playlist for the given user sub.
  """
  tracks: List[str] = []
  page_token: Optional[str] = None

  while True:
    params = {
      "part": "snippet,contentDetails",
      "playlistId": playlist_id,
      "maxResults": 100,
    }
    if page_token:
      params["pageToken"] = page_token

    resp = requests.get(
      f"{YOUTUBE_API_BASE}/playlistItems",
      headers=auth_headers(sub),
      params=params,
      timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    for item in data.get("items", []):
      video_id = item.get("contentDetails", {}).get("videoId") or item.get(
        "snippet", {}
      ).get("resourceId", {}).get("videoId")
      if video_id:
        tracks.append(video_id)

    page_token = data.get("nextPageToken")
    if not page_token:
      break

  return tracks


def get_playlist_songs_public(playlist_id: str) -> List[Dict]:
  """
  Retrieve all tracks from a public playlist.
  """
  playlist = get_public_client().get_playlist(playlist_id, limit=None)
  return playlist.get("tracks", [])
