"""End-to-end checks of setup, sign-in and sign-out against a throwaway database."""

import importlib
import os
import tempfile

from fastapi.testclient import TestClient


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
        assert c.get("/api/status").json() == {"version": "0.1.0", "needs_setup": True, "user": None}
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
