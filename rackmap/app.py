"""HTTP application: JSON API under /api, the single-page front end everywhere else."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import asyncio
import http.client
import json
import os
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, auth, catalog, consoles, controllers, db, discovery, inventory, kvms, monitor, pikvm

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.connect()
    catalog.load()
    if os.environ.get("RACKMAP_MONITOR", "1") != "0":
        monitor.start()
    yield
    monitor.stop()
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
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self' ws: wss:; frame-ancestors 'none'",
    )
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif request.url.path.startswith("/static/"):
        # Always re-check with the server (cheap: an unchanged file answers "not modified").
        # Without this, browsers keep using old scripts after rackmap is updated.
        response.headers["Cache-Control"] = "no-cache"
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


class RackBody(BaseModel):
    name: str = Field(max_length=80)
    height_u: int = Field(ge=1, le=inventory.MAX_RACK_U)
    notes: str = Field(default="", max_length=2000)


class DeviceBody(BaseModel):
    """Every field is optional so the same shape serves create and partial update."""

    name: str | None = Field(default=None, max_length=120)
    type_slug: str | None = Field(default=None, max_length=200)
    manufacturer: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=160)
    u_height: float | None = Field(default=None, ge=0, le=inventory.MAX_RACK_U)
    full_depth: bool | None = None
    rack_id: int | None = None
    position_u: float | None = None
    face: str | None = Field(default=None, max_length=5)
    ip: str | None = Field(default=None, max_length=64)
    mac: str | None = Field(default=None, max_length=32)
    serial: str | None = Field(default=None, max_length=80)
    admin_url: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=4000)


@app.exception_handler(inventory.Invalid)
async def invalid_request(_request: Request, exc: inventory.Invalid) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(discovery.Invalid)
async def invalid_discovery(_request: Request, exc: discovery.Invalid) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


class DiscoverySettings(BaseModel):
    networks: str = Field(default="", max_length=2000)
    router: str = Field(default="", max_length=64)
    # None leaves the saved community as it is; an empty string clears it
    community: str | None = Field(default=None, max_length=64)


class IgnoreBody(BaseModel):
    ignored: bool


def _discovery_view() -> dict:
    settings = discovery.get_settings()
    return {
        "settings": {"networks": settings["networks"], "router": settings["router"], "community_set": bool(settings["community"])},
        "suggested_networks": discovery.own_networks(),
        "job": discovery.status(),
        "hosts": discovery.list_hosts(),
    }


@app.get("/api/discovery")
def discovery_view(_user: dict = Depends(current_user)) -> dict:
    return _discovery_view()


@app.put("/api/discovery/settings")
def discovery_settings(body: DiscoverySettings, _user: dict = Depends(current_user)) -> dict:
    community = discovery.get_settings()["community"] if body.community is None else body.community
    discovery.save_settings(body.networks, body.router, community)
    return _discovery_view()


@app.post("/api/discovery/scan")
def discovery_scan(_user: dict = Depends(current_user)) -> dict:
    return {"job": discovery.start_scan()}


@app.post("/api/discovery/hosts/{host_id}/ignore")
def discovery_ignore(host_id: int, body: IgnoreBody, _user: dict = Depends(current_user)) -> dict:
    discovery.set_ignored(host_id, body.ignored)
    return {}


@app.get("/api/overview")
def overview(user: dict = Depends(current_user)) -> dict:
    """Everything the rack view draws: racks with their equipment, and equipment not in a rack."""
    new = sum(1 for h in discovery.list_hosts() if not h["ignored"] and not h["device"])
    return {"user": user["name"], "racks": inventory.list_racks(), "unplaced": inventory.unplaced_devices(), "new_found": new,
            "console_servers": consoles.list_servers(), "console_kinds": consoles.KINDS,
            "kvms": kvms.list_kvms(), "kvm_kinds": kvms.KINDS}


@app.post("/api/racks")
def create_rack(body: RackBody, _user: dict = Depends(current_user)) -> dict:
    return inventory.create_rack(body.name, body.height_u, body.notes)


@app.put("/api/racks/{rack_id}")
def update_rack(rack_id: int, body: RackBody, _user: dict = Depends(current_user)) -> dict:
    return inventory.update_rack(rack_id, body.name, body.height_u, body.notes)


@app.delete("/api/racks/{rack_id}")
def delete_rack(rack_id: int, _user: dict = Depends(current_user)) -> dict:
    inventory.delete_rack(rack_id)
    return {}


@app.get("/api/racks/{rack_id}/free")
def free_positions(rack_id: int, u_height: float, face: str = "front", full_depth: bool = True,
                   ignore: int | None = None, _user: dict = Depends(current_user)) -> dict:
    return {"positions": inventory.free_positions(rack_id, u_height, face, full_depth, ignore)}


@app.post("/api/devices")
def create_device(body: DeviceBody, _user: dict = Depends(current_user)) -> dict:
    device = inventory.create_device(body.model_dump(exclude_unset=True))
    monitor.poke()
    return device


@app.patch("/api/devices/{device_id}")
def update_device(device_id: int, body: DeviceBody, _user: dict = Depends(current_user)) -> dict:
    device = inventory.update_device(device_id, body.model_dump(exclude_unset=True))
    monitor.poke()
    return device


@app.delete("/api/devices/{device_id}")
def delete_device(device_id: int, _user: dict = Depends(current_user)) -> dict:
    inventory.delete_device(device_id)
    return {}


@app.get("/api/catalog")
def catalog_search(q: str = "", _user: dict = Depends(current_user)) -> dict:
    return {"results": catalog.search(q[:100])}


@app.get("/api/catalog/{slug}")
def catalog_get(slug: str, _user: dict = Depends(current_user)) -> dict:
    kind = catalog.get(slug)
    if not kind:
        raise HTTPException(status_code=404, detail="That model isn't in the equipment database.")
    return kind


@app.get("/api/catalog/{slug}/image/{face}")
def catalog_image(slug: str, face: str, _user: dict = Depends(current_user)) -> Response:
    found = catalog.image(slug, face)
    if not found:
        raise HTTPException(status_code=404, detail="No picture for this model.")
    data, media = found
    return Response(content=data, media_type=media, headers={"Cache-Control": "private, max-age=604800"})


class ConsoleServerBody(BaseModel):
    name: str = Field(max_length=80)
    kind: str = Field(default="digi-cm", max_length=20)
    host: str = Field(max_length=120)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(max_length=64)
    # empty when editing means "keep the saved password"
    password: str = Field(default="", max_length=256)
    port_count: int = Field(ge=1, le=consoles.MAX_PORTS)
    # the piece of equipment in the rack that this console server is, if it has been added
    device_id: int | None = None


class KvmBody(BaseModel):
    name: str = Field(max_length=80)
    kind: str = Field(default="other", max_length=20)
    url: str = Field(default="", max_length=300)
    port_count: int = Field(ge=1, le=kvms.MAX_PORTS)
    device_id: int
    # PiKVM only: an account on it lets rackmap show the screen itself. An empty password
    # when editing means "keep the saved one".
    username: str = Field(default="", max_length=64)
    password: str = Field(default="", max_length=256)


@app.exception_handler(kvms.Invalid)
async def invalid_kvm(_request: Request, exc: kvms.Invalid) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.post("/api/kvms")
def kvm_create(body: KvmBody, _user: dict = Depends(current_user)) -> dict:
    return kvms.save(body.model_dump())


@app.put("/api/kvms/{kvm_id}")
def kvm_update(kvm_id: int, body: KvmBody, _user: dict = Depends(current_user)) -> dict:
    return kvms.save(body.model_dump(), kvm_id)


@app.delete("/api/kvms/{kvm_id}")
def kvm_delete(kvm_id: int, _user: dict = Depends(current_user)) -> dict:
    kvms.delete(kvm_id)
    return {"ok": True}


@app.exception_handler(pikvm.KvmError)
async def kvm_failed(_request: Request, exc: pikvm.KvmError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


class KvmPowerBody(BaseModel):
    action: str = Field(max_length=20)


class KvmTextBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.get("/api/kvm-ports/{port_id}")
def kvm_port_about(port_id: int, _user: dict = Depends(current_user)) -> dict:
    return kvms.live_port(port_id)[2]


@app.get("/api/kvm-ports/{port_id}/snapshot")
def kvm_port_snapshot(port_id: int, _user: dict = Depends(current_user)) -> Response:
    """One still picture of the screen; the page falls back to these if the live stream fails."""
    return Response(kvms.live_port(port_id)[0].snapshot(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/kvm-ports/{port_id}/stream")
def kvm_port_stream(port_id: int, _user: dict = Depends(current_user)) -> StreamingResponse:
    """The KVM's live picture (MJPEG), passed straight through."""
    conn, upstream = kvms.live_port(port_id)[0].open_stream()

    def frames():
        try:
            while True:
                chunk = upstream.read1(65536)
                if not chunk:
                    break
                yield chunk
        except (OSError, http.client.HTTPException):
            pass
        finally:
            conn.close()

    return StreamingResponse(frames(), media_type=upstream.getheader("Content-Type") or "multipart/x-mixed-replace",
                             headers={"Cache-Control": "no-store"})


@app.get("/api/kvm-ports/{port_id}/power")
def kvm_port_power_state(port_id: int, _user: dict = Depends(current_user)) -> dict:
    client, switch_port, _about = kvms.live_port(port_id)
    if switch_port is not None:
        return {"wired": True, "on": None, "busy": False}  # a multi-port switch doesn't report one machine's light here
    return client.power_state()


@app.post("/api/kvm-ports/{port_id}/power")
def kvm_port_power(port_id: int, body: KvmPowerBody, _user: dict = Depends(current_user)) -> dict:
    client, switch_port, _about = kvms.live_port(port_id)
    client.power(body.action, switch_port)
    return {"ok": True}


@app.post("/api/kvm-ports/{port_id}/text")
def kvm_port_text(port_id: int, body: KvmTextBody, _user: dict = Depends(current_user)) -> dict:
    """Type a piece of text on the machine, as if on its keyboard."""
    kvms.live_port(port_id)[0].type_text(body.text)
    return {"ok": True}


@app.websocket("/ws/kvm/{port_id}")
async def kvm_socket(websocket: WebSocket, port_id: int) -> None:
    """Keyboard and mouse for an open screen. While this is connected the KVM keeps capturing
    video; what the browser sends is translated into the few event shapes PiKVM understands."""
    if not _same_origin(websocket) or not auth.session_user(websocket.cookies.get(auth.SESSION_COOKIE)):
        await websocket.close(code=4401)
        return
    await websocket.accept()

    async def say(state: str, **more) -> None:
        await websocket.send_text(json.dumps({"state": state, **more}))

    upstream = None
    try:
        await say("connecting")
        try:
            client, switch_port, about = kvms.live_port(port_id)
            if switch_port is not None:
                await run_in_threadpool(client.select_port, switch_port)
            upstream = await client.events()
        except (kvms.Invalid, pikvm.KvmError) as exc:
            await say("failed", message=str(exc))
            return
        await say("open", **about)

        async def from_browser() -> None:
            while True:
                try:
                    message = json.loads(await websocket.receive_text())
                except ValueError:
                    continue
                if isinstance(message, dict):
                    for event in pikvm.translate(message):
                        await upstream.send(json.dumps(event))

        async def from_kvm() -> None:
            async for raw in upstream:
                try:
                    note = pikvm.summarize(json.loads(raw)) if isinstance(raw, str) else None
                except ValueError:
                    note = None
                if note:
                    await websocket.send_text(json.dumps(note))
            await say("closed", message="The KVM closed the connection.")

        tasks = [asyncio.create_task(from_browser()), asyncio.create_task(from_kvm())]
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, WebSocketDisconnect, Exception):  # noqa: BLE001 - either side going away ends the session
                pass
    except WebSocketDisconnect:
        pass
    finally:
        if upstream is not None:
            await upstream.close()
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass  # the browser had already gone


class DevicePortBody(BaseModel):
    port_id: int | None = None


class ConsolePortBody(BaseModel):
    label: str = Field(default="", max_length=80)
    device_id: int | None = None


@app.exception_handler(consoles.Invalid)
async def invalid_console(_request: Request, exc: consoles.Invalid) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.get("/api/consoles")
def consoles_list(_user: dict = Depends(current_user)) -> dict:
    return {"kinds": consoles.KINDS, "servers": consoles.list_servers()}


@app.post("/api/consoles")
async def consoles_create(body: ConsoleServerBody, _user: dict = Depends(current_user)) -> dict:
    return await consoles.save_server(body.model_dump())


@app.put("/api/consoles/{server_id}")
async def consoles_update(server_id: int, body: ConsoleServerBody, _user: dict = Depends(current_user)) -> dict:
    return await consoles.save_server(body.model_dump(), server_id)


@app.delete("/api/consoles/{server_id}")
def consoles_delete(server_id: int, _user: dict = Depends(current_user)) -> dict:
    consoles.delete_server(server_id)
    return {}


@app.put("/api/devices/{device_id}/console-port")
def device_console_port(device_id: int, body: DevicePortBody, _user: dict = Depends(current_user)) -> dict:
    consoles.set_device_port(device_id, body.port_id)
    return {"ok": True}


@app.put("/api/devices/{device_id}/kvm-port")
def device_kvm_port(device_id: int, body: DevicePortBody, _user: dict = Depends(current_user)) -> dict:
    kvms.set_device_port(device_id, body.port_id)
    return {"ok": True}


@app.put("/api/consoles/ports/{port_id}")
def consoles_port(port_id: int, body: ConsolePortBody, _user: dict = Depends(current_user)) -> dict:
    consoles.update_port(port_id, body.label, body.device_id)
    return {}


def _same_origin(websocket: WebSocket) -> bool:
    """Browsers send cookies with cross-site WebSocket requests, so the page that opens a
    console must come from rackmap itself."""
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host")
    return bool(origin and host and urlsplit(origin).netloc == host)


async def _terminal(websocket: WebSocket, opener) -> None:
    """Bridge a browser terminal to a console session. `opener` is awaited to get the session."""
    if not _same_origin(websocket) or not auth.session_user(websocket.cookies.get(auth.SESSION_COOKIE)):
        await websocket.close(code=4401)
        return
    await websocket.accept()

    async def say(state: str, message: str = "") -> None:
        await websocket.send_text(json.dumps({"state": state, "message": message}))

    session = None
    try:
        await say("connecting")
        try:
            session = await opener()
        except consoles.ConsoleError as exc:
            await say("failed", str(exc))
            await websocket.close(code=1000)
            return
        await say("open")

        async def pump_out() -> None:
            while True:
                data = await session.read()
                if not data:
                    return
                await websocket.send_bytes(data)

        async def pump_in() -> None:
            while True:
                message = json.loads(await websocket.receive_text())
                if message.get("t") == "in":
                    session.write(str(message.get("d", "")).encode("utf-8")[:65536])
                elif message.get("t") == "size":
                    session.resize(int(message.get("cols", 80)), int(message.get("rows", 24)))

        out_task = asyncio.create_task(pump_out())
        in_task = asyncio.create_task(pump_in())
        done, pending = await asyncio.wait({out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if in_task in done and not in_task.cancelled():
            in_task.exception()  # the browser going away is expected; mark it as seen
        if out_task in done and not out_task.cancelled() and out_task.exception() is None:
            await say("closed", "The far end closed the session.")
            await websocket.close(code=1000)
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        if session is not None:
            session.close()


@app.websocket("/ws/console/{port_id}")
async def console_socket(websocket: WebSocket, port_id: int) -> None:
    await _terminal(websocket, lambda: consoles.open_port(port_id))


@app.websocket("/ws/controller/{device_id}")
async def controller_socket(websocket: WebSocket, device_id: int) -> None:
    await _terminal(websocket, lambda: controllers.open_console(device_id))


class ControllerBody(BaseModel):
    kind: str = Field(default="redfish", max_length=20)
    host: str = Field(max_length=120)
    username: str = Field(max_length=64)
    password: str = Field(default="", max_length=256)  # empty when editing keeps the saved one
    console_command: str = Field(default="", max_length=80)
    kvm_url: str = Field(default="", max_length=300)


class PowerBody(BaseModel):
    action: str = Field(max_length=20)


@app.exception_handler(controllers.Invalid)
async def invalid_controller(_request: Request, exc: controllers.Invalid) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(controllers.ControllerError)
async def controller_failed(_request: Request, exc: controllers.ControllerError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.get("/api/devices/{device_id}/controller")
def controller_get(device_id: int, _user: dict = Depends(current_user)) -> dict:
    return {"kinds": controllers.KINDS, "controller": controllers.get(device_id)}


@app.put("/api/devices/{device_id}/controller")
def controller_save(device_id: int, body: ControllerBody, _user: dict = Depends(current_user)) -> dict:
    return {"controller": controllers.save(device_id, body.model_dump())}


@app.delete("/api/devices/{device_id}/controller")
def controller_delete(device_id: int, _user: dict = Depends(current_user)) -> dict:
    controllers.remove(device_id)
    return {}


@app.get("/api/devices/{device_id}/controller/status")
def controller_status(device_id: int, last: bool = False, _user: dict = Depends(current_user)) -> dict:
    """`last=true` answers at once with what the controller said last time (`known: false` if
    it hasn't been asked since rackmap started); otherwise the controller is asked now."""
    if last:
        kept = controllers.last_status(device_id)
        return {"known": kept is not None, **(kept or {})}
    return {"known": True, **controllers.status(device_id)}


@app.post("/api/devices/{device_id}/controller/power")
def controller_power(device_id: int, body: PowerBody, _user: dict = Depends(current_user)) -> dict:
    return {"sent": controllers.power(device_id, body.action)}


@app.get("/api/live")
def live(_user: dict = Depends(current_user)) -> dict:
    """Which equipment is answering on the network, by device id."""
    return {"devices": {str(k): v for k, v in monitor.snapshot().items()}}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/{path:path}", include_in_schema=False)
def front_end(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
