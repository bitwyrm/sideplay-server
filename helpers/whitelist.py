from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from helpers.db import get_db
from helpers.accounts import require_user

router = APIRouter(tags=["whitelist"])


class WhitelistRequest(BaseModel):
  youtube_id: str


@router.post("/whitelist/add")
def add_to_whitelist(
  req: WhitelistRequest, user_id: str = Depends(require_user), db=Depends(get_db)
) -> dict:
  """
  Add a YouTube track to the current user's whitelist.

  Args:
    req (WhitelistRequest): Contains 'youtube_id' to whitelist.
    user_id (str): Authenticated user ID.
    db: Database connection.

  Returns:
    dict: Confirmation with 'youtube_id' and action performed.
  """
  try:
    db.execute(
      """
            INSERT OR IGNORE INTO user_whitelist (user_id, youtube_id)
            VALUES (?, ?)
            """,
      (user_id, req.youtube_id),
    )
    db.commit()
  except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))

  return {"ok": True, "youtube_id": req.youtube_id, "action": "added"}


@router.post("/whitelist/remove")
def remove_from_whitelist(
  req: WhitelistRequest, user_id: str = Depends(require_user), db=Depends(get_db)
) -> dict:
  """
  Remove a YouTube track from the current user's whitelist.

  Args:
    req (WhitelistRequest): Contains 'youtube_id' to remove.
    user_id (str): Authenticated user ID.
    db: Database connection.

  Returns:
    dict: Confirmation with 'youtube_id' and action performed.
  """
  db.execute(
    "DELETE FROM user_whitelist WHERE user_id = ? AND youtube_id = ?",
    (user_id, req.youtube_id),
  )
  db.commit()
  return {"ok": True, "youtube_id": req.youtube_id, "action": "removed"}


@router.get("/whitelist")
def get_whitelist(user_id: str = Depends(require_user), db=Depends(get_db)) -> dict:
  """
  Retrieve all YouTube track IDs currently whitelisted by the user.

  Args:
    user_id (str): Authenticated user ID.
    db: Database connection.

  Returns:
    dict: {"ok": True, "youtube_ids": list of whitelisted track IDs}
  """
  rows = db.execute(
    "SELECT youtube_id FROM user_whitelist WHERE user_id = ?", (user_id,)
  ).fetchall()
  return {"ok": True, "youtube_ids": [r["youtube_id"] for r in rows]}
