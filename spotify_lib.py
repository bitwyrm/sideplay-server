import json
import os
import redis
import logging
from typing import Any, Optional

from spotipy import Spotify
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth
from helpers.setup import SPOTIFY_OAUTH_DIR
try:
  from spotify_scraper import SpotifyClient as SpotifyScraperClient
except Exception:
  SpotifyScraperClient = None

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# Paths and configuration
# -------------------------------------------------------------------

CREDENTIALS_FILE = os.environ.get("SPOTIFY_CREDENTIALS_FILE", "spotify-credentials.json")
SPOTIFY_DETAILS_TTL = 30 * 24 * 3600  # 30 days

# -------------------------------------------------------------------
# Redis setup
# -------------------------------------------------------------------

r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

def redis_set_json(key: str, value: dict, ttl: Optional[int] = None):
  r.set(key, json.dumps(value), ex=ttl if ttl else 0)

def redis_get_json(key: str) -> Optional[dict]:
  data = r.get(key)
  if not data:
    return None
  return json.loads(data)


def _spotify_track_url(track_id: str) -> str:
  return f"https://open.spotify.com/track/{track_id}"


def _spotify_playlist_url(playlist_id: str) -> str:
  return f"https://open.spotify.com/playlist/{playlist_id}"


def _parse_scraper_track(item: dict[str, Any]) -> Optional[dict]:
  if not item:
    return None

  # Some scraper responses wrap the track in {"track": {...}}.
  track = item.get("track") if isinstance(item, dict) else None
  if isinstance(track, dict):
    item = track

  track_id = item.get("id")
  title = item.get("name") or item.get("title")
  raw_artists = item.get("artists") or []
  artists: list[str] = []
  for artist in raw_artists:
    if isinstance(artist, dict):
      name = artist.get("name")
      if name:
        artists.append(name)
    elif isinstance(artist, str):
      artists.append(artist)

  album = ""
  raw_album = item.get("album")
  if isinstance(raw_album, dict):
    album = raw_album.get("name") or ""
  elif isinstance(raw_album, str):
    album = raw_album

  if not title or not artists:
    return None

  return {
    "track_id": track_id,
    "title": title,
    "artists": artists,
    "album": album,
  }


def _scraper_client() -> Optional[Any]:
  if SpotifyScraperClient is None:
    return None
  try:
    return SpotifyScraperClient()
  except Exception as exc:
    logger.warning("event=spotify_scraper_client_init_error error=%s", exc)
    return None


def _fallback_get_song_details(track_id: str) -> Optional[dict]:
  client = _scraper_client()
  if client is None:
    return None

  try:
    track = client.get_track_info(_spotify_track_url(track_id))
  except Exception as exc:
    logger.warning(
      "event=spotify_scraper_track_details_error track_id=%s error=%s", track_id, exc
    )
    return None

  parsed = _parse_scraper_track(track or {})
  if not parsed:
    return None
  return {"title": parsed["title"], "artists": parsed["artists"]}


def _fallback_get_playlist_songs(playlist_id: str) -> list:
  client = _scraper_client()
  if client is None:
    return []

  try:
    playlist = client.get_playlist_info(_spotify_playlist_url(playlist_id))
  except Exception as exc:
    logger.warning(
      "event=spotify_scraper_playlist_error playlist_id=%s error=%s", playlist_id, exc
    )
    return []

  items = ((playlist or {}).get("tracks") or {}).get("items") or []
  out = []
  for item in items:
    parsed = _parse_scraper_track(item)
    if parsed:
      out.append(parsed)
  return out

# -------------------------------------------------------------------
# Credentials
# -------------------------------------------------------------------

def load_spotify_credentials():
  if not os.path.exists(CREDENTIALS_FILE):
    raise FileNotFoundError(f"{CREDENTIALS_FILE} not found")

  with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
    creds = json.load(f)

  for key in ("client_id", "client_secret", "redirect_uri"):
    if key not in creds:
      raise KeyError(f"Missing '{key}' in {CREDENTIALS_FILE}")

  return creds["client_id"], creds["client_secret"], creds["redirect_uri"]

CLIENT_ID, CLIENT_SECRET, REDIRECT_URI = load_spotify_credentials()

# -------------------------------------------------------------------
# Spotify clients
# -------------------------------------------------------------------

def get_public_client() -> Spotify:
  """
  Shared public Spotify client (client credentials flow).
  """
  auth = SpotifyClientCredentials(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
  )
  return Spotify(auth_manager=auth)

def get_private_client(user_id: str) -> Spotify:
  """
  Backend-only authenticated Spotify client.

  Assumes OAuth already completed and token exists at:
      <SIDEPLAY_DATA_ROOT>/oauth/spotify/{user_id}.json
  """
  os.makedirs(SPOTIFY_OAUTH_DIR, exist_ok=True)
  token_path = os.path.join(SPOTIFY_OAUTH_DIR, f"{user_id}.json")

  if not os.path.exists(token_path):
    raise FileNotFoundError(f"Missing token file for user '{user_id}'")

  auth = SpotifyOAuth(
    client_id=CLIENT_ID,
    client_secret=CLIENT_SECRET,
    redirect_uri=REDIRECT_URI,
    scope="playlist-read-private playlist-read-collaborative",
    cache_path=token_path,
    open_browser=False,
  )

  client = Spotify(auth_manager=auth)

  # Defensive identity check (recommended)
  me = client.current_user()
  if me["id"] != user_id:
    raise RuntimeError(f"Token user mismatch: expected '{user_id}', got '{me['id']}'")

  return client

# -------------------------------------------------------------------
# Public catalog API
# -------------------------------------------------------------------

def search_playlists(query: str, client: Optional[Spotify] = None) -> dict:
  """
  Deterministic top-result PUBLIC playlist search.
  """
  client = client or get_public_client()

  res = client.search(q=query, type="playlist", limit=20)
  items = res.get("playlists", {}).get("items", [])

  if not items:
    return []

  return [
    {
      "playlist_id": pl["id"],
      "title": pl["name"],
    }
    for pl in items
    if pl
  ]

def get_song_details(track_id: str, client: Optional[Spotify] = None) -> Optional[dict]:
  """
  Retrieve the title and artists for a Spotify track ID.
  Uses Redis cache keyed by track_id.
  """
  key = f"spotify:track:{track_id}"
  cached = redis_get_json(key)
  if cached:
    return cached

  client = client or get_public_client()
  try:
    track = client.track(track_id)
  except Exception as exc:
    logger.warning(
      "event=spotify_api_track_details_error track_id=%s error=%s", track_id, exc
    )
    return _fallback_get_song_details(track_id)

  if not track:
    return None

  details = {
    "title": track.get("name"),
    "artists": [a.get("name") for a in track.get("artists", []) if a.get("name")],
  }

  # Store in Redis with TTL
  redis_set_json(key, details, ttl=SPOTIFY_DETAILS_TTL)
  return details

# -------------------------------------------------------------------
# Authenticated user API
# -------------------------------------------------------------------

def get_user_playlists(user_id: str) -> list:
  """
  Returns ALL playlists accessible to the user.
  """
  client = get_private_client(user_id)
  playlists = []

  results = client.current_user_playlists(limit=50)
  while True:
    for pl in results["items"]:
      playlists.append(
        {
          "id": pl["id"],
          "title": pl["name"],
        }
      )

    if not results["next"]:
      break
    results = client.next(results)

  return playlists

def get_playlist_songs(user_id: str, playlist_id: str) -> list:
  """
  Returns ALL tracks from a private or collaborative playlist.
  """
  try:
    client = get_private_client(user_id)
  except Exception as exc:
    logger.warning(
      "event=spotify_api_private_client_error user_id=%s playlist_id=%s error=%s",
      user_id,
      playlist_id,
      exc,
    )
    return _fallback_get_playlist_songs(playlist_id)
  tracks = []

  try:
    results = client.playlist_items(playlist_id, limit=100)
  except Exception as exc:
    logger.warning(
      "event=spotify_api_playlist_items_error user_id=%s playlist_id=%s error=%s",
      user_id,
      playlist_id,
      exc,
    )
    return _fallback_get_playlist_songs(playlist_id)
  while True:
    for item in results["items"]:
      track = item.get("track")
      if not track:
        continue

      tracks.append(
        {
          "track_id": track["id"],
          "title": track["name"],
          "artists": [a["name"] for a in track["artists"]],
          "album": track["album"]["name"],
        }
      )

    if not results["next"]:
      break
    try:
      results = client.next(results)
    except Exception as exc:
      logger.warning(
        "event=spotify_api_playlist_page_error user_id=%s playlist_id=%s error=%s",
        user_id,
        playlist_id,
        exc,
      )
      return _fallback_get_playlist_songs(playlist_id)

  return tracks

def get_playlist_songs_public(playlist_id: str) -> list:
  """
  Returns ALL tracks from a public playlist.
  """
  client = get_public_client()
  tracks = []

  try:
    results = client.playlist_items(playlist_id, limit=100)
  except Exception as exc:
    logger.warning(
      "event=spotify_api_public_playlist_items_error playlist_id=%s error=%s",
      playlist_id,
      exc,
    )
    return _fallback_get_playlist_songs(playlist_id)
  while True:
    for item in results["items"]:
      track = item.get("track")
      if not track:
        continue

      tracks.append(
        {
          "track_id": track["id"],
          "title": track["name"],
          "artists": [a["name"] for a in track["artists"]],
          "album": track["album"]["name"],
        }
      )

    if not results["next"]:
      break
    try:
      results = client.next(results)
    except Exception as exc:
      logger.warning(
        "event=spotify_api_public_playlist_page_error playlist_id=%s error=%s",
        playlist_id,
        exc,
      )
      return _fallback_get_playlist_songs(playlist_id)

  return tracks
