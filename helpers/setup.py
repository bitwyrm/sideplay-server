import json
import os
import redis
import logging
from datetime import datetime, timezone
from typing import Optional

DATA_ROOT = os.path.abspath(os.environ.get("SIDEPLAY_DATA_ROOT", "./data"))
logger = logging.getLogger(__name__)

SPOTIFY_CREDENTIALS_ENV = os.environ.get("SPOTIFY_CREDENTIALS_FILE")
GOOGLE_CREDENTIALS_ENV = os.environ.get("GOOGLE_CREDENTIALS_FILE")
YT_HEADERS_ENV = os.environ.get("YT_HEADERS_FILE")

SPOTIFY_CREDENTIALS_FILE = SPOTIFY_CREDENTIALS_ENV or "spotify-credentials.json"
GOOGLE_CREDENTIALS_FILE = GOOGLE_CREDENTIALS_ENV or "google-credentials.json"
YT_HEADERS_FILE = YT_HEADERS_ENV or "headers.json"
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "cookies.txt")
OPEN_ACCESS_ENV = os.environ.get("OPEN_ACCESS")

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

def _is_readable_file(path: str) -> bool:
  return os.path.isfile(path) and os.access(path, os.R_OK)


SPOTIFY_CREDENTIALS_ACCESSIBLE = bool(
  SPOTIFY_CREDENTIALS_ENV and _is_readable_file(SPOTIFY_CREDENTIALS_FILE)
)

SPOTIFY_CREDS = {}
if SPOTIFY_CREDENTIALS_ACCESSIBLE:
  with open(SPOTIFY_CREDENTIALS_FILE) as f:
    SPOTIFY_CREDS = json.load(f)

SPOTIFY_CLIENT_ID = SPOTIFY_CREDS.get("client_id")
SPOTIFY_CLIENT_SECRET = SPOTIFY_CREDS.get("client_secret")
SPOTIFY_REDIRECT = SPOTIFY_CREDS.get("redirect_uri")
SPOTIFY_ENABLED = bool(
  SPOTIFY_CREDENTIALS_ACCESSIBLE
  and SPOTIFY_CLIENT_ID
  and SPOTIFY_CLIENT_SECRET
  and SPOTIFY_REDIRECT
)
GOOGLE_CREDENTIALS_ACCESSIBLE = bool(
  GOOGLE_CREDENTIALS_ENV and _is_readable_file(GOOGLE_CREDENTIALS_FILE)
)
YT_HEADERS_ACCESSIBLE = bool(YT_HEADERS_ENV and _is_readable_file(YT_HEADERS_FILE))

if not GOOGLE_CREDENTIALS_ENV:
  logger.warning(
    "event=youtube_google_credentials_env_missing var=GOOGLE_CREDENTIALS_FILE"
  )
elif not GOOGLE_CREDENTIALS_ACCESSIBLE:
  logger.warning(
    "event=youtube_google_credentials_inaccessible path=%s",
    GOOGLE_CREDENTIALS_FILE,
  )

if not YT_HEADERS_ENV:
  logger.warning("event=youtube_headers_env_missing var=YT_HEADERS_FILE")
elif not YT_HEADERS_ACCESSIBLE:
  logger.warning("event=youtube_headers_inaccessible path=%s", YT_HEADERS_FILE)

if not SPOTIFY_CREDENTIALS_ENV:
  logger.warning(
    "event=spotify_credentials_env_missing var=SPOTIFY_CREDENTIALS_FILE"
  )
elif not SPOTIFY_CREDENTIALS_ACCESSIBLE:
  logger.warning(
    "event=spotify_credentials_inaccessible path=%s",
    SPOTIFY_CREDENTIALS_FILE,
  )

YOUTUBE_REDIRECT = "https://sideplay.endothermic-dragon.dev/oauth/youtube/callback"


def _env_bool(value: str | None, default: bool = False) -> bool:
  if value is None:
    return default
  return value.strip().lower() in ("1", "true", "yes", "on")


OPEN_ACCESS = _env_bool(OPEN_ACCESS_ENV, default=False)

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
