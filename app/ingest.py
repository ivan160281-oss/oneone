"""Turning what the trackers send into raw_points rows (+ learning beacons).

Two firmwares feed this:
  * wifigps (SD card) uploads whole log files, one line every 30 s:
        HH:MM:SS_lat_lon_status_speed_<wifi>_<empty>_<ble>_heading_steps
    wifi = ssid|bssid|rssi,...   ble = MAC|rssi,...
  * WIFI_GPS_T-CALL (GSM) posts JSON points with a per-device seq number.
"""
from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timedelta, timezone

FILENAME_RE = re.compile(r"^log_(\d{8})_(\d{6})\.txt$")
UPTIME_FILENAME_RE = re.compile(r"^log_uptime_\d+\.txt$")
WIFI_ENTRY_RE = re.compile(r"(.*?)\|([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\|(-?\d+)(?:,|$)")
BLE_ENTRY_RE = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\|(-?\d+)")
# The GSM firmware doesn't JSON-escape SSIDs, so a '"' or '\' in a network
# name would make the whole batch invalid JSON and stall its queue forever.
SSID_FIX_RE = re.compile(r'"ssid":"(.*?)","bssid"')


def valid_log_filename(name: str) -> bool:
    return bool(FILENAME_RE.match(name) or UPTIME_FILENAME_RE.match(name))


def rssi_weight(rssi: float) -> float:
    return 10 ** ((max(min(rssi, -20), -110) + 100) / 20)


def _float(s):
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def parse_log_line(line: str):
    """One SD-log line -> dict, or None if it doesn't look like one."""
    line = line.strip()
    if not line:
        return None
    head = line.split("_", 5)
    if len(head) < 6:
        return None
    hms, lat, lon, status, _speed, rest = head
    tail = rest.rsplit("_", 2)
    if len(tail) < 3:
        return None
    middle, heading, steps = tail
    if "__" in middle:
        wifi_part, ble_part = middle.rsplit("__", 1)
    else:
        wifi_part, ble_part = middle, ""
    try:
        h, m, s = (int(x) for x in hms.split(":"))
    except ValueError:
        return None
    wifi = [
        {"ssid": ssid, "bssid": bssid.upper(), "rssi": int(rssi)}
        for ssid, bssid, rssi in WIFI_ENTRY_RE.findall(wifi_part)
    ]
    ble = [{"mac": mac.upper(), "rssi": int(rssi)} for mac, rssi in BLE_ENTRY_RE.findall(ble_part)]
    lat_f, lon_f = _float(lat), _float(lon)
    gps_ok = status == "ok" and lat_f is not None and lon_f is not None and not (lat_f == 0 and lon_f == 0)
    return {
        "seconds": h * 3600 + m * 60 + s,
        "lat": lat_f if gps_ok else None,
        "lon": lon_f if gps_ok else None,
        "gps_ok": gps_ok,
        "wifi": wifi,
        "ble": ble,
        "heading": _float(heading),
        "steps": _int(steps),
    }


def parse_log_file(filename: str, text: str, now: float | None = None):
    """Whole SD log -> list of point dicts with absolute UTC ts.

    The line only carries HH:MM:SS; the date comes from the file name
    (log_YYYYMMDD_HHMMSS.txt). log_uptime_*.txt files were written before the
    clock was known, so their date is taken as the upload day. A line whose
    time is earlier than the previous one means midnight was crossed.
    """
    m = FILENAME_RE.match(filename)
    if m:
        start = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    else:
        start = datetime.fromtimestamp(now or time.time(), timezone.utc)
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    prev = start.hour * 3600 + start.minute * 60 + start.second if m else None
    points = []
    for line in text.splitlines():
        p = parse_log_line(line)
        if p is None:
            continue
        if prev is not None and p["seconds"] < prev - 3600:
            day += timedelta(days=1)
        prev = p["seconds"]
        p["ts"] = int((day + timedelta(seconds=p.pop("seconds"))).timestamp())
        points.append(p)
    return points


def parse_realtime_body(raw: bytes):
    text = raw.decode("utf-8", errors="replace")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        fixed = SSID_FIX_RE.sub(lambda mm: '"ssid":' + json.dumps(mm.group(1)) + ',"bssid"', text)
        doc = json.loads(fixed)
    points = doc.get("points") if isinstance(doc, dict) else None
    if not isinstance(points, list):
        raise ValueError("no points array")
    return points


def realtime_point(p: dict, received_at: int):
    """GSM-tracker JSON point -> point dict, or None if unusable."""
    seq = p.get("seq")
    if not isinstance(seq, int) or seq < 0:
        return None
    ts = received_at
    raw_ts = p.get("ts")
    if isinstance(raw_ts, str) and not raw_ts.startswith("uptime_"):
        try:
            ts = int(datetime.strptime(raw_ts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            pass
    lat, lon = _float(p.get("lat")), _float(p.get("lon"))
    gps_ok = p.get("status") == "ok" and lat is not None and lon is not None and not (lat == 0 and lon == 0)
    wifi = [
        {"ssid": str(w.get("ssid", "")), "bssid": str(w.get("bssid", "")).upper(), "rssi": _int(w.get("rssi"))}
        for w in p.get("wifi") or [] if isinstance(w, dict) and w.get("bssid")
    ]
    ble = [
        {"mac": str(b.get("mac", "")).upper(), "rssi": _int(b.get("rssi"))}
        for b in p.get("ble") or [] if isinstance(b, dict) and b.get("mac")
    ]
    return {
        "seq": seq, "ts": ts, "lat": lat if gps_ok else None, "lon": lon if gps_ok else None,
        "gps_ok": gps_ok, "wifi": wifi, "ble": ble, "heading": None, "steps": None,
    }


def _learn_beacons(conn, p):
    if not p["gps_ok"]:
        return
    seen = [("wifi", w["bssid"], w["rssi"]) for w in p["wifi"]] + [("ble", b["mac"], b["rssi"]) for b in p["ble"]]
    for kind, addr, rssi in seen:
        if rssi is None:
            continue
        w = rssi_weight(rssi)
        lat, lon = p["lat"], p["lon"]
        conn.execute(
            """INSERT INTO beacons (kind, addr, w_sum, lat_sum, lon_sum, lat2_sum, lon2_sum, n)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT (kind, addr) DO UPDATE SET
                 w_sum = w_sum + excluded.w_sum, lat_sum = lat_sum + excluded.lat_sum,
                 lon_sum = lon_sum + excluded.lon_sum, lat2_sum = lat2_sum + excluded.lat2_sum,
                 lon2_sum = lon2_sum + excluded.lon2_sum, n = n + 1""",
            (kind, addr, w, w * lat, w * lon, w * lat * lat, w * lon * lon),
        )


def store_points(conn, source_key: str, points, file: str | None = None) -> list[int]:
    """Insert points; returns the seqs that are now stored (new or duplicate)."""
    now = int(time.time())
    stored = []
    for p in points:
        cur = conn.execute(
            """INSERT OR IGNORE INTO raw_points
               (source_key, ts, lat, lon, gps_ok, wifi, ble, heading, steps, seq, file, received_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source_key, p["ts"], p["lat"], p["lon"], int(p["gps_ok"]), json.dumps(p["wifi"]),
             json.dumps(p["ble"]), p["heading"], p["steps"], p.get("seq"), file, now),
        )
        if cur.rowcount:
            _learn_beacons(conn, p)
        if p.get("seq") is not None:
            stored.append(p["seq"])
    return stored
