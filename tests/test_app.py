"""End-to-end checks of setup, sign-in and sign-out against a throwaway database."""

import importlib
import os
import tempfile

from fastapi.testclient import TestClient

from rackmap import __version__


def make_client():
    os.environ["RACKMAP_DATA"] = tempfile.mkdtemp(prefix="rackmap-test-")
    from rackmap import app as app_module, auth, db

    db.close()
    importlib.reload(db)
    importlib.reload(auth)
    importlib.reload(app_module)
    return TestClient(app_module.app)


def test_first_run_then_login_flow():
    with make_client() as c:
        assert c.get("/api/status").json() == {"version": __version__, "needs_setup": True, "user": None}
        assert c.get("/api/overview").status_code == 401

        # too-short password is refused
        assert c.post("/api/setup", json={"name": "admin", "password": "short"}).status_code == 422
        assert c.post("/api/setup", json={"name": "admin", "password": "correct horse battery"}).status_code == 200
        assert c.get("/api/status").json()["user"] == "admin"
        assert c.get("/api/overview").json()["racks"] == []

        # setup can only happen once
        assert c.post("/api/setup", json={"name": "other", "password": "another long password"}).status_code == 409

        assert c.post("/api/logout", json={}).status_code == 200
        assert c.get("/api/overview").status_code == 401
        assert c.post("/api/login", json={"name": "admin", "password": "wrong password!"}).status_code == 401
        assert c.post("/api/login", json={"name": "admin", "password": "correct horse battery"}).status_code == 200
        assert c.get("/api/overview").status_code == 200


def test_lockout_after_repeated_failures():
    with make_client() as c:
        c.post("/api/setup", json={"name": "admin", "password": "correct horse battery"})
        c.post("/api/logout", json={})
        for _ in range(5):
            assert c.post("/api/login", json={"name": "admin", "password": "nope nope nope"}).status_code == 401
        # even the right password is refused during the lockout
        assert c.post("/api/login", json={"name": "admin", "password": "correct horse battery"}).status_code == 429


def test_front_end_and_headers():
    with make_client() as c:
        r = c.get("/")
        assert r.status_code == 200 and "rackmap" in r.text
        assert r.headers["x-frame-options"] == "DENY"
        assert c.get("/static/app.js").status_code == 200
        assert c.get("/api/nope").status_code == 404
        # password hashes never leave the database in clear
        from rackmap import db
        c.post("/api/setup", json={"name": "admin", "password": "correct horse battery"})
        row = db.query("SELECT pw_hash FROM users")[0]["pw_hash"]
        assert row.startswith("scrypt$") and "correct horse" not in row


def signed_in():
    c = make_client()
    c.__enter__()
    c.post("/api/setup", json={"name": "admin", "password": "correct horse battery"})
    return c


def test_catalog_search_and_placement_rules():
    c = signed_in()
    try:
        hits = c.get("/api/catalog", params={"q": "crs328 24p"}).json()["results"]
        assert hits and hits[0]["manufacturer"] == "MikroTik" and hits[0]["u_height"] == 1
        assert c.get("/api/catalog", params={"q": "zzzz-no-such-model"}).json()["results"] == []

        rack = c.post("/api/racks", json={"name": "Garage", "height_u": 12}).json()
        sw = c.post("/api/devices", json={"type_slug": hits[0]["slug"], "name": "Core", "rack_id": rack["id"], "position_u": 12}).json()
        assert sw["manufacturer"] == "MikroTik" and sw["full_depth"] is False and sw["kind"]["has_front_image"]

        # same units, same side: refused with a message that names what is in the way
        r = c.post("/api/devices", json={"name": "Shelf", "u_height": 1, "rack_id": rack["id"], "position_u": 12})
        assert r.status_code == 422 and "Core" in r.json()["detail"]
        # half-depth switch on the front leaves the rear of the same unit free for a half-depth device
        assert c.post("/api/devices", json={"name": "PDU", "u_height": 1, "full_depth": False, "face": "rear",
                                            "rack_id": rack["id"], "position_u": 12}).status_code == 200
        # a full-depth device blocks both sides
        assert c.post("/api/devices", json={"name": "Server", "u_height": 2, "rack_id": rack["id"], "position_u": 11}).status_code == 422
        server = c.post("/api/devices", json={"name": "Server", "u_height": 2, "rack_id": rack["id"], "position_u": 1}).json()
        # above the top of the rack
        assert c.post("/api/devices", json={"name": "Tall", "u_height": 4, "rack_id": rack["id"], "position_u": 10}).status_code == 422

        free = c.get(f"/api/racks/{rack['id']}/free", params={"u_height": 2}).json()["positions"]
        assert free[0] == 10 and 1 not in free and 2 not in free and 3 in free

        # moving a device onto itself (overlapping its own old place) is allowed
        assert c.patch(f"/api/devices/{server['id']}", json={"position_u": 2}).status_code == 200
        # shrinking the rack below its equipment is refused
        assert c.put(f"/api/racks/{rack['id']}", json={"name": "Garage", "height_u": 6}).status_code == 422

        # deleting the rack keeps the equipment, unplaced
        assert c.delete(f"/api/racks/{rack['id']}").status_code == 200
        over = c.get("/api/overview").json()
        assert over["racks"] == [] and len(over["unplaced"]) == 3
        assert c.delete(f"/api/devices/{sw['id']}").status_code == 200
        assert len(c.get("/api/overview").json()["unplaced"]) == 2
    finally:
        c.__exit__(None, None, None)


def test_equipment_needs_sign_in_and_sane_values():
    with make_client() as c:
        assert c.get("/api/catalog", params={"q": "dell"}).status_code == 401
        assert c.post("/api/racks", json={"name": "x", "height_u": 10}).status_code == 401
        c.post("/api/setup", json={"name": "admin", "password": "correct horse battery"})
        assert c.post("/api/racks", json={"name": "  ", "height_u": 10}).status_code == 422
        assert c.post("/api/racks", json={"name": "x", "height_u": 400}).status_code == 422
        assert c.post("/api/devices", json={"name": "x", "admin_url": "javascript:alert(1)"}).status_code == 422
        assert c.post("/api/devices", json={"type_slug": "not-a-real-model"}).status_code == 422
        assert c.get("/api/catalog/not-a-real-model/image/front").status_code == 404
        d = c.post("/api/devices", json={"manufacturer": "EnGenius", "model": "EWS357AP", "u_height": 0}).json()
        assert d["name"] == "EnGenius EWS357AP" and d["rack_id"] is None
