# -------------------------------------------------------------------
# Sideplay Media Service
# -------------------------------------------------------------------

import asyncio
import json
import logging
import os
import signal
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, List, Optional, Tuple

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from helpers.setup import DATA_ROOT, MEDIA_DIR, THUMB_DIR, YTDLP_COOKIES_FILE

logger = logging.getLogger(__name__)


def _configure_file_logging() -> None:
  os.makedirs(os.path.join(DATA_ROOT, "logs"), exist_ok=True)
  logfile = os.path.join(DATA_ROOT, "logs", "media_service.log")
  root = logging.getLogger()
  root.setLevel(logging.INFO)
  if not any(
    isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "") == logfile
    for h in root.handlers
  ):
    file_handler = RotatingFileHandler(logfile, maxBytes=10 * 1024 * 1024, backupCount=5)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
      logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root.addHandler(file_handler)


_configure_file_logging()

MAX_WORKERS = 4
JOB_TIMEOUT = 30  # seconds
HEARTBEAT_INTERVAL = 5  # seconds

# -------------------------------------------------------------------
# GLOBAL STATE
# -------------------------------------------------------------------

download_locks: Dict[str, asyncio.Lock] = {}
locks_lock = asyncio.Lock()

# job carries (track_payload, result_queue)
job_queue: asyncio.Queue[Tuple[dict, asyncio.Queue]] = asyncio.Queue()

# -------------------------------------------------------------------
# MODELS
# -------------------------------------------------------------------


class EnsureMediaRequest(BaseModel):
  """
  Request payload for media prefetch.

  Preferred shape:
    {
      "tracks": [
        {
          "youtube_id": "string",
          "song_name": "optional string",
          "artist": "optional string",
          "album": "optional string"
        }
      ]
    }

  Legacy compatibility:
    {"youtube_ids": ["id1", "id2", ...]}
  """
  tracks: List["MediaTrackRequest"] = []
  # Backward compatibility for older callers.
  youtube_ids: List[str] = []


class MediaTrackRequest(BaseModel):
  """
  Provider-agnostic media queue item.

  Fields:
    youtube_id: required source ID used for current fetch implementation.
    song_name/artist/album: optional metadata for downstream providers.
  """
  youtube_id: str
  song_name: Optional[str] = None
  artist: Optional[str] = None
  album: Optional[str] = None


# -------------------------------------------------------------------
# UTILS
# -------------------------------------------------------------------


async def get_lock(youtube_id: str) -> asyncio.Lock:
  """Return a shared per-track lock to prevent duplicate concurrent downloads."""
  async with locks_lock:
    if youtube_id not in download_locks:
      download_locks[youtube_id] = asyncio.Lock()
    return download_locks[youtube_id]


def media_path(youtube_id: str) -> str:
  """Build the expected media file path for a YouTube ID."""
  return os.path.join(MEDIA_DIR, f"{youtube_id}.webm")


def thumb_path(youtube_id: str) -> str:
  """Build the expected thumbnail file path for a YouTube ID."""
  return os.path.join(THUMB_DIR, f"{youtube_id}.webp")


def media_exists(youtube_id: str) -> bool:
  """Return True when the normalized media file exists on disk."""
  return os.path.exists(media_path(youtube_id))


def thumb_exists(youtube_id: str) -> bool:
  """Return True when the thumbnail file exists on disk."""
  return os.path.exists(thumb_path(youtube_id))


# -------------------------------------------------------------------
# SUBPROCESS HELPERS
# -------------------------------------------------------------------


async def run_process(cmd: list[str], timeout: int) -> None:
  """
  Execute a subprocess with a hard timeout.

  Args:
    cmd (list[str]): Command and args to execute.
    timeout (int): Max seconds to allow process completion.

  Returns:
    None

  Raises:
    asyncio.TimeoutError: If the process exceeds timeout.
    RuntimeError: If the process exits non-zero.
  """
  proc = await asyncio.create_subprocess_exec(
    *cmd,
    stdout=asyncio.subprocess.DEVNULL,
    stderr=asyncio.subprocess.PIPE,
    start_new_session=True,
  )

  try:
    await asyncio.wait_for(proc.wait(), timeout=timeout)
  except asyncio.TimeoutError:
    os.killpg(proc.pid, signal.SIGKILL)
    raise

  if proc.returncode != 0:
    stderr_text = ""
    if proc.stderr is not None:
      try:
        stderr_bytes = await proc.stderr.read()
        stderr_text = (stderr_bytes or b"").decode(errors="replace").strip()
      except Exception:
        stderr_text = ""
    if stderr_text:
      # Keep tail only to avoid huge error payloads.
      stderr_text = stderr_text[-3000:]
      raise RuntimeError(f"Process failed: {cmd} :: stderr={stderr_text}")
    raise RuntimeError(f"Process failed: {cmd}")


# -------------------------------------------------------------------
# DOWNLOAD + CONVERT
# -------------------------------------------------------------------


async def download_and_convert(youtube_id: str) -> None:
  """
  Ensure media and thumbnail exist locally for a single YouTube ID.

  Downloads with `yt-dlp` and, when needed, converts fallback audio formats
  to `.webm` using ffmpeg.

  Returns:
    None
  """
  lock = await get_lock(youtube_id)

  async with lock:
    if media_exists(youtube_id) and thumb_exists(youtube_id):
      logger.info("event=media_cache_hit youtube_id=%s", youtube_id)
      return
    logger.info("event=media_download_start youtube_id=%s", youtube_id)

    target_url = f"https://www.youtube.com/watch?v={youtube_id}"
    base_cmd = [
      "yt-dlp",
      "--no-update",
      "--extractor-args",
      "youtube:player_client=android,web",
      "-f",
      "bestaudio[acodec=opus]/bestaudio/best",
      "--merge-output-format",
      "webm",
      "--write-thumbnail",
      "--convert-thumbnails",
      "webp",
      "--no-playlist",
      "--quiet",
      "-o",
      os.path.join(MEDIA_DIR, "%(id)s.%(ext)s"),
    ]
    cmd_with_cookies = [*base_cmd, "--cookies", YTDLP_COOKIES_FILE, target_url]
    cmd_no_cookies = [*base_cmd, target_url]

    cookie_file_usable = os.path.exists(YTDLP_COOKIES_FILE) and os.access(
      YTDLP_COOKIES_FILE, os.R_OK
    )

    if cookie_file_usable:
      try:
        await run_process(cmd_with_cookies, timeout=JOB_TIMEOUT)
      except Exception as exc:
        logger.warning(
          "event=media_download_retry_without_cookies youtube_id=%s error=%s",
          youtube_id,
          exc,
        )
        await run_process(cmd_no_cookies, timeout=JOB_TIMEOUT)
    else:
      await run_process(cmd_no_cookies, timeout=JOB_TIMEOUT)

    src_thumb = os.path.join(MEDIA_DIR, f"{youtube_id}.webp")
    if os.path.exists(src_thumb):
      os.replace(src_thumb, thumb_path(youtube_id))

    final_path = media_path(youtube_id)
    if not os.path.exists(final_path):
      for ext in [".m4a", ".mp4", ".aac"]:
        p = os.path.join(MEDIA_DIR, f"{youtube_id}{ext}")
        if os.path.exists(p):
          await run_process(
            [
              "ffmpeg",
              "-y",
              "-i",
              p,
              "-c:a",
              "libopus",
              "-vn",
              final_path,
            ],
            timeout=JOB_TIMEOUT,
          )
          os.remove(p)
          break

    if not (media_exists(youtube_id) and thumb_exists(youtube_id)):
      raise RuntimeError("Post-download validation failed")
    logger.info("event=media_download_complete youtube_id=%s", youtube_id)


# -------------------------------------------------------------------
# WORKERS
# -------------------------------------------------------------------


async def worker() -> None:
  """Consume queued jobs and emit per-item completion results."""
  while True:
    track, result_queue = await job_queue.get()
    youtube_id = str(track.get("youtube_id") or "").strip()
    try:
      await download_and_convert(youtube_id)
      await result_queue.put(
        {
          "youtube_id": youtube_id,
          "song_name": track.get("song_name"),
          "artist": track.get("artist"),
          "album": track.get("album"),
          "status": "ok",
        }
      )
    except asyncio.TimeoutError:
      logger.warning("event=media_download_timeout youtube_id=%s", youtube_id)
      await result_queue.put(
        {
          "youtube_id": youtube_id,
          "song_name": track.get("song_name"),
          "artist": track.get("artist"),
          "album": track.get("album"),
          "status": "failed",
          "error": "timeout",
        }
      )
    except Exception as e:
      logger.exception("event=media_download_error youtube_id=%s error=%s", youtube_id, e)
      await result_queue.put(
        {
          "youtube_id": youtube_id,
          "song_name": track.get("song_name"),
          "artist": track.get("artist"),
          "album": track.get("album"),
          "status": "failed",
          "error": str(e),
        }
      )
    finally:
      job_queue.task_done()


# -------------------------------------------------------------------
# LIFESPAN
# -------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
  """Start worker tasks on app startup and cancel them on shutdown."""
  workers = [asyncio.create_task(worker()) for _ in range(MAX_WORKERS)]
  try:
    yield
  finally:
    for w in workers:
      w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)


# -------------------------------------------------------------------
# FASTAPI APP
# -------------------------------------------------------------------

app = FastAPI(
  title="Sideplay Media Service",
  lifespan=lifespan,
)

# -------------------------------------------------------------------
# STREAMING ENDPOINT
# -------------------------------------------------------------------


@app.post("/ensure-media")
async def ensure_media(req: EnsureMediaRequest) -> StreamingResponse:
  """
  Ensure media and thumbnails exist for the provided tracks.

  Args:
    req (EnsureMediaRequest): Request containing `tracks` (preferred).

  Returns:
    StreamingResponse: JSONL stream with per-track status objects.

  Stream line shape:
    {
      "youtube_id": str,
      "song_name": str | null,
      "artist": str | null,
      "album": str | null,
      "status": "ok" | "failed",
      "error": "string when failed"
    }
  """
  tracks = req.tracks or []
  if not tracks and req.youtube_ids:
    tracks = [MediaTrackRequest(youtube_id=yt_id) for yt_id in req.youtube_ids if yt_id]

  if not tracks:
    raise HTTPException(400, "tracks required")

  # Deduplicate by youtube_id while retaining first metadata payload.
  unique_tracks: list[dict] = []
  seen_ids: set[str] = set()
  for t in tracks:
    youtube_id = str(t.youtube_id).strip()
    if not youtube_id or youtube_id in seen_ids:
      continue
    seen_ids.add(youtube_id)
    unique_tracks.append(
      {
        "youtube_id": youtube_id,
        "song_name": t.song_name,
        "artist": t.artist,
        "album": t.album,
      }
    )

  total = len(unique_tracks)

  # ⬇️ request-scoped queue
  result_queue: asyncio.Queue = asyncio.Queue()

  for track in unique_tracks:
    yt_id = track["youtube_id"]
    if media_exists(yt_id) and thumb_exists(yt_id):
      await result_queue.put({**track, "status": "ok"})
    else:
      await job_queue.put((track, result_queue))

  async def stream() -> AsyncGenerator[str, None]:
    """Yield status lines until all submitted IDs are completed."""
    completed = 0
    while completed < total:
      try:
        result = await asyncio.wait_for(
          result_queue.get(),
          timeout=HEARTBEAT_INTERVAL,
        )
        completed += 1
        yield json.dumps(result) + "\n"
      except asyncio.TimeoutError:
        yield json.dumps({"type": "heartbeat"}) + "\n"

  return StreamingResponse(stream(), media_type="application/jsonl")


# -------------------------------------------------------------------
# ENTRYPOINT
# -------------------------------------------------------------------

if __name__ == "__main__":
  uvicorn.run(
    "media_service:app",
    host="127.0.0.1",
    port=7000,
    reload=True,
  )
