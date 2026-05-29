import os
import sqlite3
from typing import Generator
from helpers.setup import DB_PATH

MIGRATION_ADD_PLAYLIST_LAST_SYNCED_AT = "20260510_add_playlists_last_synced_at"

def get_db() -> Generator[sqlite3.Connection, None, None]:
  """
  Yield a SQLite connection for a single request lifecycle.

  Foreign keys are enabled on every connection.

  Yields:
    sqlite3.Connection: Active database connection.
  """
  os.makedirs("data", exist_ok=True)
  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  conn.row_factory = sqlite3.Row
  # Enable foreign key enforcement for SQLite
  conn.execute("PRAGMA foreign_keys = ON")
  try:
    yield conn
  finally:
    conn.close()

def init_db() -> None:
  """
  Initialize and migrate the SQLite schema required by the API service.

  Creates tables/indexes if missing and applies lightweight additive
  migrations needed by newer code paths.

  Returns:
    None
  """
  conn = sqlite3.connect(DB_PATH)
  c = conn.cursor()

  # Users
  c.execute("""
    CREATE TABLE IF NOT EXISTS users (
      user_id TEXT PRIMARY KEY,
      username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL
    )
  """)

  # Sessions
  c.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
      session_token TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      expires_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )
  """)
  c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)")

  # Linked accounts
  c.execute("""
    CREATE TABLE IF NOT EXISTS accounts (
      user_id TEXT NOT NULL,
      provider TEXT NOT NULL,
      external_id TEXT,
      UNIQUE(user_id, provider, external_id),
      FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )
  """)
  c.execute("CREATE INDEX IF NOT EXISTS idx_accounts_user_id ON accounts(user_id)")
  c.execute("CREATE INDEX IF NOT EXISTS idx_accounts_provider ON accounts(provider)")

  # Tracks (global)
  c.execute("""
    CREATE TABLE IF NOT EXISTS tracks (
      youtube_id TEXT PRIMARY KEY,
      artists TEXT NOT NULL,
      title TEXT NOT NULL,
      album TEXT,
      available INTEGER NOT NULL
    )
  """)

  # User library (per user)
  c.execute("""
    CREATE TABLE IF NOT EXISTS user_library (
      user_id TEXT NOT NULL,
      youtube_id TEXT NOT NULL,
      UNIQUE(user_id, youtube_id),
      FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )
  """)
  c.execute("CREATE INDEX IF NOT EXISTS idx_user_library_user_id ON user_library(user_id)")

  # Playlists
  c.execute("""
    CREATE TABLE IF NOT EXISTS playlists (
      playlist_id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      provider TEXT NOT NULL,
      external_id TEXT,
      name TEXT NOT NULL,
      local_tracks TEXT,
      UNIQUE(playlist_id, user_id, provider),
      FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )
  """)
  c.execute("CREATE INDEX IF NOT EXISTS idx_playlists_user_id ON playlists(user_id)")
  c.execute("CREATE INDEX IF NOT EXISTS idx_playlists_provider ON playlists(provider)")

  # User whitelist (per user)
  c.execute("""
    CREATE TABLE IF NOT EXISTS user_whitelist (
      user_id TEXT NOT NULL,
      youtube_id TEXT NOT NULL,
      UNIQUE(user_id, youtube_id),
      FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
    )
  """)
  c.execute("CREATE INDEX IF NOT EXISTS idx_user_whitelist_user_id ON user_whitelist(user_id)")

  _ensure_migration_table(c)
  _apply_migrations(conn, c)

  conn.commit()
  conn.close()


def _ensure_migration_table(c: sqlite3.Cursor) -> None:
  """Create the schema migration tracking table if it does not exist."""
  c.execute("""
    CREATE TABLE IF NOT EXISTS schema_migrations (
      migration_id TEXT PRIMARY KEY,
      applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
  """)


def _has_column(c: sqlite3.Cursor, table_name: str, column_name: str) -> bool:
  """Return True if `table_name` has a column named `column_name`."""
  columns = {row[1] for row in c.execute(f"PRAGMA table_info({table_name})").fetchall()}
  return column_name in columns


def _apply_migrations(conn: sqlite3.Connection, c: sqlite3.Cursor) -> None:
  """Apply pending additive migrations in a deterministic order."""
  migrations = [
    (MIGRATION_ADD_PLAYLIST_LAST_SYNCED_AT, _migration_add_playlists_last_synced_at),
  ]

  applied = {
    row[0] for row in c.execute("SELECT migration_id FROM schema_migrations").fetchall()
  }
  for migration_id, migration_fn in migrations:
    if migration_id in applied:
      continue
    migration_fn(c)
    c.execute(
      "INSERT INTO schema_migrations (migration_id) VALUES (?)",
      (migration_id,),
    )
    conn.commit()


def _migration_add_playlists_last_synced_at(c: sqlite3.Cursor) -> None:
  """Add `playlists.last_synced_at` if missing."""
  if not _has_column(c, "playlists", "last_synced_at"):
    c.execute("ALTER TABLE playlists ADD COLUMN last_synced_at TEXT")
