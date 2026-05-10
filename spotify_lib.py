import json
import os
import redis
from typing import Optional

from spotipy import Spotify
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth
from helpers.setup import SPOTIFY_OAUTH_DIR

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
  except Exception:
    return None

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
  client = get_private_client(user_id)
  tracks = []

  results = client.playlist_items(playlist_id, limit=100)
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
    results = client.next(results)

  return tracks

def get_playlist_songs_public(playlist_id: str) -> list:
  """
  Returns ALL tracks from a public playlist.
  """
  client = get_public_client()
  tracks = []

  results = client.playlist_items(playlist_id, limit=100)
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
    results = client.next(results)

  return tracks
