from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, JSONResponse
import os
import hashlib
from email.utils import formatdate
from helpers.setup import MEDIA_DIR, THUMB_DIR
from helpers.accounts import require_user_or_open_access

router = APIRouter(tags=["media"])


def media_path(youtube_id: str) -> str:
  return os.path.join(MEDIA_DIR, f"{youtube_id}.webm")


def thumb_path(youtube_id: str) -> str:
  return os.path.join(THUMB_DIR, f"{youtube_id}.webp")


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
