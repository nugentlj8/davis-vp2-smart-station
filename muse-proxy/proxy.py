#!/usr/bin/env python3
"""Muse -> Home Assistant allow-list proxy.

Muse reaches this through the gadget's home-network tunnel. This proxy holds the
Home Assistant token, so Muse never sees it, and it only exposes:

  * light on/off/brightness
  * fan on/off/speed
  * READ-ONLY: weather station sensors and their history, NWS forecast, thermostat status

Everything it can touch is listed in allowed.json (edit it any time; changes are
picked up automatically within a second or two, no rebuild needed). Anything not
listed is a 404, and the code only ever calls the services in ALLOWED_SERVICES.
There is no way to change the thermostat: climate services are not in the list.

Standard library only. Config comes from environment variables:
  HA_URL       Home Assistant address   (default http://192.168.1.100:8123)
  HA_TOKEN     long-lived access token  (required)
  PORT         listen port              (default 8765)
  URL_SECRET   optional secret path prefix, e.g. "k7f3x9" -> /k7f3x9/lights
"""
import base64
import datetime
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

HA_URL = os.environ.get("HA_URL", "http://192.168.1.100:8123").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "8765"))
URL_SECRET = os.environ.get("URL_SECRET", "").strip("/ ")
ALLOWED_FILE = os.environ.get(
    "ALLOWED_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "allowed.json")
)

if not HA_TOKEN:
    sys.exit("HA_TOKEN is not set")

# The only things this proxy will ever ask Home Assistant to do.
ALLOWED_SERVICES = {
    ("light", "turn_on"),
    ("light", "turn_off"),
    ("fan", "turn_on"),
    ("fan", "turn_off"),
    ("fan", "set_percentage"),
}
FORECAST_TYPES = ("daily", "hourly", "twice_daily")  # via weather.get_forecasts (read-only)

# Weather history comes from Home Assistant's long-term statistics (kept far longer than raw
# history). period -> (days back or None for everything, bucket size HA aggregates into)
HISTORY_PERIODS = {
    "day": (1, "hour"),
    "week": (7, "day"),
    "month": (30, "day"),
    "quarter": (91, "week"),
    "year": (365, "month"),
    "max": (None, "month"),
}
HISTORY_TYPES = ["mean", "min", "max", "change"]

CURRENT_FIELDS = (
    "temperature", "apparent_temperature", "dew_point", "humidity", "wind_speed",
    "wind_gust_speed", "wind_bearing", "pressure", "visibility", "temperature_unit",
    "wind_speed_unit", "pressure_unit", "visibility_unit",
)
FORECAST_FIELDS = (
    "datetime", "is_daytime", "condition", "temperature", "templow",
    "precipitation_probability", "wind_speed", "wind_bearing", "humidity", "detailed_description",
)


class HAError(Exception):
    pass


class NotFound(Exception):
    pass


class Config:
    """Parsed and validated allowed.json."""

    def __init__(self, raw):
        self.lights = raw.get("lights", {})
        self.fans = raw.get("fans", {})
        self.sensors = raw.get("sensors", {})
        self.thermostats = raw.get("thermostats", {})
        self.weather = raw.get("weather", {})
        self.temp_unit = raw.get("temperature_unit", "°F")

        def check(group, prefixes, label):
            for key, v in group.items():
                if not isinstance(v, dict) or not str(v.get("entity", "")).startswith(prefixes):
                    raise ValueError("%s '%s' must have an entity starting with %s" % (label, key, "/".join(prefixes)))
                if not key.replace("_", "").isalnum():
                    raise ValueError("%s id '%s' may only use letters, numbers and _" % (label, key))

        check(self.lights, ("light.",), "light")
        check(self.fans, ("fan.",), "fan")
        check(self.sensors, ("sensor.", "input_text."), "sensor")
        check(self.thermostats, ("climate.",), "thermostat")
        check(self.weather, ("weather.",), "weather")
        for key, v in list(self.lights.items()) + list(self.fans.items()):
            if not v.get("room") or not v["room"].replace("_", "").isalnum():
                raise ValueError("'%s' needs a room (letters, numbers, _)" % key)

        self.rooms = sorted({v["room"] for g in (self.lights, self.fans) for v in g.values()})
        self.control_entities = {v["entity"] for g in (self.lights, self.fans) for v in g.values()}
        self.weather_entities = {v["entity"] for v in self.weather.values()}
        self.read_entities = set(self.control_entities) | self.weather_entities
        self.read_entities |= {v["entity"] for v in self.sensors.values()}
        for t in self.thermostats.values():
            self.read_entities.add(t["entity"])
            if t.get("humidity_entity"):
                if not str(t["humidity_entity"]).startswith("sensor."):
                    raise ValueError("humidity_entity must be a sensor.")
                self.read_entities.add(t["humidity_entity"])


CFG = None
_cfg_mtime = None
_cfg_lock = threading.Lock()


def load_cfg(initial=False):
    """(Re)load allowed.json if it changed. A bad edit keeps the previous good config."""
    global CFG, _cfg_mtime
    mtime = None
    try:
        mtime = os.stat(ALLOWED_FILE).st_mtime
        if mtime == _cfg_mtime and CFG is not None:
            return
        with open(ALLOWED_FILE, encoding="utf-8") as f:
            new = Config(json.load(f))
        with _cfg_lock:
            CFG, _cfg_mtime = new, mtime
        print("config %s: %d lights, %d fans, %d sensors, %d thermostats, %d weather" % (
            "loaded" if initial else "reloaded", len(new.lights), len(new.fans),
            len(new.sensors), len(new.thermostats), len(new.weather)), flush=True)
    except Exception as e:
        if initial:
            sys.exit("Could not load %s: %s" % (ALLOWED_FILE, e))
        with _cfg_lock:
            _cfg_mtime = mtime
        print("allowed.json has a problem, keeping the previous version: %s" % e, flush=True)


def ha_request(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        HA_URL + path,
        data=data,
        method=method,
        headers={"Authorization": "Bearer " + HA_TOKEN, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raise HAError("Home Assistant returned HTTP %d" % e.code)
    except Exception:
        raise HAError("Could not reach Home Assistant")


def read_states(c):
    """One call to HA, filtered down to allowed entities before anything is returned."""
    states = ha_request("GET", "/api/states") or []
    return {s["entity_id"]: s for s in states if s.get("entity_id") in c.read_entities}


def call_service(c, domain, service, entities, **data):
    if (domain, service) not in ALLOWED_SERVICES:
        raise NotFound("service not allowed")
    entities = list(entities)
    if not entities or any(e not in c.control_entities or e.split(".")[0] != domain for e in entities):
        raise NotFound("entity not allowed")
    ha_request("POST", "/api/services/%s/%s" % (domain, service), {"entity_id": entities, **data})


def get_forecast(c, entity, ftype):
    """Read-only: weather.get_forecasts for one configured weather entity."""
    if entity not in c.weather_entities or ftype not in FORECAST_TYPES:
        raise NotFound("forecast not allowed")
    resp = ha_request("POST", "/api/services/weather/get_forecasts?return_response",
                      {"entity_id": entity, "type": ftype}) or {}
    return ((resp.get("service_response") or {}).get(entity) or {}).get("forecast") or []


# ---- minimal read-only websocket client (stdlib) ----
# Long-term statistics are only available over HA's websocket API. The proxy sends exactly one
# fixed command type (recorder/statistics_during_period) and nothing else.
def _ws_recv_frame(f):
    def readn(n):
        buf = f.read(n)
        if len(buf) < n:
            raise HAError("Could not reach Home Assistant")
        return buf
    b1, b2 = readn(2)
    op, ln = b1 & 0x0F, b2 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", readn(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", readn(8))[0]
    if ln > 32 * 1024 * 1024:
        raise HAError("Home Assistant reply too large")
    if b2 & 0x80:
        mask = readn(4)
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(readn(ln)))
    else:
        data = readn(ln)
    return bool(b1 & 0x80), op, data


def _ws_send(sock, obj):
    data = json.dumps(obj).encode()
    mask = os.urandom(4)
    n = len(data)
    head = b"\x81" + (bytes([0x80 | n]) if n < 126 else b"\xfe" + struct.pack(">H", n) if n < 65536
                      else b"\xff" + struct.pack(">Q", n))
    sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _ws_recv_json(sock, f):
    msg = b""
    while True:
        fin, op, data = _ws_recv_frame(f)
        if op == 9:  # ping -> pong
            sock.sendall(b"\x8a\x80" + os.urandom(4))
            continue
        if op == 8:
            raise HAError("Home Assistant closed the connection")
        msg += data
        if fin and op in (0, 1):
            return json.loads(msg)


def ha_statistics(statistic_ids, start_iso, end_iso, period):
    """recorder/statistics_during_period over a short-lived websocket."""
    u = urlsplit(HA_URL)
    secure = u.scheme == "https"
    host, port = u.hostname, u.port or (443 if secure else 80)
    try:
        sock = socket.create_connection((host, port), timeout=15)
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        sock.settimeout(30)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(("GET %s/api/websocket HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
                      % (u.path.rstrip("/"), host, port, key)).encode())
        f = sock.makefile("rb")
        status = f.readline(4096)
        if b" 101 " not in status:
            raise HAError("Home Assistant refused the websocket")
        for _ in range(100):  # skip response headers
            if f.readline(4096) in (b"\r\n", b"\n", b""):
                break
        if _ws_recv_json(sock, f).get("type") != "auth_required":
            raise HAError("Unexpected reply from Home Assistant")
        _ws_send(sock, {"type": "auth", "access_token": HA_TOKEN})
        if _ws_recv_json(sock, f).get("type") != "auth_ok":
            raise HAError("Home Assistant rejected the token")
        _ws_send(sock, {"id": 1, "type": "recorder/statistics_during_period", "start_time": start_iso,
                        "end_time": end_iso, "statistic_ids": list(statistic_ids), "period": period,
                        "types": HISTORY_TYPES})
        while True:
            m = _ws_recv_json(sock, f)
            if m.get("id") == 1 and m.get("type") == "result":
                if not m.get("success"):
                    raise HAError("Home Assistant could not return history")
                return m.get("result") or {}
    except HAError:
        raise
    except Exception:
        raise HAError("Could not reach Home Assistant")
    finally:
        try:
            sock.close()
        except Exception:
            pass


_tz_cache = {}


def ha_timezone():
    """HA's own time zone (for readable bucket labels). Falls back to UTC."""
    if "tz" not in _tz_cache:
        tz = datetime.timezone.utc
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo((ha_request("GET", "/api/config") or {}).get("time_zone") or "UTC")
        except Exception:
            pass
        _tz_cache["tz"] = tz
    return _tz_cache["tz"]


def bucket_label(ts, bucket):
    if isinstance(ts, (int, float)):
        ts = ts / 1000.0 if ts > 1e11 else ts
        dt = datetime.datetime.fromtimestamp(ts, ha_timezone())
    else:
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(ha_timezone())
    return {"hour": dt.strftime("%Y-%m-%d %H:%M"), "day": dt.strftime("%Y-%m-%d"),
            "week": dt.strftime("%Y-%m-%d"), "month": dt.strftime("%Y-%m"), "year": dt.strftime("%Y")}[bucket]


def history_view(c, period, only):
    if period not in HISTORY_PERIODS:
        raise NotFound("unknown period. Use: " + ", ".join(HISTORY_PERIODS))
    days, bucket = HISTORY_PERIODS[period]
    chosen = {k: v for k, v in c.sensors.items() if (not only or k in only) and v.get("history", True)
              and v["entity"].startswith("sensor.")}
    for k in only or ():
        if k not in c.sensors:
            raise NotFound("unknown sensor '%s'" % k)
    now = datetime.datetime.now(datetime.timezone.utc)
    start = (now - datetime.timedelta(days=days)) if days else datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
    stats = ha_statistics([v["entity"] for v in chosen.values()], start.isoformat(), now.isoformat(), bucket)
    st = read_states(c)
    out, missing = [], []
    for key, info in chosen.items():
        rows = stats.get(info["entity"]) or []
        if not rows:
            missing.append(key)
            continue
        buckets = []
        for r in rows:
            b = {"start": bucket_label(r.get("start"), bucket)}
            for f in HISTORY_TYPES:
                if r.get(f) is not None:
                    b[f] = round(r[f], 2)
            buckets.append(b)
        out.append({"id": key, "name": info["name"],
                    "unit": st.get(info["entity"], {}).get("attributes", {}).get("unit_of_measurement"),
                    "buckets": buckets})
    return {"ok": True, "period": period, "bucket": bucket, "sensors": out,
            "no_history": missing,
            "note": "mean/min/max per bucket. For rain, 'change' is the amount that fell in the bucket."}


# Imported Davis console archive (external statistics "weather:<scale>_<metric>"). These are the
# station's own records: yearly highs/lows/rain since 2005, monthly for ~2 years, daily for the
# last few weeks. Each row holds one value (stored in mean/min/max). Read-only, fixed id list.
ARCHIVE_SCALES = ("year", "month", "day", "hour")
ARCHIVE_METRICS = {
    "temp_high": ("Highest temperature", "°F"), "temp_low": ("Lowest temperature", "°F"),
    "temp": ("Temperature", "°F"), "rain_total": ("Total rainfall", "in"),
    "rain_rate_max": ("Peak rain rate", "in/h"), "wind_high": ("Highest wind speed", "mph"),
    "hum_high": ("Highest humidity", "%"), "hum_low": ("Lowest humidity", "%"),
    "humidity": ("Humidity", "%"), "baro_avg": ("Average barometer", "inHg"),
    "barometer": ("Barometer", "inHg"),
}


def archive_view(c, scale):
    if scale not in ARCHIVE_SCALES:
        raise NotFound("unknown scale. Use: " + ", ".join(ARCHIVE_SCALES))
    ids = {"weather:%s_%s" % (scale, m): m for m in ARCHIVE_METRICS}
    now = datetime.datetime.now(datetime.timezone.utc)
    start = datetime.datetime(1990, 1, 1, tzinfo=datetime.timezone.utc)
    stats = ha_statistics(list(ids), start.isoformat(), now.isoformat(), "hour")
    out = []
    for sid, m in ids.items():
        rows = stats.get(sid) or []
        if not rows:
            continue
        label = scale
        vals = []
        for r in rows:
            v = next((r[f] for f in ("mean", "max", "min") if r.get(f) is not None), None)
            if v is not None:
                vals.append({"at": bucket_label(r.get("start"), label), "value": round(v, 2)})
        name, unit = ARCHIVE_METRICS[m]
        out.append({"id": m, "name": name, "unit": unit, "values": vals})
    return {"ok": True, "scale": scale, "series": out,
            "note": "Davis console archive records (one value per %s), not raw readings. 'year' goes "
                    "back to 2005." % scale}


def parse_int(value, label, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a whole number from %d to %d" % (label, lo, hi))
    if not lo <= n <= hi:
        raise ValueError("%s must be a whole number from %d to %d" % (label, lo, hi))
    return n


def parse_pct(value, label):
    return parse_int(value, label, 0, 100)


def light_view(key, info, states):
    s = states.get(info["entity"], {})
    a = s.get("attributes", {})
    state = s.get("state", "unknown")
    b = a.get("brightness")
    return {
        "id": key,
        "name": info["name"],
        "room": info["room"],
        "state": state,
        "brightness_pct": round(b * 100 / 255) if (state == "on" and b is not None) else None,
    }


def fan_view(key, info, states):
    s = states.get(info["entity"], {})
    return {
        "id": key,
        "name": info["name"],
        "room": info["room"],
        "state": s.get("state", "unknown"),
        "speed_pct": s.get("attributes", {}).get("percentage"),
    }


def sensor_view(key, info, states):
    s = states.get(info["entity"], {})
    return {
        "id": key,
        "name": info["name"],
        "value": s.get("state", "unknown"),
        "unit": s.get("attributes", {}).get("unit_of_measurement"),
    }


def thermostat_view(c, key, info, states):
    s = states.get(info["entity"], {})
    a = s.get("attributes", {})
    out = {"id": key, "name": info["name"], "read_only": True,
           "mode": s.get("state", "unknown"), "unit": c.temp_unit}
    for src, dst in (("current_temperature", "current_temp"), ("temperature", "target_temp"),
                     ("target_temp_high", "target_high"), ("target_temp_low", "target_low"),
                     ("hvac_action", "action"), ("preset_mode", "preset"), ("fan_mode", "fan")):
        if a.get(src) is not None:
            out[dst] = a[src]
    hum = states.get(info.get("humidity_entity") or "", {}).get("state")
    if hum not in (None, "unknown", "unavailable"):
        out["humidity_pct"] = hum
    return out


def current_view(key, info, states):
    s = states.get(info["entity"], {})
    a = s.get("attributes", {})
    out = {"id": key, "name": info["name"], "condition": s.get("state", "unknown")}
    for k in CURRENT_FIELDS:
        if a.get(k) is not None:
            out[k] = a[k]
    return out


def slim_forecast(items, limit):
    out = []
    for item in items[:limit]:
        row = {k: item[k] for k in FORECAST_FIELDS if item.get(k) is not None}
        if "detailed_description" in row:
            row["detailed_description"] = str(row["detailed_description"])[:400]
        out.append(row)
    return out


def help_doc(c):
    return {
        "ok": True,
        "about": "Home control for lights and fans, plus read-only weather, forecast and thermostat "
        "status. The thermostat can be read but not changed. Every URL works with a plain GET.",
        "endpoints": {
            "/forecast": "NWS current conditions + 7-day forecast",
            "/forecast/detailed": "NWS day/night forecast with descriptions (5 days)",
            "/forecast/hourly": "NWS next 12 hours (optional ?hours=1-24)",
            "/weather": "weather station readings, plus the AI weather ticker",
            "/sensor/<id>": "one weather reading",
            "/history/<period>": "weather station history for week, month, quarter, year or max "
            "(also day). Daily/weekly/monthly mean, min, max (rain: change). "
            "Optional ?sensors=id1,id2 or /history/<period>/<sensor_id>",
            "/archive/<scale>": "OLD station records imported from the Davis console. year = highs, lows, "
            "rain, wind per year since 2005 (start here for long-term/'all time' questions); month = "
            "last ~2 years; day = last few weeks. /history only covers June 2026 onward",
            "/thermostat": "thermostat status (read-only: temperature, setpoint, mode, humidity)",
            "/lights": "state of every light",
            "/light/<id>": "state of one light",
            "/light/<id>/on": "turn on (optional ?brightness=0-100)",
            "/light/<id>/off": "turn off",
            "/light/<id>/brightness/<0-100>": "set brightness (0 = off)",
            "/lights/on | /lights/off": "all lights",
            "/fans": "state of every fan",
            "/fan/<id>": "state of one fan",
            "/fan/<id>/on | /fan/<id>/off": "fan power",
            "/fan/<id>/speed/<0-100>": "fan speed (0 = off)",
            "/room/<room>": "lights and fans in a room",
            "/room/<room>/lights/on | off": "lights in a room",
            "/room/<room>/fans/on | off": "fans in a room",
            "/room/<room>/off": "lights and fans in a room, off",
        },
        "lights": {k: "%s (%s)" % (v["name"], v["room"]) for k, v in c.lights.items()},
        "fans": {k: "%s (%s)" % (v["name"], v["room"]) for k, v in c.fans.items()},
        "weather": {k: v["name"] for k, v in c.sensors.items()},
        "rooms": c.rooms,
    }


def room_items(c, room):
    if room not in c.rooms:
        raise NotFound("unknown room")
    lights = {k: v for k, v in c.lights.items() if v["room"] == room}
    fans = {k: v for k, v in c.fans.items() if v["room"] == room}
    return lights, fans


def power(c, domain, group, turn_on):
    ents = [v["entity"] for v in group.values()]
    if not ents:
        return []
    call_service(c, domain, "turn_on" if turn_on else "turn_off", ents)
    return list(group)


def route(c, segs, query):
    """Returns (status, body). Raises NotFound / ValueError / HAError."""
    if not segs or segs == ["help"]:
        return 200, help_doc(c)
    if segs == ["health"]:
        return 200, {"ok": True}

    head = segs[0]

    # ---- read-only weather ----
    if head in ("weather", "sensors") and len(segs) == 1:
        st = read_states(c)
        return 200, {"ok": True, "sensors": [sensor_view(k, v, st) for k, v in c.sensors.items()]}
    if head == "sensor" and len(segs) == 2:
        if segs[1] not in c.sensors:
            raise NotFound("unknown sensor")
        st = read_states(c)
        return 200, {"ok": True, **sensor_view(segs[1], c.sensors[segs[1]], st)}

    # ---- read-only weather history (long-term statistics) ----
    if head == "history" and len(segs) <= 3:
        period = segs[1] if len(segs) >= 2 else (query.get("period") or ["week"])[0]
        only = []
        if len(segs) == 3:
            only = [segs[2]]
        elif query.get("sensors"):
            only = [s for s in query["sensors"][0].split(",") if s]
        return 200, history_view(c, period, only)

    # ---- read-only archive of imported station records (years back to 2005) ----
    if head == "archive" and len(segs) <= 2:
        return 200, archive_view(c, segs[1] if len(segs) == 2 else "year")

    # ---- read-only NWS forecast ----
    if head == "forecast" and len(segs) <= 2:
        if not c.weather:
            raise NotFound("no forecast configured")
        key, info = next(iter(c.weather.items()))
        kind = segs[1] if len(segs) == 2 else "daily"
        if kind not in ("daily", "hourly", "detailed"):
            raise NotFound("unknown forecast type")
        current = current_view(key, info, read_states(c))
        if kind == "hourly":
            hours = parse_int(query.get("hours", ["12"])[0], "hours", 1, 24)
            items, ftype, limit = get_forecast(c, info["entity"], "hourly"), "hourly", hours
        elif kind == "detailed":
            items, ftype, limit = get_forecast(c, info["entity"], "twice_daily"), "twice_daily", 10
        else:
            try:
                items, ftype, limit = get_forecast(c, info["entity"], "daily"), "daily", 7
            except HAError:  # some NWS versions only offer day/night periods
                items, ftype, limit = get_forecast(c, info["entity"], "twice_daily"), "twice_daily", 10
        return 200, {"ok": True, "source": info["name"], "type": ftype, "current": current,
                     "forecast": slim_forecast(items, limit)}

    # ---- read-only thermostat (there is deliberately no way to change it) ----
    if head == "thermostat" and len(segs) <= 2:
        st = read_states(c)
        if len(segs) == 1:
            return 200, {"ok": True, "thermostats": [thermostat_view(c, k, v, st) for k, v in c.thermostats.items()]}
        if segs[1] not in c.thermostats:
            raise NotFound("unknown thermostat")
        return 200, {"ok": True, **thermostat_view(c, segs[1], c.thermostats[segs[1]], st)}

    # ---- lights ----
    if head == "lights":
        if len(segs) == 1:
            st = read_states(c)
            return 200, {"ok": True, "lights": [light_view(k, v, st) for k, v in c.lights.items()]}
        if len(segs) == 2 and segs[1] in ("on", "off"):
            done = power(c, "light", c.lights, segs[1] == "on")
            return 200, {"ok": True, "action": "all lights " + segs[1], "lights": done}
        raise NotFound("unknown path")

    if head == "light" and len(segs) >= 2:
        key = segs[1]
        if key not in c.lights:
            raise NotFound("unknown light")
        info = c.lights[key]
        if len(segs) == 2:
            return 200, {"ok": True, **light_view(key, info, read_states(c))}
        action = segs[2]
        if action == "off" and len(segs) == 3:
            call_service(c, "light", "turn_off", [info["entity"]])
            return 200, {"ok": True, "action": "off", "light": key}
        if action == "on" and len(segs) == 3:
            if "brightness" in query:
                n = parse_pct(query["brightness"][0], "brightness")
                if n == 0:
                    call_service(c, "light", "turn_off", [info["entity"]])
                    return 200, {"ok": True, "action": "off", "light": key}
                call_service(c, "light", "turn_on", [info["entity"]], brightness_pct=n)
                return 200, {"ok": True, "action": "on", "brightness_pct": n, "light": key}
            call_service(c, "light", "turn_on", [info["entity"]])
            return 200, {"ok": True, "action": "on", "light": key}
        if action == "brightness" and len(segs) == 4:
            n = parse_pct(segs[3], "brightness")
            if n == 0:
                call_service(c, "light", "turn_off", [info["entity"]])
                return 200, {"ok": True, "action": "off", "light": key}
            call_service(c, "light", "turn_on", [info["entity"]], brightness_pct=n)
            return 200, {"ok": True, "action": "on", "brightness_pct": n, "light": key}
        raise NotFound("unknown path")

    # ---- fans ----
    if head == "fans" and len(segs) == 1:
        st = read_states(c)
        return 200, {"ok": True, "fans": [fan_view(k, v, st) for k, v in c.fans.items()]}

    if head == "fan" and len(segs) >= 2:
        key = segs[1]
        if key not in c.fans:
            raise NotFound("unknown fan")
        info = c.fans[key]
        if len(segs) == 2:
            return 200, {"ok": True, **fan_view(key, info, read_states(c))}
        action = segs[2]
        if action == "on" and len(segs) == 3:
            call_service(c, "fan", "turn_on", [info["entity"]])
            return 200, {"ok": True, "action": "on", "fan": key}
        if action == "off" and len(segs) == 3:
            call_service(c, "fan", "turn_off", [info["entity"]])
            return 200, {"ok": True, "action": "off", "fan": key}
        if action == "speed" and len(segs) == 4:
            n = parse_pct(segs[3], "speed")
            if n == 0:
                call_service(c, "fan", "turn_off", [info["entity"]])
                return 200, {"ok": True, "action": "off", "fan": key}
            call_service(c, "fan", "set_percentage", [info["entity"]], percentage=n)
            return 200, {"ok": True, "action": "speed", "speed_pct": n, "fan": key}
        raise NotFound("unknown path")

    # ---- rooms ----
    if head == "room" and len(segs) >= 2:
        lights, fans = room_items(c, segs[1])
        if len(segs) == 2:
            st = read_states(c)
            return 200, {
                "ok": True,
                "room": segs[1],
                "lights": [light_view(k, v, st) for k, v in lights.items()],
                "fans": [fan_view(k, v, st) for k, v in fans.items()],
            }
        if len(segs) == 3 and segs[2] == "off":
            return 200, {
                "ok": True,
                "action": "room off",
                "lights": power(c, "light", lights, False),
                "fans": power(c, "fan", fans, False),
            }
        if len(segs) == 4 and segs[3] in ("on", "off"):
            on = segs[3] == "on"
            if segs[2] == "lights":
                return 200, {"ok": True, "action": "lights " + segs[3], "lights": power(c, "light", lights, on)}
            if segs[2] == "fans":
                return 200, {"ok": True, "action": "fans " + segs[3], "fans": power(c, "fan", fans, on)}
        raise NotFound("unknown path")

    raise NotFound("unknown path")


class Handler(BaseHTTPRequestHandler):
    server_version = "muse-proxy"
    sys_version = ""

    def _send(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)
        self.log_line(status)

    def log_line(self, status):
        # Log the path only (never headers/token). Strip the secret prefix from logs.
        path = urlsplit(self.path).path
        if URL_SECRET:
            path = path.replace("/" + URL_SECRET, "/<secret>", 1)
        print("%s %s -> %s" % (self.command, path[:200], status), flush=True)

    def _handle(self):
        parts = urlsplit(self.path)
        segs = [unquote(s) for s in parts.path.split("/") if s]
        if URL_SECRET:
            if not segs or segs[0] != URL_SECRET:
                return self._send(404, {"ok": False, "error": "not found"})
            segs = segs[1:]
        load_cfg()
        try:
            status, body = route(CFG, segs, parse_qs(parts.query))
        except NotFound as e:
            status, body = 404, {"ok": False, "error": str(e) + ". Try /help"}
        except ValueError as e:
            status, body = 400, {"ok": False, "error": str(e)}
        except HAError as e:
            status, body = 502, {"ok": False, "error": str(e)}
        except Exception:
            status, body = 500, {"ok": False, "error": "internal error"}
        self._send(status, body)

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *args):  # silence default logging (we log our own line)
        pass


if __name__ == "__main__":
    load_cfg(initial=True)
    print("muse-proxy listening on :%d -> %s%s" % (
        PORT, HA_URL, ", secret prefix ON" if URL_SECRET else ", no secret prefix"), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
