"""A stand-in LibreNMS for tests: just enough of its HTTP API for rackmap's monitoring source."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def device(device_id, hostname, display, status=1, sys_name=None, hardware="", version="", uptime=1000):
    return {"device_id": device_id, "hostname": hostname, "sysName": sys_name or hostname, "ip": hostname if hostname[0].isdigit() else "",
            "display": display, "os": "routeros", "hardware": hardware, "version": version, "status": status, "ignore": 0, "disabled": 0,
            "uptime": uptime, "last_polled": "2026-01-01T00:00:00.000000Z"}


class FakeLibreNMS:
    def __init__(self, token="lnms-token"):
        self.token = token
        self.devices = [
            device(1, "192.168.7.2", "Core switch", hardware="CRS328", version="7.1", uptime=200000),
            device(2, "nas.lan", "nas.lan", status=0),
            device(3, "192.168.7.9", "Access point"),
        ]
        self.alerts = [{"id": 5, "device_id": 1, "rule_id": 9, "state": 1, "timestamp": "2026-01-01 10:00:00", "severity": "warning"}]
        self.rules = [{"id": 9, "name": "Port Speed Degraded", "severity": "warning"}]
        self.sensors = [
            {"sensor_id": 1, "device_id": 1, "sensor_class": "temperature", "sensor_descr": "Board", "sensor_current": 71.5, "sensor_limit": 70, "sensor_deleted": 0},
            {"sensor_id": 2, "device_id": 1, "sensor_class": "fanspeed", "sensor_descr": "Fan 1", "sensor_current": 4200, "sensor_limit": None, "sensor_deleted": 0},
            {"sensor_id": 3, "device_id": 3, "sensor_class": "temperature", "sensor_descr": "Radio", "sensor_current": 40, "sensor_limit": 80, "sensor_deleted": 0},
        ]
        self.paths = []
        self._server = None
        self.port = None

    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, status, body, kind="application/json"):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                path = urlsplit(self.path).path
                fake.paths.append(path)
                if self.headers.get("X-Auth-Token") != fake.token:
                    return self._send(401, {"message": "Unauthenticated."})
                if not path.startswith("/api/v0/"):
                    return self._send(404, {"status": "error"})
                parts = path[len("/api/v0/"):].split("/")
                if parts == ["system"]:
                    return self._send(200, {"status": "ok", "system": [{"local_ver": "26.11.0"}]})
                if parts == ["devices"]:
                    return self._send(200, {"status": "ok", "devices": fake.devices})
                if parts == ["alerts"]:
                    return self._send(200, {"status": "ok", "alerts": fake.alerts})
                if parts == ["rules"]:
                    return self._send(200, {"status": "ok", "rules": fake.rules})
                if parts == ["resources", "sensors"]:
                    return self._send(200, {"status": "ok", "sensors": fake.sensors})
                if len(parts) == 3 and parts[0] == "devices" and parts[2] == "ports":
                    return self._send(200, {"status": "ok", "ports": [
                        {"ifName": "ether1", "ifOperStatus": "up", "ifAdminStatus": "up", "ifInOctets_rate": 1000, "ifOutOctets_rate": 500, "ifInErrors_delta": 0, "ifOutErrors_delta": 0},
                        {"ifName": "ether2", "ifOperStatus": "down", "ifAdminStatus": "up", "ifInOctets_rate": None, "ifOutOctets_rate": None, "ifInErrors_delta": 3, "ifOutErrors_delta": None},
                        {"ifName": "ether3", "ifOperStatus": "down", "ifAdminStatus": "down"}]})
                if len(parts) == 3 and parts[0] == "devices" and parts[2] == "graphs":
                    return self._send(200, {"status": "ok", "graphs": [{"name": "device_poller_perf", "desc": "Poller Time"}, {"name": "device_bits", "desc": "Traffic"}]})
                if len(parts) == 3 and parts[0] == "logs":
                    key = "datetime" if parts[1] == "eventlog" else "timestamp"
                    text = "message" if parts[1] == "eventlog" else "msg"
                    return self._send(200, {"status": "ok", "logs": [{key: "2026-01-01 09:00:00", text: f"{parts[1]} line for {parts[2]}"}]})
                if len(parts) == 3 and parts[0] == "devices" and parts[2].startswith("device_"):
                    return self._send(200, b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml")
                return self._send(404, {"status": "error"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self.port

    def stop(self):
        server, self._server = self._server, None
        if server:
            server.shutdown()
            server.server_close()
