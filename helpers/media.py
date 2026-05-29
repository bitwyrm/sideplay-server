from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
import os
import hashlib
from email.utils import formatdate
from helpers.setup import MEDIA_DIR, THUMB_DIR
from helpers.accounts import require_user_or_open_access
from helpers.db import get_db

router = APIRouter(tags=["media"])


def media_path(youtube_id: str) -> str:
  return os.path.join(MEDIA_DIR, f"{youtube_id}.webm")


def thumb_path(youtube_id: str) -> str:
  return os.path.join(THUMB_DIR, f"{youtube_id}.webp")


def _iter_file_chunks(path: str, start: int, end: int, chunk_size: int = 1024 * 256):
  with open(path, "rb") as f:
    f.seek(start)
    remaining = end - start + 1
    while remaining > 0:
      read_size = min(chunk_size, remaining)
      data = f.read(read_size)
      if not data:
        break
      yield data
      remaining -= len(data)


def _parse_range(range_header: str, file_size: int) -> tuple[int, int] | None:
  if not range_header.startswith("bytes="):
    return None
  spec = range_header[len("bytes=") :].strip()
  if "," in spec:
    return None
  start_str, sep, end_str = spec.partition("-")
  if sep != "-":
    return None

  if start_str == "":
    if end_str == "":
      return None
    suffix_len = int(end_str)
    if suffix_len <= 0:
      return None
    start = max(file_size - suffix_len, 0)
    end = file_size - 1
    return (start, end)

  start = int(start_str)
  end = file_size - 1 if end_str == "" else int(end_str)
  if start < 0 or end < start:
    return None
  if start >= file_size:
    return None
  end = min(end, file_size - 1)
  return (start, end)


# -----------------------------
# Endpoints
# -----------------------------
@router.get("/tracks/{youtube_id}/media", response_model=None)
def serve_track(
  youtube_id: str, _auth: str | None = Depends(require_user_or_open_access)
) -> FileResponse | JSONResponse:
  """
  Serve a track's media file (.webm) with caching headers.

  Args:
    youtube_id (str): YouTube ID of the track.

  Returns:
    FileResponse: The track file if it exists.
    JSONResponse: 404 error if track not found.

  Headers included:
    - Cache-Control: public, 30 days
    - ETag: md5 hash of file modification time + size
    - Last-Modified: HTTP formatted last modified date
  """
  track_file = media_path(youtube_id)
  if not os.path.exists(track_file):
    return JSONResponse(
      status_code=404, content={"error": "Track not found", "youtube_id": youtube_id}
    )

  # Compute a simple ETag from file modification time + size
  stat = os.stat(track_file)
  etag = hashlib.md5(f"{stat.st_mtime}-{stat.st_size}".encode()).hexdigest()
  last_modified_str = formatdate(stat.st_mtime, usegmt=True)

  headers = {
    "Cache-Control": "public, max-age=2592000",
    "ETag": etag,
    "Last-Modified": last_modified_str,
  }

  return FileResponse(track_file, media_type="video/webm", headers=headers)


@router.get("/tracks/{youtube_id}/stream", response_model=None)
def stream_track(
  youtube_id: str,
  request: Request,
  _auth: str | None = Depends(require_user_or_open_access),
  db=Depends(get_db),
) -> StreamingResponse | JSONResponse:
  """
  Stream a cached track with HTTP byte-range support.

  Track must be marked available and exist in local media cache.
  """
  row = db.execute(
    "SELECT available FROM tracks WHERE youtube_id = ?",
    (youtube_id,),
  ).fetchone()
  if not row or not bool(row["available"]):
    return JSONResponse(
      status_code=404,
      content={
        "error": "Track unavailable for streaming",
        "youtube_id": youtube_id,
      },
    )

  track_file = media_path(youtube_id)
  if not os.path.exists(track_file):
    return JSONResponse(
      status_code=404,
      content={
        "error": "Track unavailable for streaming",
        "youtube_id": youtube_id,
      },
    )

  file_size = os.path.getsize(track_file)
  range_header = request.headers.get("range")
  base_headers = {
    "Accept-Ranges": "bytes",
    "Cache-Control": "public, max-age=2592000",
  }
  media_type = "audio/webm"

  if not range_header:
    headers = {**base_headers, "Content-Length": str(file_size)}
    return StreamingResponse(
      _iter_file_chunks(track_file, 0, file_size - 1),
      status_code=200,
      headers=headers,
      media_type=media_type,
    )

  try:
    parsed = _parse_range(range_header, file_size)
  except ValueError:
    parsed = None
  if not parsed:
    return JSONResponse(
      status_code=416,
      content={"error": "Invalid range"},
      headers={"Content-Range": f"bytes */{file_size}", "Accept-Ranges": "bytes"},
    )

  start, end = parsed
  content_length = end - start + 1
  headers = {
    **base_headers,
    "Content-Range": f"bytes {start}-{end}/{file_size}",
    "Content-Length": str(content_length),
  }
  return StreamingResponse(
    _iter_file_chunks(track_file, start, end),
    status_code=206,
    headers=headers,
    media_type=media_type,
  )


@router.get("/tracks/{youtube_id}/thumbnail", response_model=None)
def serve_thumbnail(
  youtube_id: str, _auth: str | None = Depends(require_user_or_open_access)
) -> FileResponse | JSONResponse:
  """
  Serve a track's thumbnail (.webp).

  Args:
    youtube_id (str): YouTube ID of the track.

  Returns:
    FileResponse: The thumbnail file if it exists.
    JSONResponse: 404 error if thumbnail not found.
  """
  thumb_file = thumb_path(youtube_id)
  if not os.path.exists(thumb_file):
    return JSONResponse(
      status_code=404,
      content={"error": "Thumbnail not found", "youtube_id": youtube_id},
    )
  return FileResponse(thumb_file, media_type="image/webp")
