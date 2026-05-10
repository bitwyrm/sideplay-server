import secrets
from datetime import UTC, datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from helpers.db import get_db
from helpers.setup import SESSION_HOURS
import bcrypt

from pydantic import BaseModel


class LoginRequest(BaseModel):
  """Request body for user login containing username and password."""

  username: str
  password: str


class SignupRequest(BaseModel):
  """Request body for user signup containing username and password."""

  username: str
  password: str


class ResetUsernameRequest(BaseModel):
  """Request body for resetting a username."""

  new_username: str


class ResetPasswordRequest(BaseModel):
  """Request body for resetting a password."""

  old_password: str
  new_password: str


def hash_password(password: str) -> str:
  """
  Hash a plaintext password using bcrypt.

  Args:
    password (str): The plaintext password.

  Returns:
    str: The bcrypt hashed password.
  """
  return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
  """
  Verify a plaintext password against a bcrypt hash.

  Args:
    password (str): The plaintext password.
    password_hash (str): The bcrypt hash to verify against.

  Returns:
    bool: True if password matches, else False.
  """
  return bcrypt.checkpw(password.encode(), password_hash.encode())


def get_user_from_session(session_token: str, db) -> str | None:
  """
  Retrieve the user_id associated with a session token.

  Args:
    session_token (str): The session token.
    db: Database connection.

  Returns:
    str | None: user_id if token is valid, None otherwise.
  """
  row = db.execute(
    "SELECT user_id, expires_at FROM sessions WHERE session_token = ?",
    (session_token,),
  ).fetchone()

  if not row:
    return None

  if datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
    return None

  return row["user_id"]


def require_user(request: Request, db=Depends(get_db)) -> str:
  """
  FastAPI dependency to ensure the user is authenticated.

  Raises:
    HTTPException 401: if session_token is missing or invalid.

  Args:
    request (Request): FastAPI request object.
    db: Database connection (provided by Depends(get_db)).

  Returns:
    str: user_id of the authenticated user.
  """
  token = request.headers.get("session_token")
  if not token:
    raise HTTPException(401, "Missing session_token")

  user_id = get_user_from_session(token, db)
  if not user_id:
    raise HTTPException(401, "Invalid or expired session")

  return user_id


router = APIRouter(tags=["accounts"])


@router.post("/sign-up")
def sign_up(data: SignupRequest, db=Depends(get_db)) -> dict[str, bool]:
  """
  Sign up a new user.

  Args:
    data (SignupRequest): User signup info.
    db: Database connection.

  Raises:
    HTTPException 400: if username already exists.

  Returns:
    dict: {"ok": True} on success.
  """
  if db.execute(
    "SELECT 1 FROM users WHERE username = ?",
    (data.username,),
  ).fetchone():
    raise HTTPException(400, "Username already exists")

  user_id = secrets.token_hex(32)

  db.execute(
    "INSERT INTO users (user_id, username, password_hash) VALUES (?, ?, ?)",
    (user_id, data.username, hash_password(data.password)),
  )
  db.commit()
  return {"ok": True}


@router.post("/login")
def login(data: LoginRequest, db=Depends(get_db)) -> dict[str, str]:
  """
  Login a user and create a session token.

  Args:
    data (LoginRequest): User login info.
    db: Database connection.

  Raises:
    HTTPException 401: if username or password is invalid.

  Returns:
    dict: {"session_token": <token>} on success.
  """
  user = db.execute(
    "SELECT * FROM users WHERE username = ?",
    (data.username,),
  ).fetchone()

  if not user or not verify_password(data.password, user["password_hash"]):
    raise HTTPException(401, "Invalid username or password")

  token = secrets.token_hex(32)
  expires = datetime.now(UTC) + timedelta(hours=SESSION_HOURS)

  db.execute(
    "INSERT INTO sessions (session_token, user_id, expires_at) VALUES (?, ?, ?)",
    (token, user["user_id"], expires.isoformat()),
  )
  db.commit()

  return {"session_token": token}


@router.post("/logout")
def logout(request: Request, db=Depends(get_db)) -> dict[str, bool]:
  """
  Logout a user by deleting their session token.

  Args:
    request (Request): FastAPI request object.
    db: Database connection.

  Returns:
    dict: {"ok": True} always.
  """
  token = request.headers.get("session_token")
  if token:
    db.execute("DELETE FROM sessions WHERE session_token = ?", (token,))
    db.commit()
  return {"ok": True}


# -------------------------------------------------------------------
# ACCOUNT MANAGEMENT
# -------------------------------------------------------------------


@router.post("/account/reset-username")
def reset_username(
  data: ResetUsernameRequest, user_id: str = Depends(require_user), db=Depends(get_db)
) -> dict[str, bool]:
  """
  Reset the username for the authenticated user.

  Args:
    data (ResetUsernameRequest): New username.
    user_id (str): Authenticated user_id from require_user.
    db: Database connection.

  Raises:
    HTTPException 400: if username already exists.

  Returns:
    dict: {"ok": True} on success.
  """
  if db.execute(
    "SELECT 1 FROM users WHERE username = ?", (data.new_username,)
  ).fetchone():
    raise HTTPException(400, "Username already exists")
  db.execute(
    "UPDATE users SET username = ? WHERE user_id = ?", (data.new_username, user_id)
  )
  db.commit()
  return {"ok": True}


@router.post("/account/reset-password")
def reset_password(
  data: ResetPasswordRequest,
  request: Request,
  user_id: str = Depends(require_user),
  db=Depends(get_db),
) -> dict[str, bool]:
  """
  Reset the password for the authenticated user.

  Args:
    data (ResetPasswordRequest): Old and new passwords.
    request (Request): FastAPI request object (kept for signature compatibility).
    user_id (str): Authenticated user_id from require_user.
    db: Database connection.

  Raises:
    HTTPException 401: if old password is invalid.

  Returns:
    dict: {"ok": True} on success.
  """
  user = db.execute(
    "SELECT password_hash FROM users WHERE user_id = ?", (user_id,)
  ).fetchone()
  if not verify_password(data.old_password, user["password_hash"]):
    raise HTTPException(401, "Invalid password")
  db.execute(
    "UPDATE users SET password_hash = ? WHERE user_id = ?",
    (hash_password(data.new_password), user_id),
  )
  db.commit()
  return {"ok": True}


@router.delete("/account")
def delete_account(
  user_id: str = Depends(require_user), db=Depends(get_db)
) -> dict[str, bool]:
  """
  Delete the authenticated user's account and related data.

  Args:
    user_id (str): Authenticated user_id from require_user.
    db: Database connection.

  Returns:
    dict: {"ok": True} on success.
  """
  # db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
  # db.execute("DELETE FROM accounts WHERE user_id = ?", (user_id,))
  # db.execute("DELETE FROM playlists WHERE user_id = ?", (user_id,))
  # db.execute(
  #   "UPDATE user_library SET removed_at = ? WHERE user_id = ?", (now_iso(), user_id)
  # )
  db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
  db.commit()
  return {"ok": True}
