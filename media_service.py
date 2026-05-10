# -------------------------------------------------------------------
# Sideplay Media Service
# -------------------------------------------------------------------

import asyncio
import json
import logging
import os
import signal
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, List, Tuple

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from helpers.setup import MEDIA_DIR, THUMB_DIR

logger = logging.getLogger(__name__)

MAX_WORKERS = 4
JOB_TIMEOUT = 30  # seconds
HEARTBEAT_INTERVAL = 5  # seconds

# -------------------------------------------------------------------
# GLOBAL STATE
# -------------------------------------------------------------------

download_locks: Dict[str, asyncio.Lock] = {}
locks_lock = asyncio.Lock()

# ⬇️ job now carries (youtube_id, result_queue)
job_queue: asyncio.Queue[Tuple[str, asyncio.Queue]] = asyncio.Queue()

# -------------------------------------------------------------------
# MODELS
# -------------------------------------------------------------------


class EnsureMediaRequest(BaseModel):
  youtube_ids: List[str]


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
    stderr=asyncio.subprocess.DEVNULL,
    start_new_session=True,
  )

  try:
    await asyncio.wait_for(proc.wait(), timeout=timeout)
  except asyncio.TimeoutError:
    os.killpg(proc.pid, signal.SIGKILL)
    raise

  if proc.returncode != 0:
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

    await run_process(
      [
        "yt-dlp",
        "-f",
        "bestaudio[acodec=opus]/bestaudio/best",
        "--merge-output-format",
        "webm",
        "--write-thumbnail",
        "--convert-thumbnails",
        "webp",
        "--cookies",
        "cookies.txt",
        "--no-playlist",
        "--quiet",
        "-o",
        os.path.join(MEDIA_DIR, "%(id)s.%(ext)s"),
        youtube_id,
      ],
      timeout=JOB_TIMEOUT,
    )

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
    youtube_id, result_queue = await job_queue.get()
    try:
      await download_and_convert(youtube_id)
      await result_queue.put({"youtube_id": youtube_id, "status": "ok"})
    except asyncio.TimeoutError:
      logger.warning("event=media_download_timeout youtube_id=%s", youtube_id)
      await result_queue.put(
        {"youtube_id": youtube_id, "status": "failed", "error": "timeout"}
      )
    except Exception as e:
      logger.exception("event=media_download_error youtube_id=%s error=%s", youtube_id, e)
      await result_queue.put(
        {"youtube_id": youtube_id, "status": "failed", "error": str(e)}
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
  Ensure media and thumbnails exist for the provided YouTube IDs.

  Args:
    req (EnsureMediaRequest): Request containing `youtube_ids`.

  Returns:
    StreamingResponse: JSONL stream with per-track status and heartbeats.
  """
  if not req.youtube_ids:
    raise HTTPException(400, "youtube_ids required")

  youtube_ids = list(dict.fromkeys(req.youtube_ids))
  total = len(youtube_ids)

  # ⬇️ request-scoped queue
  result_queue: asyncio.Queue = asyncio.Queue()

  for yt_id in youtube_ids:
    if media_exists(yt_id) and thumb_exists(yt_id):
      await result_queue.put({"youtube_id": yt_id, "status": "ok"})
    else:
      await job_queue.put((yt_id, result_queue))

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
