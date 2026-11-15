"""End-to-end checks of setup, sign-in and sign-out against a throwaway database."""

import importlib
import os
import tempfile

from fastapi.testclient import TestClient

from rackmap import __version__


def make_client():
    os.environ["RACKMAP_DATA"] = tempfile.mkdtemp(prefix="rackmap-test-")
    os.environ["RACKMAP_MONITOR"] = "0"  # tests drive the monitor by hand
    from rackmap import app as app_module, auth, catalog, consoles, controllers, db, discovery, inventory, monitor, secrets_store

    db.close()
    for module in (db, secrets_store, auth, catalog, inventory, discovery, consoles, controllers, monitor, app_module):
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
        script = c.get("/static/app.js")
        assert script.status_code == 200 and script.headers["cache-control"] == "no-cache"
        # an unchanged file is not sent again
        assert c.get("/static/app.js", headers={"If-None-Match": script.headers["etag"]}).status_code == 304
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
            r = c.post("/api/consoles", json={**body, "host": "8.8.8.8", "password": "x"})
            assert r.status_code == 422 and "private network" in r.json()["detail"]  # refused before any connection is tried

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


def test_management_controller_power_and_console():
    import json as jsonlib
    import sqlite3

    from fake_digi import FakeDigi
    from fake_redfish import FakeRedfish
    from rackmap import controllers, db

    c = signed_in()
    origin = {"origin": "http://testserver"}
    try:
        with FakeRedfish() as bmc, FakeDigi(username="root", password="bmc-secret") as ssh:
            controllers.SSH_PORT = ssh.port
            # the fake listens on a high port; tell the client where
            real_init = controllers.Redfish.__init__
            controllers.Redfish.__init__ = lambda self, host, username, password, pinned, port=None: real_init(self, host, username, password, pinned, bmc.port)

            server = c.post("/api/devices", json={"name": "PowerEdge", "u_height": 2, "ip": "10.99.0.5"}).json()
            assert server["has_controller"] is False
            url = f"/api/devices/{server['id']}/controller"
            body = {"host": "127.0.0.1", "username": "root", "console_command": "console com2", "kvm_url": "https://kvm.lan/"}

            r = c.put(url, json={**body, "host": "8.8.8.8", "password": "x"})
            assert r.status_code == 422 and "private network" in r.json()["detail"]
            # nothing is saved unless rackmap can sign in
            r = c.put(url, json={**body, "password": "wrong"})
            assert r.status_code == 422 and "refused" in r.json()["detail"]
            assert c.get(url).json()["controller"] is None
            assert c.put(url, json={**body, "password": "bmc-secret", "kvm_url": "javascript:alert(1)"}).status_code == 422
            assert c.put(url, json={**body, "password": "bmc-secret", "console_command": "console com2; reboot"}).status_code == 422

            saved = c.put(url, json={**body, "password": "bmc-secret"}).json()["controller"]
            assert saved["has_console"] and "password" not in saved and "secret" not in saved
            raw = sqlite3.connect(db.DATA_DIR / "rackmap.db").execute("SELECT secret FROM controllers").fetchone()[0]
            assert "bmc-secret" not in raw
            assert c.get("/api/overview").json()["unplaced"][0]["has_controller"] is True

            # saving forgets what was known, so nothing is remembered until the controller is asked
            assert c.get(url + "/status?last=true").json() == {"known": False}
            before = bmc.connections
            status = c.get(url + "/status").json()
            assert bmc.connections == before + 1  # five questions, one connection
            assert status["power_state"] == "On" and status["model"] == "PowerEdge R720xd" and status["watts"] == 250
            # ...and then the last answer is available at once, without asking again
            kept = c.get(url + "/status?last=true").json()
            assert kept["known"] and kept["power_state"] == "On" and bmc.connections == before + 1
            assert status["temperatures"] == [{"name": "Inlet Temp", "celsius": 21}]
            # only the actions this controller offers are listed ("restart" maps to ForceRestart here)
            assert set(status["actions"]) == {"on", "shutdown", "restart", "off"}

            assert c.post(url + "/power", json={"action": "shutdown"}).json() == {"sent": "GracefulShutdown"}
            assert c.get(url + "/status?last=true").json() == {"known": False}  # stale after a power action
            assert c.get(url + "/status").json()["power_state"] == "Off"
            assert c.post(url + "/power", json={"action": "restart"}).json() == {"sent": "ForceRestart"}
            assert c.post(url + "/power", json={"action": "explode"}).status_code == 422
            assert bmc.resets == ["GracefulShutdown", "ForceRestart"]

            # the text console runs in the same kind of terminal as a console-server port
            with c.websocket_connect(f"/ws/controller/{server['id']}", headers=origin) as ws:
                assert jsonlib.loads(ws.receive_text())["state"] == "connecting"
                assert jsonlib.loads(ws.receive_text())["state"] == "open"
                assert ws.receive_bytes().endswith(b"server login: ")
                ws.send_text(jsonlib.dumps({"t": "in", "d": "root\r"}))
                assert ws.receive_bytes() == b"echo:root\r"

            # a different certificate at the same address is refused until the password is re-entered
            bmc.new_identity()
            r = c.get(url + "/status")
            assert r.status_code == 502 and "certificate has changed" in r.json()["detail"]
            assert c.post(url + "/power", json={"action": "off"}).status_code == 502 and bmc.resets[-1] == "ForceRestart"
            assert c.put(url, json=body).status_code == 422
            assert c.put(url, json={**body, "password": "bmc-secret"}).status_code == 200
            assert c.get(url + "/status").status_code == 200

            # removing the equipment removes its controller record
            c.delete(f"/api/devices/{server['id']}")
            assert sqlite3.connect(db.DATA_DIR / "rackmap.db").execute("SELECT count(*) FROM controllers").fetchone()[0] == 0
    finally:
        c.__exit__(None, None, None)


def test_live_status_only_checks_private_addresses():
    from rackmap import monitor

    c = signed_in()
    try:
        up = c.post("/api/devices", json={"name": "switch", "u_height": 1, "ip": "192.168.7.2"}).json()
        down = c.post("/api/devices", json={"name": "nas", "u_height": 1, "ip": "192.168.7.3"}).json()
        c.post("/api/devices", json={"name": "cloud thing", "u_height": 0, "ip": "8.8.8.8"})
        c.post("/api/devices", json={"name": "no address", "u_height": 0})
        asked = []
        monitor.check_all(check=lambda ip: asked.append(ip) or ip.endswith(".2"))
        assert sorted(asked) == ["192.168.7.2", "192.168.7.3"]  # never a public address
        live = c.get("/api/live").json()["devices"]
        assert live[str(up["id"])]["up"] is True and live[str(down["id"])]["up"] is False and len(live) == 2
        c.delete(f"/api/devices/{down['id']}")
        monitor.check_all(check=lambda ip: True)
        assert list(c.get("/api/live").json()["devices"]) == [str(up["id"])]
    finally:
        c.__exit__(None, None, None)


def test_https_is_on_by_default_with_a_certificate_rackmap_makes(tmp_path):
    import ssl

    import pytest
    from cryptography import x509
    from rackmap import __main__ as launcher
    from rackmap import db, tls

    cert, key = tls.own_certificate(tmp_path)
    assert (key.stat().st_mode & 0o777) == 0o600
    ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(cert, key)  # a usable pair
    names = x509.load_pem_x509_certificate(cert.read_bytes()).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in names.get_values_for_type(x509.DNSName)
    before = cert.read_bytes()
    assert tls.own_certificate(tmp_path) == (cert, key) and cert.read_bytes() == before  # kept, not remade

    real = db.DATA_DIR
    db.DATA_DIR = tmp_path
    try:
        on = launcher.settings({})
        assert on["port"] == 8443 and on["ssl_certfile"] == str(cert) and "fingerprint" in on["note"]
        off = launcher.settings({"RACKMAP_TLS": "off"})
        assert off["port"] == 8080 and "ssl_certfile" not in off and "unencrypted" in off["note"]
        own = launcher.settings({"RACKMAP_TLS_CERT": str(cert), "RACKMAP_TLS_KEY": str(key), "RACKMAP_PORT": "443"})
        assert own["port"] == 443 and "note" not in own
        with pytest.raises(SystemExit):
            launcher.settings({"RACKMAP_TLS_CERT": str(cert)})
        with pytest.raises(SystemExit):
            launcher.settings({"RACKMAP_TLS": "maybe"})
    finally:
        db.DATA_DIR = real


def test_console_server_as_equipment_offers_its_ports_to_the_rest():
    from fake_digi import FakeDigi

    c = signed_in()
    try:
        with FakeDigi() as digi:
            digi_box = c.post("/api/devices", json={"name": "Digi CM 48", "u_height": 1}).json()
            firewall = c.post("/api/devices", json={"name": "Firewall", "u_height": 1}).json()
            switch = c.post("/api/devices", json={"name": "Switch", "u_height": 1}).json()
            assert digi_box["console_server_id"] is None
            assert c.get("/api/overview").json()["console_servers"] == []

            body = {"name": "Digi CM 48", "host": "127.0.0.1", "port": digi.port, "username": "root", "password": "digi-secret", "port_count": 4}
            assert c.post("/api/consoles", json={**body, "device_id": 9999}).status_code == 422
            server = c.post("/api/consoles", json={**body, "device_id": digi_box["id"]}).json()
            assert server["device_id"] == digi_box["id"]
            overview = c.get("/api/overview").json()
            assert overview["console_servers"][0]["id"] == server["id"] and "secret" not in str(overview["console_servers"])
            by_name = {d["name"]: d for d in overview["unplaced"]}
            assert by_name["Digi CM 48"]["console_server_id"] == server["id"] and by_name["Firewall"]["console_server_id"] is None

            port1, port2 = server["ports"][0]["id"], server["ports"][1]["id"]
            url = f"/api/devices/{firewall['id']}/console-port"
            assert c.put(url, json={"port_id": port1}).status_code == 200
            ports = c.get("/api/consoles").json()["servers"][0]["ports"]
            assert ports[0]["device"]["id"] == firewall["id"] and ports[0]["label"] == "Firewall"  # an empty label takes the name
            # a port serves one piece of equipment, and the console server isn't plugged into itself
            assert c.put(f"/api/devices/{switch['id']}/console-port", json={"port_id": port1}).status_code == 422
            assert c.put(f"/api/devices/{digi_box['id']}/console-port", json={"port_id": port2}).status_code == 422
            assert c.put(url, json={"port_id": 9999}).status_code == 422
            # moving to another port frees the first; "not connected" frees it altogether
            assert c.put(url, json={"port_id": port2}).status_code == 200
            ports = c.get("/api/consoles").json()["servers"][0]["ports"]
            assert ports[0]["device"] is None and ports[1]["device"]["id"] == firewall["id"]
            assert c.put(url, json={"port_id": None}).status_code == 200
            assert all(p["device"] is None for p in c.get("/api/consoles").json()["servers"][0]["ports"])

            # deleting the equipment keeps the console server, just no longer tied to it
            assert c.delete(f"/api/devices/{digi_box['id']}").status_code == 200
            assert c.get("/api/consoles").json()["servers"][0]["device_id"] is None
    finally:
        c.__exit__(None, None, None)


def test_kvm_as_equipment_offers_its_ports_to_the_rest():
    c = signed_in()
    try:
        kvm_box = c.post("/api/devices", json={"name": "Dell KVM", "u_height": 1}).json()
        server = c.post("/api/devices", json={"name": "Server", "u_height": 2}).json()
        other = c.post("/api/devices", json={"name": "Other", "u_height": 1}).json()
        assert kvm_box["kvm_id"] is None and server["kvm_ports"] == []
        assert c.get("/api/overview").json()["kvms"] == []

        body = {"name": "Dell KVM", "kind": "dell", "url": "10.3.0.40", "port_count": 8, "device_id": kvm_box["id"]}
        assert c.post("/api/kvms", json={**body, "url": "javascript:alert(1)"}).status_code == 422
        assert c.post("/api/kvms", json={**body, "url": "https://8.8.8.8"}).status_code == 422
        assert c.post("/api/kvms", json={**body, "kind": "nope"}).status_code == 422
        assert c.post("/api/kvms", json={**body, "device_id": 9999}).status_code == 422
        kvm = c.post("/api/kvms", json=body).json()
        assert kvm["url"] == "https://10.3.0.40" and len(kvm["ports"]) == 8 and kvm["kind_name"] == "Dell KVM switch"
        assert c.post("/api/kvms", json=body).status_code == 422  # one KVM per piece of equipment
        assert c.post("/api/kvms", json={**body, "url": "", "device_id": other["id"], "port_count": 1}).json()["url"] == ""  # not on the network

        port1, port2 = kvm["ports"][0]["id"], kvm["ports"][1]["id"]
        url = f"/api/devices/{server['id']}/kvm-port"
        assert c.put(url, json={"port_id": port1}).status_code == 200
        by_name = {d["name"]: d for d in c.get("/api/overview").json()["unplaced"]}
        assert by_name["Dell KVM"]["kvm_id"] == kvm["id"]
        assert by_name["Server"]["kvm_ports"][0]["number"] == 1 and by_name["Server"]["kvm_ports"][0]["url"] == "https://10.3.0.40"
        assert c.put(f"/api/devices/{kvm_box['id']}/kvm-port", json={"port_id": port2}).status_code == 422  # not into itself
        spare = c.post("/api/devices", json={"name": "Spare", "u_height": 1}).json()
        assert c.put(f"/api/devices/{spare['id']}/kvm-port", json={"port_id": port1}).status_code == 422  # port taken
        assert c.put(url, json={"port_id": port2}).status_code == 200  # moving frees the first
        ports = c.get("/api/overview").json()["kvms"][0]["ports"]
        assert ports[0]["device"] is None and ports[1]["device"]["id"] == server["id"]

        # fewer ports drops the ones that go; removing the equipment removes its KVM set-up
        assert len(c.put(f"/api/kvms/{kvm['id']}", json={**body, "port_count": 1}).json()["ports"]) == 1
        assert {d["name"]: d for d in c.get("/api/overview").json()["unplaced"]}["Server"]["kvm_ports"] == []
        assert c.delete(f"/api/devices/{kvm_box['id']}").status_code == 200
        assert [k["name"] for k in c.get("/api/overview").json()["kvms"]] == ["Dell KVM"]  # only the one on "Other" is left
        assert c.get("/api/overview").json()["kvms"][0]["device_id"] == other["id"]
    finally:
        c.__exit__(None, None, None)


def test_pikvm_screen_keyboard_mouse_and_power():
    import json as jsonlib
    import sqlite3
    import time

    from fake_pikvm import JPEG, FakePiKVM
    from rackmap import db, pikvm

    # what the browser sends becomes only the event shapes PiKVM knows
    assert pikvm.translate({"t": "key", "code": "KeyA", "down": True}) == [{"event_type": "key", "event": {"key": "KeyA", "state": True}}]
    assert pikvm.translate({"t": "key", "code": "../../x", "down": True}) == []
    assert pikvm.translate({"t": "move", "x": 0.5, "y": 0}) == [{"event_type": "mouse_move", "event": {"to": {"x": 0, "y": -32768}}}]
    assert pikvm.translate({"t": "move", "x": 9, "y": "no"}) == []
    assert pikvm.translate({"t": "button", "b": "fire", "down": True}) == []
    assert [e["event"] for e in pikvm.translate({"t": "shortcut", "keys": ["ControlLeft", "AltLeft", "Delete"]})][2:4] == [
        {"key": "Delete", "state": True}, {"key": "Delete", "state": False}]
    assert pikvm.translate({"t": "raw", "event_type": "anything"}) == []
    # state events, under the names of both current and older PiKVM software
    for name in ("atx", "atx_state"):
        assert pikvm.summarize({"event_type": name, "event": {"enabled": True, "leds": {"power": True}}}) == {"state": "power", "wired": True, "on": True}
    assert pikvm.summarize({"event_type": "streamer", "event": {"streamer": None}}) is None
    assert pikvm.summarize({"event_type": "streamer_state", "event": {"streamer": {"source": {"online": False}}}}) == {"state": "signal", "online": False}

    c = signed_in()
    origin = {"origin": "http://testserver"}
    try:
        with FakePiKVM() as pi:
            box = c.post("/api/devices", json={"name": "PiKVM", "u_height": 0}).json()
            nas = c.post("/api/devices", json={"name": "NAS", "u_height": 2}).json()
            body = {"name": "PiKVM", "kind": "pikvm", "url": pi.url, "port_count": 1, "device_id": box["id"], "username": "admin"}

            # nothing is saved unless rackmap can sign in
            r = c.post("/api/kvms", json={**body, "password": "wrong"})
            assert r.status_code == 422 and "refused" in r.json()["detail"]
            assert c.post("/api/kvms", json=body).status_code == 422  # no password at all
            assert c.get("/api/overview").json()["kvms"] == []

            kvm = c.post("/api/kvms", json={**body, "password": "kvm-secret"}).json()
            assert kvm["live"] is True and "secret" not in kvm and "password" not in kvm and "cert_pem" not in kvm
            raw = sqlite3.connect(db.DATA_DIR / "rackmap.db").execute("SELECT secret FROM kvms").fetchone()[0]
            assert "kvm-secret" not in raw
            # other details can change without re-entering the password
            assert c.put(f"/api/kvms/{kvm['id']}", json={**body, "name": "PiKVM on the NAS"}).json()["live"] is True

            port = kvm["ports"][0]["id"]
            assert c.put(f"/api/devices/{nas['id']}/kvm-port", json={"port_id": port}).status_code == 200
            mine = {d["name"]: d for d in c.get("/api/overview").json()["unplaced"]}["NAS"]["kvm_ports"][0]
            assert mine["live"] is True and mine["id"] == port

            url = f"/api/kvm-ports/{port}"
            assert c.get(url).json()["device"] == "NAS"
            shot = c.get(url + "/snapshot")
            assert shot.content == JPEG and shot.headers["content-type"] == "image/jpeg"
            stream = c.get(url + "/stream")
            assert stream.headers["content-type"].startswith("multipart/x-mixed-replace") and stream.content.count(JPEG) == 3

            assert c.get(url + "/power").json() == {"wired": True, "on": True, "busy": False}
            assert c.post(url + "/power", json={"action": "hold"}).status_code == 200
            assert c.post(url + "/power", json={"action": "explode"}).status_code == 502
            assert pi.calls == [("/api/atx/click", "power_long")] and c.get(url + "/power").json()["on"] is False
            assert c.post(url + "/text", json={"text": "hello\n"}).status_code == 200 and pi.typed == ["hello\n"]

            # the screen's control connection: state comes back, input goes through translated
            with c.websocket_connect(f"/ws/kvm/{port}", headers=origin) as ws:
                assert jsonlib.loads(ws.receive_text())["state"] == "connecting"
                opened = jsonlib.loads(ws.receive_text())
                assert opened["state"] == "open" and opened["device"] == "NAS"
                assert jsonlib.loads(ws.receive_text()) == {"state": "power", "wired": True, "on": False}
                assert jsonlib.loads(ws.receive_text()) == {"state": "signal", "online": True}
                assert pi.viewers == 1  # which is what keeps a real PiKVM's video running
                ws.send_text(jsonlib.dumps({"t": "key", "code": "Enter", "down": True}))
                ws.send_text("not json")
                ws.send_text(jsonlib.dumps({"event_type": "key", "event": {"key": "Sneaky"}}))  # not passed through as-is
                ws.send_text(jsonlib.dumps({"t": "button", "b": "left", "down": True}))
                for _ in range(40):
                    if len(pi.events) >= 2:
                        break
                    time.sleep(0.05)
            assert pi.events == [{"event_type": "key", "event": {"key": "Enter", "state": True}},
                                 {"event_type": "mouse_button", "event": {"button": "left", "state": True}}]
            # a page on another site can't open the control connection
            import pytest
            from starlette.websockets import WebSocketDisconnect
            with pytest.raises(WebSocketDisconnect):
                with c.websocket_connect(f"/ws/kvm/{port}", headers={"origin": "http://evil.example"}):
                    pass

            # a multi-port PiKVM Switch is pointed at the right machine first, and its buttons are per port
            c.put(f"/api/kvms/{kvm['id']}", json={**body, "port_count": 4})
            ports = c.get("/api/overview").json()["kvms"][0]["ports"]
            c.put(f"/api/devices/{nas['id']}/kvm-port", json={"port_id": ports[2]["id"]})
            pi.calls.clear()
            with c.websocket_connect(f"/ws/kvm/{ports[2]['id']}", headers=origin) as ws:
                ws.receive_text()
                assert jsonlib.loads(ws.receive_text())["state"] == "open"
            c.post(f"/api/kvm-ports/{ports[2]['id']}/power", json={"action": "press"})
            assert pi.calls == [("/api/switch/set_active", 2), ("/api/switch/atx/click", 2, "power")]

        # a different certificate at the same address is refused before the password is sent
        with FakePiKVM() as impostor:
            sqlite3.connect(db.DATA_DIR / "rackmap.db").execute("UPDATE kvms SET url = ?", (impostor.url,)).connection.commit()
            r = c.get(f"/api/kvm-ports/{ports[2]['id']}/snapshot")
            assert r.status_code == 502 and "certificate" in r.json()["detail"]

        # without an account a PiKVM is only a link, and the screen endpoints say so
        plain = c.post("/api/devices", json={"name": "Other PiKVM", "u_height": 0}).json()
        other = c.post("/api/kvms", json={"name": "Other PiKVM", "kind": "pikvm", "url": "https://10.3.0.50", "port_count": 1, "device_id": plain["id"]}).json()
        assert other["live"] is False
        assert c.get(f"/api/kvm-ports/{other['ports'][0]['id']}/snapshot").status_code == 422
    finally:
        c.__exit__(None, None, None)
