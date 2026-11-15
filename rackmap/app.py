"""HTTP application: JSON API under /api, the single-page front end everywhere else."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, auth, db

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.connect()
    yield
    db.close()


app = FastAPI(title="rackmap", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class Credentials(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def current_user(request: Request) -> dict:
    user = auth.session_user(request.cookies.get(auth.SESSION_COOKIE))
    if not user:
        raise HTTPException(status_code=401, detail="Sign in first.")
    return user


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self' ws: wss:; frame-ancestors 'none'",
    )
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.get("/api/status")
def status(request: Request) -> dict:
    """What the front end needs to decide which screen to show. Safe to call signed out."""
    user = auth.session_user(request.cookies.get(auth.SESSION_COOKIE))
    return {"version": __version__, "needs_setup": not auth.has_users(), "user": user["name"] if user else None}


@app.post("/api/setup")
def setup(body: Credentials, request: Request, response: Response) -> dict:
    """Create the first (administrator) account. Refused once any account exists."""
    if auth.has_users():
        raise HTTPException(status_code=409, detail="rackmap is already set up. Sign in instead.")
    if len(body.password) < auth.MIN_PASSWORD:
        raise HTTPException(status_code=422, detail=f"Use a password of at least {auth.MIN_PASSWORD} characters.")
    user_id = auth.create_user(body.name.strip(), body.password)
    _set_session_cookie(response, request, auth.start_session(user_id))
    return {"user": body.name.strip()}


@app.post("/api/login")
def login(body: Credentials, request: Request, response: Response) -> dict:
    client = client_address(request)
    if auth.locked_out(client):
        raise HTTPException(status_code=429, detail="Too many failed sign-ins. Wait a minute and try again.")
    user_id = auth.check_login(body.name.strip(), body.password, client)
    if user_id is None:
        raise HTTPException(status_code=401, detail="That name and password don't match.")
    _set_session_cookie(response, request, auth.start_session(user_id))
    return {"user": body.name.strip()}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict:
    auth.end_session(request.cookies.get(auth.SESSION_COOKIE))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {}


@app.get("/api/overview")
def overview(user: dict = Depends(current_user)) -> dict:
    """Signed-in landing data. Racks, devices and consoles arrive in later stages."""
    return {"user": user["name"], "racks": [], "devices": [], "consoles": []}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/{path:path}", include_in_schema=False)
def front_end(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
