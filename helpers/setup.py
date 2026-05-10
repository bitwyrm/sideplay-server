import json
import os
import redis
from datetime import datetime, timezone
from typing import Optional

DATA_ROOT = os.path.abspath(os.environ.get("SIDEPLAY_DATA_ROOT", "./data"))
SPOTIFY_CREDENTIALS_FILE = os.environ.get(
  "SPOTIFY_CREDENTIALS_FILE", "spotify-credentials.json"
)
GOOGLE_CREDENTIALS_FILE = os.environ.get(
  "GOOGLE_CREDENTIALS_FILE", "google-credentials.json"
)
YT_HEADERS_FILE = os.environ.get("YT_HEADERS_FILE", "headers.json")
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "cookies.txt")

DB_PATH = os.path.join(DATA_ROOT, "sideplay.sqlite")
MEDIA_DIR = os.path.join(DATA_ROOT, "media")
THUMB_DIR = os.path.join(DATA_ROOT, "thumbs")
SPOTIFY_OAUTH_DIR = os.path.join(DATA_ROOT, "oauth", "spotify")
GOOGLE_OAUTH_DIR = os.path.join(DATA_ROOT, "oauth", "google")
NORMALIZE_MAP_PATH = os.path.join(DATA_ROOT, "normalize_map.json")
STATIC_DIR = "static"

SPOTIFY_SCOPES = "playlist-read-private playlist-read-collaborative"
YOUTUBE_SCOPES = [
  "https://www.googleapis.com/auth/youtube.readonly",
  "openid",
]

with open(SPOTIFY_CREDENTIALS_FILE) as f:
  SPOTIFY_CREDS = json.load(f)

SPOTIFY_CLIENT_ID = SPOTIFY_CREDS["client_id"]
SPOTIFY_CLIENT_SECRET = SPOTIFY_CREDS["client_secret"]
SPOTIFY_REDIRECT = SPOTIFY_CREDS["redirect_uri"]

YOUTUBE_REDIRECT = "https://sideplay.endothermic-dragon.dev/oauth/youtube/callback"

SESSION_HOURS = 24
PLAYLIST_CACHE_TTL = 30 * 24 * 3600  # 30 days

r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

for dir_path in (
  DATA_ROOT,
  MEDIA_DIR,
  THUMB_DIR,
  SPOTIFY_OAUTH_DIR,
  GOOGLE_OAUTH_DIR,
):
  os.makedirs(dir_path, exist_ok=True)

def now_iso() -> str:
  """Return the current UTC time as an ISO 8601 string."""
  return datetime.now(timezone.utc).isoformat()

def redis_set_json(key: str, value: dict, ttl: Optional[int] = None) -> None:
  """
  Serialize and cache a dictionary in Redis.

  Args:
    key (str): Redis cache key.
    value (dict): JSON-serializable value.
    ttl (Optional[int]): Expiration in seconds. If omitted, key is persistent.

  Returns:
    None
  """
  r.set(key, json.dumps(value), ex=ttl if ttl else 0)

def redis_get_json(key: str) -> Optional[dict]:
  """
  Read and deserialize a JSON dictionary from Redis.

  Args:
    key (str): Redis cache key.

  Returns:
    Optional[dict]: Parsed object if present, otherwise None.
  """
  data = r.get(key)
  if not data:
    return None
  return json.loads(data)
