"""The ALTGEO track: what a client is allowed to see.

GPS fixes are used as-is. Where GPS was lost, the position is estimated
from the WiFi networks and BLE beacons heard at that moment, using beacon
positions learned from every GPS-fixed point on the server (all devices).
Implausible jumps are dropped. The output carries only time and position.
"""
import json
import math
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from .ingest import rssi_weight

EARTH_R = 6371000.0
MAX_BEACON_SPREAD_M = 300.0   # beacons seen over a wider area are probably moving (phone hotspots)
MAX_SPEED_MS = 70.0           # ~250 km/h: anything faster between two points is a glitch
STOP_RADIUS_M = 60.0
STOP_MIN_S = 5 * 60
MOVING_MIN_SPEED_MS = 0.5


def haversine(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def _beacon_positions(conn, keys):
    out = {}
    keys = list(keys)
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        where = " OR ".join("(kind = ? AND addr = ?)" for _ in chunk)
        args = [x for k in chunk for x in k]
        for b in conn.execute(f"SELECT * FROM beacons WHERE {where}", args):
            w = b["w_sum"]
            lat, lon = b["lat_sum"] / w, b["lon_sum"] / w
            var_lat = max(0.0, b["lat2_sum"] / w - lat * lat)
            var_lon = max(0.0, b["lon2_sum"] / w - lon * lon)
            spread = math.sqrt(var_lat + var_lon * math.cos(math.radians(lat)) ** 2) * 111320
            if spread <= MAX_BEACON_SPREAD_M:
                out[(b["kind"], b["addr"])] = (lat, lon)
    return out


def _estimate(seen, beacons):
    w_sum = lat_sum = lon_sum = 0.0
    for kind, addr, rssi in seen:
        pos = beacons.get((kind, addr))
        if pos is None or rssi is None:
            continue
        w = rssi_weight(rssi)
        w_sum += w
        lat_sum += w * pos[0]
        lon_sum += w * pos[1]
    if w_sum == 0:
        return None
    return lat_sum / w_sum, lon_sum / w_sum


def build_track(conn, source_key: str, t_from: int, t_to: int):
    rows = conn.execute(
        """SELECT ts, lat, lon, gps_ok, wifi, ble FROM raw_points
           WHERE source_key = ? AND ts BETWEEN ? AND ? ORDER BY ts, id""",
        (source_key, t_from, t_to),
    ).fetchall()

    pending, needed = [], set()
    for r in rows:
        if r["gps_ok"]:
            pending.append((r["ts"], r["lat"], r["lon"], None))
        else:
            seen = [("wifi", w.get("bssid"), w.get("rssi")) for w in json.loads(r["wifi"])]
            seen += [("ble", b.get("mac"), b.get("rssi")) for b in json.loads(r["ble"])]
            if seen:
                pending.append((r["ts"], None, None, seen))
                needed.update((k, a) for k, a, _ in seen)
    beacons = _beacon_positions(conn, needed) if needed else {}

    track = []
    for ts, lat, lon, seen in pending:
        if seen is not None:
            est = _estimate(seen, beacons)
            if est is None:
                continue
            lat, lon = est
        if track:
            last = track[-1]
            dt = ts - last["t"]
            if dt <= 0:
                continue
            if haversine(last["lat"], last["lon"], lat, lon) / dt > MAX_SPEED_MS:
                continue
        track.append({"t": ts, "lat": round(lat, 6), "lon": round(lon, 6)})
    return track


def summarize(track):
    """Distance, time on the move and stops for a report."""
    if not track:
        return {"points": 0, "distance_km": 0.0, "start": None, "end": None,
                "duration_s": 0, "moving_s": 0, "max_speed_kmh": 0.0, "stops": []}
    distance = moving = 0.0
    max_speed = 0.0
    for a, b in zip(track, track[1:]):
        d = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
        dt = b["t"] - a["t"]
        distance += d
        if dt > 0 and dt <= 600:
            speed = d / dt
            if speed >= MOVING_MIN_SPEED_MS:
                moving += dt
                max_speed = max(max_speed, speed)

    stops = []
    i = 0
    while i < len(track):
        j = i
        while j + 1 < len(track) and haversine(track[i]["lat"], track[i]["lon"],
                                               track[j + 1]["lat"], track[j + 1]["lon"]) <= STOP_RADIUS_M:
            j += 1
        if track[j]["t"] - track[i]["t"] >= STOP_MIN_S:
            stops.append({"from": track[i]["t"], "to": track[j]["t"],
                          "lat": track[i]["lat"], "lon": track[i]["lon"],
                          "duration_s": track[j]["t"] - track[i]["t"]})
            i = j + 1
        else:
            i += 1

    return {
        "points": len(track),
        "distance_km": round(distance / 1000, 2),
        "start": track[0]["t"],
        "end": track[-1]["t"],
        "duration_s": track[-1]["t"] - track[0]["t"],
        "moving_s": int(moving),
        "max_speed_kmh": round(max_speed * 3.6, 1),
        "stops": stops,
    }


def to_gpx(name: str, track) -> str:
    pts = "\n".join(
        f'      <trkpt lat="{p["lat"]}" lon="{p["lon"]}"><time>'
        f'{datetime.fromtimestamp(p["t"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}</time></trkpt>'
        for p in track
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="ALTGEO" xmlns="http://www.topografix.com/GPX/1/1">\n'
        f"  <trk><name>{escape(name)}</name>\n    <trkseg>\n{pts}\n    </trkseg>\n  </trk>\n</gpx>\n"
    )
