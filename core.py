import os
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from helpers.setup import STATIC_DIR
from helpers.db import init_db
from helpers.external import router as external_router
from helpers.accounts import router as accounts_router
from helpers.sync import router as sync_router
from helpers.library import router as library_router
from helpers.playlists import router as playlists_router
from helpers.search import router as search_router
from helpers.media import router as media_router
from helpers.whitelist import router as whitelist_router

app = FastAPI(title="Sideplay API")

init_db()

os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
  """
  Normalize HTTPException payloads across the API.

  Returns:
    JSONResponse: `{"ok": False, "error": <detail>}` with the original status code.
  """
  return JSONResponse(
    status_code=exc.status_code,
    content={"ok": False, "error": exc.detail},
  )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
  request: Request, exc: RequestValidationError
) -> JSONResponse:
  """
  Normalize request validation errors across the API.

  Returns:
    JSONResponse: `{"ok": False, "error": "Validation error", "details": [...]}`.
  """
  return JSONResponse(
    status_code=422,
    content={"ok": False, "error": "Validation error", "details": exc.errors()},
  )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
  """
  Normalize unexpected server errors.

  Returns:
    JSONResponse: `{"ok": False, "error": "Internal server error"}`.
  """
  return JSONResponse(
    status_code=500,
    content={"ok": False, "error": "Internal server error"},
  )


@app.get("/", response_class=HTMLResponse)
def gui() -> HTMLResponse:
  """
  Serve the main GUI page for Sideplay.

  Reads the 'gui.html' file from the static directory and returns its contents as HTML.

  Returns:
      HTMLResponse: The contents of the GUI HTML page.
  """
  with open(os.path.join(STATIC_DIR, "gui.html")) as f:
    return f.read()


app.include_router(external_router)
app.include_router(sync_router)
app.include_router(library_router)
app.include_router(accounts_router)
app.include_router(playlists_router)
app.include_router(search_router)
app.include_router(media_router)
app.include_router(whitelist_router)

if __name__ == "__main__":
  uvicorn.run("core:app", host="0.0.0.0", port=5000, reload=True)
