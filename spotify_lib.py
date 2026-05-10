import json
import os
import redis
import logging
import re
import inspect
from urllib.parse import quote, urlparse, parse_qs, unquote
from typing import Any, Optional
import requests
try:
  from ddgs import DDGS
except Exception:
  DDGS = None

from spotipy import Spotify
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth
from helpers.setup import SPOTIFY_OAUTH_DIR, SPOTIFY_ENABLED, SPOTIFY_CREDENTIALS_FILE
try:
  from spotify_scraper import SpotifyClient as SpotifyScraperClient
except Exception:
  try:
    from spotifyscraper import SpotifyClient as SpotifyScraperClient
  except Exception:
    SpotifyScraperClient = None

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# Paths and configuration
# -------------------------------------------------------------------

CREDENTIALS_FILE = SPOTIFY_CREDENTIALS_FILE
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


def _fallback_search_playlists(query: str) -> list[dict]:
  client = _scraper_client()
  if client is None:
    logger.warning(
      "event=spotify_scraper_playlist_search_no_client query=%s result_count=0",
      query,
    )
    return []

  raw = None
  call_attempts: list[tuple[str, tuple[Any, ...]]] = [
    ("search_playlists", (query,)),
    ("search", (query, "playlist")),
    ("search", (query,)),
    ("get_search_results", (query, "playlist")),
    ("get_search_results", (query,)),
  ]
  for method_name, args in call_attempts:
    method = getattr(client, method_name, None)
    if not callable(method):
      continue
    try:
      raw = method(*args)
      break
    except Exception as exc:
      logger.warning(
        "event=spotify_scraper_playlist_search_error method=%s query=%s error=%s",
        method_name,
        query,
        exc,
      )

  if raw is None:
    logger.warning(
      "event=spotify_scraper_playlist_search_no_raw query=%s result_count=0",
      query,
    )
    return []

  def _as_obj(v: Any) -> Any:
    if hasattr(v, "model_dump") and callable(v.model_dump):
      try:
        return v.model_dump()
      except Exception:
        pass
    if hasattr(v, "dict") and callable(v.dict):
      try:
        return v.dict()
      except Exception:
        pass
    if hasattr(v, "__dict__") and isinstance(getattr(v, "__dict__", None), dict):
      return dict(v.__dict__)
    return v

  def _extract_playlist_id(value: Any) -> Optional[str]:
    if value is None:
      return None
    s = str(value).strip()
    if not s:
      return None
    # spotify:playlist:<id>
    if s.startswith("spotify:playlist:"):
      return s.split(":")[-1]
    # https://open.spotify.com/playlist/<id>[?...]
    m = re.search(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)", s)
    if m:
      return m.group(1)
    # plain ID form
    if re.fullmatch(r"[A-Za-z0-9]{10,}", s):
      return s
    return None

  def _candidate_id_title(obj: Any) -> tuple[Optional[str], Optional[str]]:
    obj = _as_obj(obj)
    if not isinstance(obj, dict):
      return None, None

    pid = (
      _extract_playlist_id(obj.get("id"))
      or _extract_playlist_id(obj.get("playlist_id"))
      or _extract_playlist_id(obj.get("uri"))
      or _extract_playlist_id(obj.get("href"))
      or _extract_playlist_id(obj.get("url"))
      or _extract_playlist_id((obj.get("external_urls") or {}).get("spotify"))
    )
    title = obj.get("name") or obj.get("title")
    if title is not None:
      title = str(title)
    return pid, title

  def _walk_collect(node: Any, out: list[dict], seen: set[str]) -> None:
    node = _as_obj(node)
    if isinstance(node, dict):
      pid, title = _candidate_id_title(node)
      if pid and title and pid not in seen:
        seen.add(pid)
        out.append({"playlist_id": pid, "title": title})
      for v in node.values():
        _walk_collect(v, out, seen)
      return
    if isinstance(node, list):
      for item in node:
        _walk_collect(item, out, seen)

  out: list[dict] = []
  seen: set[str] = set()
  _walk_collect(raw, out, seen)
  logger.warning(
    "event=spotify_scraper_playlist_search_raw_summary query=%s raw_type=%s",
    query,
    type(raw).__name__,
  )
  logger.warning(
    "event=spotify_scraper_playlist_search_parsed query=%s count=%s",
    query,
    len(out),
  )
  return out


def iter_scraper_playlist_search(query: str):
  """
  Yield normalized playlist objects from spotifyscraper one-by-one.
  """
  client = _scraper_client()
  if client is None:
    logger.warning(
      "event=spotify_scraper_playlist_search_no_client query=%s result_count=0",
      query,
    )
    return

  raw = None
  call_attempts: list[tuple[str, tuple[Any, ...]]] = [
    ("search_playlists", (query,)),
    ("search", (query, "playlist")),
    ("search", (query,)),
    ("get_search_results", (query, "playlist")),
    ("get_search_results", (query,)),
  ]

  def _as_obj(v: Any) -> Any:
    if hasattr(v, "model_dump") and callable(v.model_dump):
      try:
        return v.model_dump()
      except Exception:
        pass
    if hasattr(v, "dict") and callable(v.dict):
      try:
        return v.dict()
      except Exception:
        pass
    if hasattr(v, "__dict__") and isinstance(getattr(v, "__dict__", None), dict):
      return dict(v.__dict__)
    return v

  def _extract_playlist_id(value: Any) -> Optional[str]:
    if value is None:
      return None
    s = str(value).strip()
    if not s:
      return None
    if s.startswith("spotify:playlist:"):
      return s.split(":")[-1]
    m = re.search(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)", s)
    if m:
      return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{10,}", s):
      return s
    return None

  def _walk(node: Any, seen: set[str]):
    node = _as_obj(node)
    if isinstance(node, dict):
      playlist_id = (
        _extract_playlist_id(node.get("id"))
        or _extract_playlist_id(node.get("playlist_id"))
        or _extract_playlist_id(node.get("uri"))
        or _extract_playlist_id(node.get("href"))
        or _extract_playlist_id(node.get("url"))
        or _extract_playlist_id((node.get("external_urls") or {}).get("spotify"))
      )
      title = node.get("name") or node.get("title")
      if playlist_id and title and playlist_id not in seen:
        seen.add(playlist_id)
        yield {"playlist_id": str(playlist_id), "title": str(title)}
      for v in node.values():
        yield from _walk(v, seen)
      return
    if isinstance(node, list):
      for item in node:
        yield from _walk(item, seen)

  # Preferred: paged one-by-one fetch when scraper supports it.
  for method_name, args in call_attempts:
    method = getattr(client, method_name, None)
    if not callable(method):
      continue
    try:
      sig = inspect.signature(method)
      params = set(sig.parameters.keys())
    except Exception:
      params = set()
    has_limit = "limit" in params
    has_offset = "offset" in params
    has_page = "page" in params
    if not has_limit or (not has_offset and not has_page):
      continue

    seen: set[str] = set()
    empty_streak = 0
    for i in range(0, 30):
      kwargs: dict[str, Any] = {"limit": 1}
      if has_offset:
        kwargs["offset"] = i
      else:
        kwargs["page"] = i + 1
      try:
        paged_raw = method(*args, **kwargs)
      except Exception as exc:
        logger.warning(
          "event=spotify_scraper_playlist_search_paged_error method=%s query=%s index=%s error=%s",
          method_name,
          query,
          i,
          exc,
        )
        break
      found_this_page = 0
      for item in _walk(paged_raw, seen):
        found_this_page += 1
        yield item
      if found_this_page == 0:
        empty_streak += 1
      else:
        empty_streak = 0
      if empty_streak >= 2:
        break
    if seen:
      return

  # Fallback: one-shot fetch.
  for method_name, args in call_attempts:
    method = getattr(client, method_name, None)
    if not callable(method):
      continue
    try:
      raw = method(*args)
      break
    except Exception as exc:
      logger.warning(
        "event=spotify_scraper_playlist_search_error method=%s query=%s error=%s",
        method_name,
        query,
        exc,
      )

  if raw is None:
    logger.warning(
      "event=spotify_scraper_playlist_search_no_raw query=%s result_count=0",
      query,
    )
    return

  seen: set[str] = set()
  for item in _walk(raw, seen):
    yield item


def _fallback_search_playlists_web(query: str, limit: int = 20) -> list[dict]:
  """
  Last-resort playlist discovery that does not use Spotify APIs.
  Uses a public HTML search page and extracts Spotify playlist URLs.
  """
  if not query.strip():
    return []

  # Primary web search path via package, fallback to HTML scraping below.
  if DDGS is not None:
    try:
      out: list[dict] = []
      seen: set[str] = set()
      scraper = _scraper_client()
      with DDGS() as ddgs:
        results = list(ddgs.text(f"site:open.spotify.com/playlist {query}", max_results=limit * 3))
      logger.warning(
        "event=spotify_web_playlist_search_ddgs_summary query=%s result_count=%s",
        query,
        len(results),
      )
      for item in results:
        href = str((item or {}).get("href") or (item or {}).get("url") or "")
        pid_match = re.search(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)", href)
        if not pid_match:
          continue
        playlist_id = pid_match.group(1)
        if playlist_id in seen:
          continue
        seen.add(playlist_id)

        title = str((item or {}).get("title") or f"Spotify Playlist {playlist_id[:8]}")
        if scraper is not None and hasattr(scraper, "get_playlist_info"):
          try:
            meta = scraper.get_playlist_info(_spotify_playlist_url(playlist_id))
            if isinstance(meta, dict):
              title = str(meta.get("name") or meta.get("title") or title)
          except Exception as exc:
            logger.warning(
              "event=spotify_web_playlist_enrich_error playlist_id=%s error=%s",
              playlist_id,
              exc,
            )
        out.append({"playlist_id": playlist_id, "title": title})
        if len(out) >= limit:
          break

      if out:
        logger.warning(
          "event=spotify_web_playlist_search_ddgs_parsed query=%s count=%s",
          query,
          len(out),
        )
        return out
    except Exception as exc:
      logger.warning("event=spotify_web_playlist_search_ddgs_error query=%s error=%s", query, exc)

  url = f"https://duckduckgo.com/html/?q={quote(f'site:open.spotify.com/playlist {query}')}"
  headers = {
    "User-Agent": (
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
  }
  try:
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    html = resp.text
  except Exception as exc:
    logger.warning("event=spotify_web_playlist_search_error query=%s error=%s", query, exc)
    return []

  # Extract title/url pairs from DDG result blocks.
  pairs = re.findall(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    html,
    flags=re.IGNORECASE | re.DOTALL,
  )

  out: list[dict] = []
  seen: set[str] = set()
  scraper = _scraper_client()
  logger.warning(
    "event=spotify_web_playlist_search_raw_summary query=%s pair_count=%s",
    query,
    len(pairs),
  )
  for href, raw_title in pairs:
    resolved_href = href
    # DuckDuckGo html results often wrap outbound URLs in /l/?...&uddg=<encoded_url>.
    if "duckduckgo.com/l/?" in href or href.startswith("/l/?"):
      try:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        if qs.get("uddg"):
          resolved_href = unquote(qs["uddg"][0])
      except Exception:
        pass

    pid_match = re.search(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)", resolved_href)
    if not pid_match:
      continue
    playlist_id = pid_match.group(1)
    if playlist_id in seen:
      continue
    seen.add(playlist_id)

    title = re.sub(r"<[^>]+>", "", raw_title or "").strip()
    title = re.sub(r"\s+", " ", title)
    if scraper is not None and hasattr(scraper, "get_playlist_info"):
      try:
        meta = scraper.get_playlist_info(_spotify_playlist_url(playlist_id))
        if isinstance(meta, dict):
          title = str(meta.get("name") or meta.get("title") or title)
      except Exception as exc:
        logger.warning(
          "event=spotify_web_playlist_enrich_error playlist_id=%s error=%s",
          playlist_id,
          exc,
        )
    if not title:
      title = f"Spotify Playlist {playlist_id[:8]}"

    out.append({"playlist_id": playlist_id, "title": title})
    if len(out) >= limit:
      break

  # If DDG result blocks are missing, fall back to raw HTML ID extraction.
  if not out:
    decoded_html = unquote(html)
    raw_ids = set(
      re.findall(r"open\.spotify\.com/playlist/([A-Za-z0-9]{10,})", decoded_html)
    )
    logger.warning(
      "event=spotify_web_playlist_search_raw_id_scan query=%s id_count=%s",
      query,
      len(raw_ids),
    )
    for playlist_id in raw_ids:
      if playlist_id in seen:
        continue
      seen.add(playlist_id)
      title = f"Spotify Playlist {playlist_id[:8]}"
      if scraper is not None and hasattr(scraper, "get_playlist_info"):
        try:
          meta = scraper.get_playlist_info(_spotify_playlist_url(playlist_id))
          if isinstance(meta, dict):
            title = str(meta.get("name") or meta.get("title") or title)
        except Exception as exc:
          logger.warning(
            "event=spotify_web_playlist_enrich_error playlist_id=%s error=%s",
            playlist_id,
            exc,
          )
      out.append({"playlist_id": playlist_id, "title": title})
      if len(out) >= limit:
        break

  logger.warning(
    "event=spotify_web_playlist_search_parsed query=%s count=%s",
    query,
    len(out),
  )
  return out

# -------------------------------------------------------------------
# Credentials
# -------------------------------------------------------------------

def load_spotify_credentials() -> tuple[Optional[str], Optional[str], Optional[str]]:
  if not SPOTIFY_ENABLED:
    return None, None, None
  if not os.path.exists(CREDENTIALS_FILE):
    return None, None, None

  with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
    creds = json.load(f)

  return (
    creds.get("client_id"),
    creds.get("client_secret"),
    creds.get("redirect_uri"),
  )

CLIENT_ID, CLIENT_SECRET, REDIRECT_URI = load_spotify_credentials()
HAS_SPOTIFY_CREDS = bool(CLIENT_ID and CLIENT_SECRET and REDIRECT_URI)

# -------------------------------------------------------------------
# Spotify clients
# -------------------------------------------------------------------

def get_public_client() -> Spotify:
  """
  Shared public Spotify client (client credentials flow).
  """
  if not HAS_SPOTIFY_CREDS:
    raise RuntimeError("spotify_credentials_unavailable")
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
  if not HAS_SPOTIFY_CREDS:
    raise RuntimeError("spotify_credentials_unavailable")
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

def search_playlists(
  query: str, client: Optional[Spotify] = None, spotify_user_id: Optional[str] = None
) -> list[dict]:
  """
  Deterministic top-result PUBLIC playlist search.
  """
  items = []
  tried_private = False

  if HAS_SPOTIFY_CREDS and client is None and spotify_user_id:
    tried_private = True
    try:
      private_client = get_private_client(spotify_user_id)
      res = private_client.search(q=query, type="playlist", limit=20, market="from_token")
      items = res.get("playlists", {}).get("items", [])
      logger.info(
        "event=spotify_api_private_playlist_search_results query=%s spotify_user_id=%s count=%s",
        query,
        spotify_user_id,
        len(items),
      )
    except Exception as exc:
      logger.warning(
        "event=spotify_api_private_playlist_search_error spotify_user_id=%s query=%s error=%s",
        spotify_user_id,
        query,
        exc,
      )

  if HAS_SPOTIFY_CREDS and not items:
    try:
      client = client or get_public_client()
      res = client.search(q=query, type="playlist", limit=20, market="US")
      items = res.get("playlists", {}).get("items", [])
      logger.info(
        "event=spotify_api_public_playlist_search_results query=%s count=%s",
        query,
        len(items),
      )
    except Exception as exc:
      logger.warning(
        "event=spotify_api_playlist_search_error query=%s error=%s", query, exc
      )
      scraper_items = _fallback_search_playlists(query)
      if scraper_items:
        logger.warning(
          "event=spotify_playlist_search_scraper_fallback_used query=%s count=%s",
          query,
          len(scraper_items),
        )
        return scraper_items
      web_items = _fallback_search_playlists_web(query)
      if web_items:
        logger.warning(
          "event=spotify_playlist_search_web_fallback_used query=%s count=%s",
          query,
          len(web_items),
        )
        return web_items
      return []

  if not items:
    if tried_private:
      logger.warning(
        "event=spotify_playlist_search_empty_after_private_and_public query=%s spotify_user_id=%s",
        query,
        spotify_user_id,
      )
    scraper_items = _fallback_search_playlists(query)
    if scraper_items:
      logger.warning(
        "event=spotify_playlist_search_scraper_fallback_used query=%s count=%s",
        query,
        len(scraper_items),
      )
      return scraper_items
    web_items = _fallback_search_playlists_web(query)
    if web_items:
      logger.warning(
        "event=spotify_playlist_search_web_fallback_used query=%s count=%s",
        query,
        len(web_items),
      )
      return web_items
    return []

  final_items = [
    {
      "playlist_id": pl["id"],
      "title": pl["name"],
    }
    for pl in items
    if pl
  ]
  logger.warning(
    "event=spotify_playlist_search_final_source source=spotipy query=%s count=%s",
    query,
    len(final_items),
  )
  return final_items

def get_song_details(track_id: str, client: Optional[Spotify] = None) -> Optional[dict]:
  """
  Retrieve the title and artists for a Spotify track ID.
  Uses Redis cache keyed by track_id.
  """
  key = f"spotify:track:{track_id}"
  cached = redis_get_json(key)
  if cached:
    return cached

  if not HAS_SPOTIFY_CREDS:
    return _fallback_get_song_details(track_id)

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
  if not HAS_SPOTIFY_CREDS:
    return []
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
  if not HAS_SPOTIFY_CREDS:
    return _fallback_get_playlist_songs(playlist_id)
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
  if not HAS_SPOTIFY_CREDS:
    return _fallback_get_playlist_songs(playlist_id)
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


def get_playlist_info_public(playlist_id: str) -> Optional[dict]:
  """
  Resolve basic public playlist metadata without requiring a linked account.
  Returns: {"id": str, "title": str} or None.
  """
  if HAS_SPOTIFY_CREDS:
    try:
      pl = get_public_client().playlist(playlist_id, fields="id,name")
      if pl and pl.get("id") and pl.get("name"):
        return {"id": pl["id"], "title": pl["name"]}
    except Exception as exc:
      logger.warning(
        "event=spotify_public_playlist_info_spotipy_error playlist_id=%s error=%s",
        playlist_id,
        exc,
      )

  scraper = _scraper_client()
  if scraper is None or not hasattr(scraper, "get_playlist_info"):
    return None
  try:
    meta = scraper.get_playlist_info(_spotify_playlist_url(playlist_id))
  except Exception as exc:
    logger.warning(
      "event=spotify_public_playlist_info_scraper_error playlist_id=%s error=%s",
      playlist_id,
      exc,
    )
    return None
  if not isinstance(meta, dict):
    return None
  title = meta.get("name") or meta.get("title")
  if not title:
    return None
  return {"id": playlist_id, "title": str(title)}
