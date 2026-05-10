import json
import os
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheFileHandler
from google_auth_oauthlib.flow import Flow
from google.oauth2 import id_token
from google.auth.transport.requests import Request as GoogleRequest

from helpers.db import get_db
from helpers.accounts import get_user_from_session, require_user
from helpers.setup import (
  GOOGLE_CREDENTIALS_FILE,
  GOOGLE_CREDENTIALS_ACCESSIBLE,
  GOOGLE_OAUTH_DIR,
  SPOTIFY_CLIENT_ID,
  SPOTIFY_CLIENT_SECRET,
  SPOTIFY_ENABLED,
  SPOTIFY_OAUTH_DIR,
  SPOTIFY_REDIRECT,
  SPOTIFY_SCOPES,
  YOUTUBE_REDIRECT,
  YOUTUBE_SCOPES,
)
from pydantic import BaseModel

# ----------------------------
# Linked accounts
# ----------------------------


class UnlinkAccountRequest(BaseModel):
  """Request body to unlink an external account from the current user."""

  provider: str
  external_id: str


router = APIRouter(tags=["oauth"])

# -------------------------------------------------------------------
# LINKED ACCOUNTS
# -------------------------------------------------------------------


@router.get("/oauth/spotify/init", response_model=None)
def spotify_start(token: str, db=Depends(get_db)) -> RedirectResponse:
  """
  Initiate Spotify OAuth flow for linking a Spotify account.

  Args:
    token (str): Session token of the current user.
    db: Database connection.

  Raises:
    HTTPException 401: If session token is invalid.

  Returns:
    RedirectResponse: URL to Spotify's OAuth authorization page.
  """
  user_id = get_user_from_session(token, db)
  if not user_id:
    raise HTTPException(401)
  if not SPOTIFY_ENABLED:
    raise HTTPException(503, "Spotify linking is disabled: missing spotify credentials")

  auth = SpotifyOAuth(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET,
    scope=SPOTIFY_SCOPES,
    redirect_uri=SPOTIFY_REDIRECT,
    state=token,
    open_browser=False,
  )

  return RedirectResponse(auth.get_authorize_url())


@router.get("/oauth/spotify/callback", response_model=None)
def spotify_callback(code: str, state: str, db=Depends(get_db)) -> RedirectResponse:
  """
  Callback endpoint for Spotify OAuth. Exchanges code for access token and
  stores the linked Spotify account in the database.

  Args:
    code (str): Authorization code returned by Spotify.
    state (str): Original session token passed to Spotify OAuth.
    db: Database connection.

  Raises:
    HTTPException 401: If session token is invalid.

  Returns:
    RedirectResponse: Redirects to the main GUI page.
  """
  user_id = get_user_from_session(state, db)
  if not user_id:
    raise HTTPException(401)
  if not SPOTIFY_ENABLED:
    raise HTTPException(503, "Spotify linking is disabled: missing spotify credentials")

  os.makedirs(SPOTIFY_OAUTH_DIR, exist_ok=True)

  auth = SpotifyOAuth(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET,
    scope=SPOTIFY_SCOPES,
    redirect_uri=SPOTIFY_REDIRECT,
    cache_handler=None,
  )

  token_info = auth.get_access_token(code, as_dict=True)
  sp = Spotify(auth=token_info["access_token"])
  spotify_id = sp.current_user()["id"]

  cache_handler = CacheFileHandler(
    cache_path=os.path.join(SPOTIFY_OAUTH_DIR, f"{spotify_id}.json")
  )
  cache_handler.save_token_to_cache(token_info)

  db.execute(
    "INSERT OR IGNORE INTO accounts (user_id, provider, external_id) VALUES (?, 'spotify', ?)",
    (user_id, spotify_id),
  )
  db.commit()

  return RedirectResponse("/")


@router.get("/oauth/youtube/init", response_model=None)
def youtube_start(token: str, db=Depends(get_db)) -> RedirectResponse:
  """
  Initiate YouTube OAuth flow for linking a Google account.

  Args:
    token (str): Session token of the current user.
    db: Database connection.

  Raises:
    HTTPException 401: If session token is invalid.

  Returns:
    RedirectResponse: URL to Google's OAuth authorization page.
  """
  user_id = get_user_from_session(token, db)
  if not user_id:
    raise HTTPException(401)
  if not GOOGLE_CREDENTIALS_ACCESSIBLE:
    raise HTTPException(503, "YouTube linking is disabled: google credentials unavailable")

  flow = Flow.from_client_secrets_file(
    GOOGLE_CREDENTIALS_FILE,
    scopes=YOUTUBE_SCOPES,
    redirect_uri=YOUTUBE_REDIRECT,
  )

  url, _ = flow.authorization_url(
    access_type="offline",
    include_granted_scopes="true",
    prompt="consent",
    state=token,
  )

  return RedirectResponse(url)


@router.get("/oauth/youtube/callback", response_model=None)
def youtube_callback(code: str, state: str, db=Depends(get_db)) -> RedirectResponse:
  """
  Callback endpoint for YouTube OAuth. Exchanges code for credentials,
  saves tokens locally, and links YouTube account in the database.

  Args:
    code (str): Authorization code returned by Google.
    state (str): Original session token passed to Google OAuth.
    db: Database connection.

  Raises:
    HTTPException 401: If session token is invalid.

  Returns:
    RedirectResponse: Redirects to the main GUI page.
  """
  user_id = get_user_from_session(state, db)
  if not user_id:
    raise HTTPException(401)
  if not GOOGLE_CREDENTIALS_ACCESSIBLE:
    raise HTTPException(503, "YouTube linking is disabled: google credentials unavailable")

  flow = Flow.from_client_secrets_file(
    GOOGLE_CREDENTIALS_FILE,
    scopes=YOUTUBE_SCOPES,
    redirect_uri=YOUTUBE_REDIRECT,
  )

  flow.fetch_token(code=code)
  creds = flow.credentials

  info = id_token.verify_oauth2_token(
    creds.id_token,
    GoogleRequest(),
    audience=creds.client_id,
  )

  youtube_sub = info["sub"]

  os.makedirs(GOOGLE_OAUTH_DIR, exist_ok=True)
  with open(os.path.join(GOOGLE_OAUTH_DIR, f"{youtube_sub}.json"), "w") as f:
    json.dump(
      {
        "refresh_token": creds.refresh_token,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "token_uri": creds.token_uri,
      },
      f,
      indent=2,
    )

  db.execute(
    "INSERT OR IGNORE INTO accounts (user_id, provider, external_id) VALUES (?, 'youtube', ?)",
    (user_id, youtube_sub),
  )
  db.commit()

  return RedirectResponse("/")


@router.get("/external-accounts")
def list_accounts(user_id: str = Depends(require_user), db=Depends(get_db)) -> dict:
  """
  List all external accounts linked to the current user.

  Returns:
    dict: {
      "ok": True,
      "accounts": [
        {"provider": "spotify"|"youtube", "external_id": str},
        ...
      ]
    }
  """
  rows = db.execute(
    "SELECT provider, external_id FROM accounts WHERE user_id = ?", (user_id,)
  ).fetchall()

  accounts = [
    {"provider": r["provider"], "external_id": r["external_id"]} for r in rows
  ]

  return {"ok": True, "accounts": accounts}


@router.post("/external-accounts/unlink")
def unlink_account(
  data: UnlinkAccountRequest,
  user_id: str = Depends(require_user),
  db=Depends(get_db),
) -> dict:
  """
  Unlink a user's external account if no playlists are blocking it.

  Args:
    data (UnlinkAccountRequest): Provider and external ID to unlink.
    user_id (str): Authenticated user_id from require_user dependency.
    db: Database connection.

  Returns:
    dict: {
      "ok": True/False,
      "error": str (if blocked),
      "blocking_playlists": list (if blocked)
    }
  """
  # Check for blocking playlists
  blocking_playlists = db.execute(
    "SELECT playlist_id, name FROM playlists WHERE user_id = ? AND provider = ? AND external_id = ?",
    (user_id, data.provider, data.external_id),
  ).fetchall()

  if blocking_playlists:
    return {
      "ok": False,
      "error": "Account has linked playlists",
      "blocking_playlists": [
        {"playlist_id": r["playlist_id"], "name": r["name"]} for r in blocking_playlists
      ],
    }

  # Safe to unlink
  db.execute(
    "DELETE FROM accounts WHERE user_id = ? AND provider = ? AND external_id = ?",
    (user_id, data.provider, data.external_id),
  )
  db.commit()

  return {"ok": True}
