"""End-to-end checks of setup, sign-in and sign-out against a throwaway database."""

import importlib
import os
import tempfile

from fastapi.testclient import TestClient

from rackmap import __version__


def make_client():
    os.environ["RACKMAP_DATA"] = tempfile.mkdtemp(prefix="rackmap-test-")
    from rackmap import app as app_module, auth, catalog, consoles, db, discovery, inventory, secrets_store

    db.close()
    for module in (db, secrets_store, auth, catalog, inventory, discovery, consoles, app_module):
        importlib.reload(module)
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


def test_snmp_packets_round_trip():
    from rackmap import snmp

    request = snmp.encode_request("public", 0xA0, 1234, snmp.SYS_DESCR)
    # a known-good SNMPv2c GET for sysDescr.0 with community "public"
    assert request.hex() == "302702010104067075626c6963a01a020204d2020100020100300e300c06082b060102010101000500"
    binding = snmp._tlv(0x30, snmp._oid("1.3.6.1.2.1.4.22.1.2.7.10.5.0.20") + snmp._tlv(0x04, bytes.fromhex("bc2411916c40")))
    pdu = snmp._tlv(0xA2, snmp._int(77) + snmp._int(0) + snmp._int(0) + snmp._tlv(0x30, binding))
    reply = snmp._tlv(0x30, snmp._int(1) + snmp._tlv(0x04, b"public") + pdu)
    assert snmp.decode_response(reply, 77) == ("1.3.6.1.2.1.4.22.1.2.7.10.5.0.20", 0x04, bytes.fromhex("bc2411916c40"))
    import pytest
    with pytest.raises(snmp.SnmpError):
        snmp.decode_response(reply, 78)  # answer to someone else's question
    with pytest.raises(snmp.SnmpError):
        snmp.decode_response(reply[:20], 77)  # truncated


def test_discovery_scan_review_and_add():
    from rackmap import discovery

    c = signed_in()
    try:
        # only private networks, of a sensible size
        assert c.put("/api/discovery/settings", json={"networks": "8.8.8.0/24"}).status_code == 422
        assert c.put("/api/discovery/settings", json={"networks": "10.0.0.0/8"}).status_code == 422
        assert c.post("/api/discovery/scan", json={}).status_code == 422  # nothing to scan yet
        assert c.put("/api/discovery/settings", json={"networks": "192.168.50.0/29", "community": "public"}).status_code == 200
        view = c.get("/api/discovery").json()
        assert view["settings"]["community_set"] is True and "community" not in view["settings"]

        answers = {
            "192.168.50.1": {"ports": [22, 80, 8291], "hostname": "core.lan", "sys_name": "core", "sys_descr": "RouterOS CRS328-24P-4S+RM",
                             "http_title": "RouterOS router configuration page", "http_server": ""},
            "192.168.50.2": {"ports": [9100], "hostname": "", "sys_name": "", "sys_descr": "", "http_title": "", "http_server": ""},
        }

        def fake_probe(ip, community):
            return {"ip": ip, **answers[ip]} if ip in answers else None

        job = discovery.start_scan(probe_fn=fake_probe, neighbours_fn=lambda: {"192.168.50.1": "48:a9:8a:29:e7:5c"}, wait=True)
        assert job["state"] == "finished" and job["total"] == 6 and job["found"] == 2

        hosts = {h["ip"]: h for h in c.get("/api/discovery").json()["hosts"]}
        core = hosts["192.168.50.1"]
        assert core["vendor"] == "Routerboard.com" and core["suggested_slug"] == "mikrotik-crs328-24p-4s-plus-rm"
        assert core["looks_like"] == "MikroTik router or switch" and core["best_name"] == "core" and core["device"] is None
        assert hosts["192.168.50.2"]["looks_like"] == "Printer"
        assert c.get("/api/overview").json()["new_found"] == 2

        # adding the equipment (with the found address) marks the result as recorded
        added = c.post("/api/devices", json={"type_slug": core["suggested_slug"], "name": "core", "ip": core["ip"], "mac": core["mac"]}).json()
        hosts = {h["ip"]: h for h in c.get("/api/discovery").json()["hosts"]}
        assert hosts["192.168.50.1"]["device"] == {"id": added["id"], "name": "core"}
        # ignoring hides a result from the count without deleting it
        assert c.post(f"/api/discovery/hosts/{hosts['192.168.50.2']['id']}/ignore", json={"ignored": True}).status_code == 200
        assert c.get("/api/overview").json()["new_found"] == 0
        # a silent device on the local network is still listed, from the neighbour table
        discovery.start_scan(probe_fn=fake_probe, neighbours_fn=lambda: {"192.168.50.5": "58:47:ca:75:ad:bf", "10.9.9.9": "aa:aa:aa:aa:aa:aa"}, wait=True)
        hosts = {h["ip"]: h for h in c.get("/api/discovery").json()["hosts"]}
        assert hosts["192.168.50.5"]["ports"] == [] and "10.9.9.9" not in hosts
        c.post(f"/api/discovery/hosts/{hosts['192.168.50.5']['id']}/ignore", json={"ignored": True})
        # a second scan updates rather than duplicates
        discovery.start_scan(probe_fn=fake_probe, neighbours_fn=lambda: {}, wait=True)
        again = c.get("/api/discovery").json()["hosts"]
        assert len(again) == 3 and {h["ip"]: h for h in again}["192.168.50.1"]["mac"] == "48:a9:8a:29:e7:5c"
    finally:
        c.__exit__(None, None, None)


def test_console_server_sessions_and_secrets():
    import json as jsonlib
    import sqlite3

    from fake_digi import FakeDigi
    from rackmap import db

    c = signed_in()
    origin = {"origin": "http://testserver"}
    try:
        with FakeDigi(busy_ports={3}) as digi:
            body = {"name": "Rack console", "host": "127.0.0.1", "port": digi.port, "username": "root", "port_count": 4}
            # wrong password: nothing is saved, and the reason is readable
            r = c.post("/api/consoles", json={**body, "password": "nope"})
            assert r.status_code == 422 and "refused" in r.json()["detail"]
            assert c.get("/api/consoles").json()["servers"] == []
            # public addresses are not accepted
            assert c.post("/api/consoles", json={**body, "host": "8.8.8.8", "password": "x"}).status_code == 422

            server = c.post("/api/consoles", json={**body, "password": "digi-secret"}).json()
            assert [p["number"] for p in server["ports"]] == [1, 2, 3, 4] and server["host_key_fingerprint"].startswith("SHA256:")
            # the password never comes back, and is not readable in the database file
            listing = c.get("/api/consoles").text
            assert "digi-secret" not in listing and "secret" not in server
            raw = sqlite3.connect(db.DATA_DIR / "rackmap.db").execute("SELECT secret FROM console_servers").fetchone()[0]
            assert "digi-secret" not in raw and (db.DATA_DIR / "secret.key").stat().st_mode & 0o077 == 0

            # label a port and tie it to a piece of equipment
            device = c.post("/api/devices", json={"name": "Core switch", "u_height": 1}).json()
            port1 = server["ports"][0]
            assert c.put(f"/api/consoles/ports/{port1['id']}", json={"label": "core", "device_id": device["id"]}).status_code == 200
            over = c.get("/api/overview").json()
            assert over["unplaced"][0]["console_ports"][0]["number"] == 1

            # a console needs a signed-in browser on rackmap's own page
            import pytest
            from starlette.websockets import WebSocketDisconnect
            with pytest.raises(WebSocketDisconnect):
                with c.websocket_connect(f"/ws/console/{port1['id']}", headers={"origin": "http://evil.example"}):
                    pass

            with c.websocket_connect(f"/ws/console/{port1['id']}", headers=origin) as ws:
                assert jsonlib.loads(ws.receive_text())["state"] == "connecting"
                assert jsonlib.loads(ws.receive_text())["state"] == "open"
                first = ws.receive_bytes()
                # the sign-in exchange is not shown; the device's own output is
                assert first == b"device-on-port-1 login: " and b"Password" not in first
                ws.send_text(jsonlib.dumps({"t": "size", "cols": 120, "rows": 40}))
                ws.send_text(jsonlib.dumps({"t": "in", "d": "hello\r"}))
                assert ws.receive_bytes() == b"echo:hello\r"
                ws.send_text(jsonlib.dumps({"t": "in", "d": "\u0004"}))  # device hangs up
                assert jsonlib.loads(ws.receive_text())["state"] == "closed"

            # a port someone else is using says so
            with c.websocket_connect(f"/ws/console/{server['ports'][2]['id']}", headers=origin) as ws:
                ws.receive_text()
                failed = jsonlib.loads(ws.receive_text())
                assert failed["state"] == "failed" and "in use" in failed["message"]

            # editing without a password keeps the saved one; shrinking drops the extra ports
            edited = c.put(f"/api/consoles/{server['id']}", json={**body, "name": "Renamed", "port_count": 2}).json()
            assert edited["name"] == "Renamed" and len(edited["ports"]) == 2 and edited["ports"][0]["label"] == "core"

            # a changed host key is refused until the password is entered again
            digi.new_identity()
            with c.websocket_connect(f"/ws/console/{port1['id']}", headers=origin) as ws:
                ws.receive_text()
                failed = jsonlib.loads(ws.receive_text())
                assert failed["state"] == "failed" and "identity" in failed["message"]
            assert c.put(f"/api/consoles/{server['id']}", json={**body, "port_count": 2}).status_code == 422
            relearned = c.put(f"/api/consoles/{server['id']}", json={**body, "port_count": 2, "password": "digi-secret"}).json()
            assert relearned["host_key_fingerprint"] != server["host_key_fingerprint"]

            # deleting the equipment unties the port; deleting the server removes its ports
            c.delete(f"/api/devices/{device['id']}")
            assert c.get("/api/consoles").json()["servers"][0]["ports"][0]["device"] is None
            assert c.delete(f"/api/consoles/{server['id']}").status_code == 200
            assert c.get("/api/consoles").json()["servers"] == []
    finally:
        c.__exit__(None, None, None)


def test_console_socket_needs_sign_in():
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with make_client() as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws/console/1", headers={"origin": "http://testserver"}):
                pass
