"""A monitoring source: a system that already collects health data from the network, which
rackmap reads and shows on the equipment it knows about. LibreNMS is the first; the rest of
rackmap only uses the small common shape below, so another system can be added beside it.

Nothing appears until a source is set up. rackmap only reads from it (a read-only API token is
enough), keeps a short-lived copy of the answers in memory and stores no history of its own.

Equipment is matched to the source's devices by IP address, then by name. A match can be set or
switched off by hand for any piece of equipment.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import socket
import ssl
import threading
import time
from urllib.parse import quote, urlsplit

from . import db, secrets_store

KINDS = {"librenms": "LibreNMS"}
TIMEOUT = 20
INTERVAL = 60          # how often devices and alerts are re-read
DETAIL_TTL = 30        # how long one device's sensors, ports and log lines are reused
LEVELS = {"ok": 0, "warning": 1, "alert": 2}
SENSOR_UNITS = {"temperature": "°C", "fanspeed": "rpm", "voltage": "V", "current": "A", "power": "W", "humidity": "%", "load": "%",
                "frequency": "Hz", "dbm": "dBm", "signal": "dBm", "runtime": "min", "charge": "%", "percent": "%"}
SENSOR_ORDER = ["temperature", "fanspeed", "power", "current", "voltage", "charge", "runtime", "load", "humidity", "state", "dbm", "signal", "frequency", "count", "percent"]
GRAPHS = {"device_bits": "Traffic", "device_processor": "Processor", "device_mempool": "Memory", "device_temperature": "Temperature",
          "device_fanspeed": "Fan speed", "device_power": "Power", "device_storage": "Storage", "device_ping_perf": "Ping response"}
PERIODS = {"day": "-1d", "week": "-7d"}


class Invalid(ValueError):
    """Message is written for the person using the app."""


class SourceError(Exception):
    """The monitoring source could not be reached or refused; the message is shown to the person."""


# ---------- talking to LibreNMS ----------

class LibreNMS:
    """Reads LibreNMS's HTTP API. `pinned` is the SHA-256 of a certificate that could not be
    verified the normal way when the source was set up (home-made certificates); with it set,
    exactly that certificate is accepted and no other."""

    def __init__(self, url: str, token: str, pinned: str = ""):
        parts = urlsplit(url)
        self.secure = parts.scheme == "https"
        self.host = parts.hostname or ""
        self.port = parts.port or (443 if self.secure else 80)
        self.base = parts.path.rstrip("/")
        self.token, self.pinned = token, pinned
        self.fingerprint = ""
        self._rules: dict[int, dict] = {}

    def _connect(self) -> http.client.HTTPConnection:
        if not self.secure:
            return http.client.HTTPConnection(self.host, self.port, timeout=TIMEOUT)
        if not self.pinned:
            return http.client.HTTPSConnection(self.host, self.port, timeout=TIMEOUT, context=ssl.create_default_context())
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # identity is checked against the remembered certificate instead
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=TIMEOUT, context=context)
        conn.connect()
        self.fingerprint = hashlib.sha256(conn.sock.getpeercert(True)).hexdigest()
        if self.fingerprint != self.pinned:
            conn.close()
            raise SourceError("The monitoring source's certificate has changed since it was set up. If that is expected, "
                              "save its settings again to accept the new certificate.")
        return conn

    def fetch(self, path: str) -> tuple[str, bytes]:
        """One GET under /api/v0. Returns the content type and the body."""
        try:
            conn = self._connect()
            try:
                conn.request("GET", f"{self.base}/api/v0{path}", headers={"X-Auth-Token": self.token, "Accept": "application/json"})
                response = conn.getresponse()
                raw = response.read(8_000_000)
            finally:
                conn.close()
        except ssl.SSLCertVerificationError as exc:
            raise SourceError("The monitoring source's certificate couldn't be verified.") from exc
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise SourceError("The monitoring source didn't answer. Check its address and that rackmap can reach it.") from exc
        if response.status in (401, 403):
            raise SourceError("The monitoring source refused that API token.")
        if response.status == 404:
            raise SourceError("The monitoring source doesn't have that. Check the address (it should be the page you open LibreNMS at).")
        if response.status >= 400:
            raise SourceError(f"The monitoring source answered with an error ({response.status}).")
        return response.getheader("Content-Type") or "", raw

    def get(self, path: str) -> dict:
        _kind, raw = self.fetch(path)
        try:
            data = json.loads(raw)
        except ValueError:
            raise SourceError("The answer wasn't in the expected format. Is that address a LibreNMS server?") from None
        if not isinstance(data, dict):
            raise SourceError("The answer wasn't in the expected format. Is that address a LibreNMS server?")
        return data

    def check(self) -> str:
        """Sign in and say what answered. Raises SourceError if it isn't a usable LibreNMS."""
        system = (self.get("/system").get("system") or [{}])[0]
        if "local_ver" not in system:
            raise SourceError("That address answered, but not like a LibreNMS server.")
        return f"LibreNMS {system['local_ver']}"

    def devices(self) -> list[dict]:
        out = []
        for d in self.get("/devices").get("devices") or []:
            if d.get("disabled"):
                continue
            out.append({
                "key": str(d["device_id"]),
                "name": d.get("display") or d.get("sysName") or d.get("hostname") or "",
                "hostname": d.get("hostname") or "", "sys_name": d.get("sysName") or "", "ip": d.get("ip") or "",
                "up": bool(d.get("status")), "ignored": bool(d.get("ignore")),
                "os": d.get("os") or "", "hardware": d.get("hardware") or "", "version": d.get("version") or "",
                "uptime": d.get("uptime"), "last_polled": d.get("last_polled") or "",
            })
        return out

    def alerts(self) -> list[dict]:
        rows = self.get("/alerts?state=1").get("alerts") or []
        if rows and any(r.get("rule_id") not in self._rules for r in rows):
            self._rules = {r["id"]: r for r in self.get("/rules").get("rules") or []}
        out = []
        for a in rows:
            rule = self._rules.get(a.get("rule_id"), {})
            severity = (a.get("severity") or rule.get("severity") or "critical").lower()
            out.append({"key": str(a.get("device_id")), "name": a.get("name") or rule.get("name") or "Alert",
                        "level": "warning" if severity == "warning" else "alert", "since": a.get("timestamp") or ""})
        return out

    def sensors(self) -> list[dict]:
        return self.get("/resources/sensors").get("sensors") or []

    def detail(self, key: str, hostname: str) -> dict:
        ports = self.get(f"/devices/{quote(key)}/ports?columns=ifName,ifOperStatus,ifAdminStatus,ifInOctets_rate,ifOutOctets_rate,ifInErrors_delta,ifOutErrors_delta").get("ports") or []
        wanted = [p for p in ports if p.get("ifAdminStatus") != "down"]
        summary = None
        if ports:
            summary = {"total": len(wanted), "up": sum(1 for p in wanted if p.get("ifOperStatus") == "up"),
                       "in_bps": int(sum((p.get("ifInOctets_rate") or 0) for p in ports) * 8),
                       "out_bps": int(sum((p.get("ifOutOctets_rate") or 0) for p in ports) * 8),
                       "errors": int(sum((p.get("ifInErrors_delta") or 0) + (p.get("ifOutErrors_delta") or 0) for p in ports))}
        events = []
        for path, when, text in (("eventlog", "datetime", "message"), ("syslog", "timestamp", "msg")):
            try:
                for row in self.get(f"/logs/{path}/{quote(hostname)}?limit=8&sortorder=DESC").get("logs") or []:
                    events.append({"time": str(row.get(when) or ""), "text": str(row.get(text) or "")[:300], "kind": "event" if path == "eventlog" else "log"})
            except SourceError:
                pass  # a source without that log is not a failure
        events.sort(key=lambda e: e["time"], reverse=True)
        offered = {"device_bits"} if ports else set()  # LibreNMS draws traffic for anything with ports but doesn't list it
        for listing in ("graphs", "health"):
            try:
                offered.update(g.get("name") for g in self.get(f"/devices/{quote(key)}/{listing}").get("graphs") or [])
            except SourceError:
                pass
        graphs = [{"name": name, "title": title} for name, title in GRAPHS.items() if name in offered][:5]
        return {"ports": summary, "events": events[:12], "graphs": graphs}

    def graph(self, key: str, name: str, period: str, full: bool = False) -> tuple[str, bytes]:
        size = "width=1000&height=260" if full else "width=380&height=120&legend=no"
        return self.fetch(f"/devices/{quote(key)}/{name}?from={PERIODS[period]}&{size}")


# ---------- settings ----------

def _saved() -> dict | None:
    raw = db.get_setting("monitoring")
    return json.loads(raw) if raw else None


def configured() -> bool:
    return _saved() is not None


def public_source() -> dict | None:
    """The saved source without its token."""
    saved = _saved()
    if not saved:
        return None
    return {"kind": saved["kind"], "url": saved["url"], "pinned": bool(saved.get("pinned")), "says": saved.get("says", "")}


def _client() -> LibreNMS | None:
    saved = _saved()
    if not saved:
        return None
    return LibreNMS(saved["url"], secrets_store.open_sealed(saved["token"]), saved.get("pinned", ""))


def _clean_url(text: str) -> str:
    text = (text or "").strip().rstrip("/")
    if text and "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.query or parts.fragment:
        raise Invalid("Enter the address you open the monitoring system at, for example https://librenms.example.net or http://192.168.1.20:8000.")
    return text


def save(values: dict) -> dict:
    """Check the address and token by signing in, then save them. An HTTPS certificate that can't be
    verified the normal way is remembered instead (and only that certificate is accepted afterwards)."""
    kind = values.get("kind") or "librenms"
    if kind not in KINDS:
        raise Invalid("Choose a kind of monitoring system from the list.")
    url = _clean_url(values.get("url") or "")
    saved = _saved()
    token = (values.get("token") or "").strip()
    if not token:
        if not saved:
            raise Invalid("Enter an API token from the monitoring system. A read-only one is enough.")
        token = secrets_store.open_sealed(saved["token"])
    client = LibreNMS(url, token)
    pinned = ""
    try:
        says = client.check()
    except SourceError as exc:
        if not (client.secure and isinstance(exc.__cause__, ssl.SSLCertVerificationError)):
            raise
        pinned = _certificate_fingerprint(client.host, client.port)
        client = LibreNMS(url, token, pinned)
        says = client.check()
    db.set_setting("monitoring", json.dumps({"kind": kind, "url": url, "token": secrets_store.seal(token), "pinned": pinned, "says": says}))
    _reset()
    refresh()
    poke()
    return public_source() or {}


def _certificate_fingerprint(host: str, port: int) -> str:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as raw, context.wrap_socket(raw, server_hostname=host) as tls:
            return hashlib.sha256(tls.getpeercert(True)).hexdigest()
    except (OSError, ssl.SSLError) as exc:
        raise SourceError("Couldn't make a secure connection to the monitoring source.") from exc


def remove() -> None:
    db.execute("DELETE FROM settings WHERE key = 'monitoring'")
    db.execute("DELETE FROM monitor_links")
    _reset()


# ---------- the short-lived copy ----------

_lock = threading.Lock()
_state: dict = {"fetched": 0.0, "error": "", "devices": [], "alerts": []}
_details: dict[str, tuple[float, dict]] = {}
_sensors: tuple[float, list[dict]] = (0.0, [])
_stop = threading.Event()
_wake = threading.Event()
_thread: threading.Thread | None = None


def _reset() -> None:
    global _sensors
    with _lock:
        _state.update({"fetched": 0.0, "error": "", "devices": [], "alerts": []})
        _details.clear()
        _sensors = (0.0, [])


def refresh() -> None:
    """Re-read devices and open alerts. A failure keeps the last good copy and records why."""
    try:
        client = _client()
    except ValueError as exc:
        with _lock:
            _state["error"] = str(exc)
        return
    if client is None:
        return
    try:
        devices, alerts = client.devices(), client.alerts()
    except SourceError as exc:
        with _lock:
            _state["error"] = str(exc)
        return
    with _lock:
        _state.update({"fetched": time.time(), "error": "", "devices": devices, "alerts": alerts})


def _copy() -> dict:
    with _lock:
        return {"fetched": _state["fetched"], "error": _state["error"], "devices": list(_state["devices"]), "alerts": list(_state["alerts"])}


def _links() -> dict[int, str]:
    return {r["device_id"]: r["source_key"] for r in db.query("SELECT device_id, source_key FROM monitor_links")}


def _short(name: str) -> str:
    name = name.strip()
    return name if re.fullmatch(r"[0-9.]+", name) else name.casefold().split(".")[0]


def matches(devices: list[dict] | None = None) -> dict[int, dict]:
    """rackmap equipment id -> the source's device. A link set by hand wins (an empty one means
    "not monitored"); otherwise the IP address decides, then the name."""
    source = devices if devices is not None else _copy()["devices"]
    by_key = {d["key"]: d for d in source}
    by_ip: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for d in source:
        for address in (d["ip"], d["hostname"]):
            if address:
                by_ip.setdefault(address.strip(), d)
        for name in (d["name"], d["sys_name"], d["hostname"]):
            if name:
                by_name.setdefault(_short(name), d)
    links = _links()
    out: dict[int, dict] = {}
    for row in db.query("SELECT id, name, ip FROM devices"):
        if row["id"] in links:
            hit = by_key.get(links[row["id"]])
        else:
            hit = by_ip.get(row["ip"].strip()) if row["ip"].strip() else None
            hit = hit or by_name.get(_short(row["name"]))
        if hit:
            out[row["id"]] = hit
    return out


def _level(device: dict, alerts: list[dict]) -> tuple[str, str]:
    mine = [a for a in alerts if a["key"] == device["key"]]
    if not device["up"] and not device["ignored"]:
        return "alert", "Not answering the monitoring system"
    if mine:
        worst = max(mine, key=lambda a: LEVELS[a["level"]])
        more = f" (and {len(mine) - 1} more)" if len(mine) > 1 else ""
        return worst["level"], worst["name"] + more
    return "ok", "No open alerts"


def health() -> dict[int, dict]:
    """For the rack view: equipment id -> how it is doing, for everything that is matched."""
    copy = _copy()
    out = {}
    for device_id, device in matches(copy["devices"]).items():
        level, why = _level(device, copy["alerts"])
        out[device_id] = {"level": level, "why": why}
    return out


def alerts_view() -> list[dict]:
    """Everything currently wrong, worst first, with the rackmap equipment it belongs to (if any)."""
    copy = _copy()
    owner = {device["key"]: device_id for device_id, device in matches(copy["devices"]).items()}
    names = {d["key"]: d["name"] for d in copy["devices"]}
    out = [{"device_id": owner.get(a["key"]), "source_name": names.get(a["key"], ""), "name": a["name"], "level": a["level"], "since": a["since"]}
           for a in copy["alerts"]]
    for d in copy["devices"]:
        if not d["up"] and not d["ignored"] and not any(a["key"] == d["key"] for a in copy["alerts"]):
            out.append({"device_id": owner.get(d["key"]), "source_name": d["name"], "name": "Not answering the monitoring system", "level": "alert", "since": ""})
    out.sort(key=lambda a: (-LEVELS[a["level"]], a["since"]))
    return out


def overview() -> dict:
    """For the Monitoring page."""
    copy = _copy()
    matched = matches(copy["devices"])
    taken = {d["key"] for d in matched.values()}
    return {"kinds": KINDS, "source": public_source(),
            "state": {"fetched": copy["fetched"], "error": copy["error"], "devices": len(copy["devices"]), "matched": len(matched)},
            "alerts": alerts_view(),
            "unmatched": [{"key": d["key"], "name": d["name"], "address": d["ip"] or d["hostname"], "up": d["up"]} for d in copy["devices"] if d["key"] not in taken]}


def set_link(device_id: int, key: str | None) -> None:
    """`None` goes back to matching by address and name; an empty string means "not monitored"."""
    if not db.query("SELECT 1 FROM devices WHERE id = ?", (device_id,)):
        raise Invalid("That equipment no longer exists.")
    if key is None:
        db.execute("DELETE FROM monitor_links WHERE device_id = ?", (device_id,))
        return
    if key and key not in {d["key"] for d in _copy()["devices"]}:
        raise Invalid("The monitoring system doesn't list that device any more.")
    db.execute("INSERT INTO monitor_links (device_id, source_key) VALUES (?, ?) ON CONFLICT(device_id) DO UPDATE SET source_key = excluded.source_key", (device_id, key))


def _sensor_rows(client: LibreNMS, key: str) -> list[dict]:
    global _sensors
    with _lock:
        fetched, rows = _sensors
    if time.time() - fetched > DETAIL_TTL:
        rows = client.sensors()
        with _lock:
            _sensors = (time.time(), rows)
    out = []
    for s in rows:
        if str(s.get("device_id")) != key or s.get("sensor_deleted"):
            continue
        value, kind = s.get("sensor_current"), s.get("sensor_class") or ""
        if value is None:
            continue
        level = "ok"
        high, low, warn, warn_low = s.get("sensor_limit"), s.get("sensor_limit_low"), s.get("sensor_limit_warn"), s.get("sensor_limit_low_warn")
        if kind != "state":
            if (high is not None and value > high) or (low is not None and value < low):
                level = "alert"
            elif (warn is not None and value > warn) or (warn_low is not None and value < warn_low):
                level = "warning"
        out.append({"kind": kind, "label": s.get("sensor_descr") or kind, "value": value, "unit": SENSOR_UNITS.get(kind, ""), "level": level})
    # Readings of exactly zero (an empty socket, a port powering nothing) go last: the first few are what the panel shows.
    out.sort(key=lambda s: (s["value"] == 0 and s["level"] == "ok", SENSOR_ORDER.index(s["kind"]) if s["kind"] in SENSOR_ORDER else 99, s["label"]))
    return out


def device_health(device_id: int) -> dict:
    """Everything the Health part of the details panel shows for one piece of equipment."""
    copy = _copy()
    link = _links().get(device_id)
    choices = [{"key": d["key"], "name": d["name"], "address": d["ip"] or d["hostname"]} for d in sorted(copy["devices"], key=lambda d: d["name"].casefold())]
    base = {"link": link, "choices": choices, "error": copy["error"], "fetched": copy["fetched"]}
    device = matches(copy["devices"]).get(device_id)
    if not device:
        return {**base, "matched": None}
    level, why = _level(device, copy["alerts"])
    matched = {k: device[k] for k in ("key", "name", "up", "os", "hardware", "version", "uptime", "last_polled")}
    matched.update({"level": level, "why": why, "address": device["ip"] or device["hostname"]})
    out = {**base, "matched": matched, "alerts": [a for a in copy["alerts"] if a["key"] == device["key"]], "sensors": [], "ports": None, "events": [], "graphs": []}
    with _lock:
        kept = _details.get(device["key"])
    if kept and time.time() - kept[0] < DETAIL_TTL:
        out.update(kept[1])
        return out
    client = _client()
    try:
        detail = client.detail(device["key"], device["hostname"])
        detail["sensors"] = _sensor_rows(client, device["key"])[:60]
    except SourceError as exc:
        out["error"] = str(exc)
        return out
    with _lock:
        _details[device["key"]] = (time.time(), detail)
    out.update(detail)
    return out


def graph(device_id: int, name: str, period: str, full: bool = False) -> tuple[str, bytes]:
    if not re.fullmatch(r"device_[a-z_]{1,40}", name) or period not in PERIODS:
        raise Invalid("That graph isn't available.")
    device = matches().get(device_id)
    client = _client()
    if not device or client is None:
        raise Invalid("That equipment isn't matched to a monitored device.")
    kind, body = client.graph(device["key"], name, period, full)
    if not kind.startswith("image/"):
        raise SourceError("The monitoring source didn't return a picture for that graph.")
    return kind, body


# ---------- background reading ----------

def _loop() -> None:
    while not _stop.is_set():
        try:
            if configured():
                refresh()
        except Exception:  # noqa: BLE001 - monitoring must never take the app down
            pass
        _wake.wait(INTERVAL)
        _wake.clear()


def start() -> None:
    global _thread
    _stop.clear()
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_loop, daemon=True, name="rackmap-monitoring-source")
        _thread.start()


def poke() -> None:
    _wake.set()


def stop() -> None:
    _stop.set()
    _wake.set()
