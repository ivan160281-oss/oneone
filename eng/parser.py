"""
Parses log_YYYYMMDD_HHMMSS.txt files produced by the T-LoRa Pager GPS+WiFi+
BLE+IMU tracker firmware.

Line format (underscore-separated):
    HH:MM:SS_lat_lon_status_speed_ssid1|bssid1|rssi1,...__mac1|rssi1,..._heading_steps

- status is "ok" or "bad_gps"
- speed is a decimal km/h value, or "NA" (the firmware no longer computes speed
  at all, so this is always "NA" for anything logged going forward - kept only
  for backward compatibility with older files that do have real values)
- the wifi list is comma-separated entries, each "ssid|bssid|rssi":
    - ssid: network name (may be blank for hidden networks)
    - bssid: MAC address of the access point, e.g. "AA:BB:CC:DD:EE:FF"
    - rssi: signal strength in dBm (negative integer, e.g. -67)
  (list may be empty - nothing between its surrounding "_")
- LoRa/Meshtastic support was removed entirely - that field's POSITION in the
  format is still skipped over (so ble/heading/steps stay correctly aligned
  for both old files that have real data there and new ones that don't), but
  its content is always discarded rather than parsed into anything
- the ble list is comma-separated entries, each "<mac>|rssi" - a passive BLE scan,
  no pairing/connecting. Many phones/wearables rotate their BLE address for
  privacy, so only genuinely fixed devices build up repeat sightings over time
  (list may be empty; older logs with no ble field at all are supported too)
- heading is the tracker's fused compass heading in degrees (float), or "NA"
- steps is the tracker's cumulative hardware step counter (int), or "NA"
  (both raw IMU data - no dead-reckoning math is done in the firmware itself;
  older logs with neither field are supported - both come back as None)

The firmware filename encodes the date+time the file was created, e.g.:
    log_20260728_143000.txt  ->  2026-07-28 14:30:00

Each line only has a time-of-day, so we combine it with the date parsed from the
filename to get a full timestamp. This is an approximation: if a single file happened
to span midnight, lines after midnight would be misdated by this method (in practice
files rotate every 30 minutes, so this is very unlikely to matter).
"""

import re
from datetime import datetime
from typing import Optional

FILENAME_RE = re.compile(r"log_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})")


def parse_filename_date(filename: str):
    """Returns a datetime.date parsed from a log_YYYYMMDD_HHMMSS.txt filename, or None."""
    m = FILENAME_RE.search(filename)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None


def parse_line(line: str, file_date: Optional[datetime]):
    """
    Parses one log line. Returns a dict:
        { ts, lat, lon, status, speed_kmh, networks,
          ble_sightings, heading_deg, steps }
    or None if the line could not be parsed (blank/malformed).

    Note: status is either "ok" or "bad_gps" - the latter itself contains an
    underscore, so we can't just split the whole line on "_" positionally. We
    split off time/lat/lon first, then explicitly check for the "ok_" or
    "bad_gps_" prefix before splitting the remainder into speed + the rest.
    """
    line = line.strip()
    if not line:
        return None

    parts = line.split("_", 3)  # time, lat, lon, "status_speed_wifilist..."
    if len(parts) < 4:
        return None

    time_str, lat_str, lon_str, remainder = parts

    if remainder.startswith("bad_gps_"):
        status = "bad_gps"
        remainder = remainder[len("bad_gps_"):]
    elif remainder.startswith("ok_"):
        status = "ok"
        remainder = remainder[len("ok_"):]
    else:
        return None  # unrecognized status - malformed line, skip it

    # remainder = "speed_wifilist_[unused]_blelist_heading_steps" - split off speed first
    rest_parts = remainder.split("_", 1)
    speed_str = rest_parts[0]
    tail = rest_parts[1] if len(rest_parts) > 1 else ""

    # Up to 5 more fields; older log files simply have fewer of them, which
    # this handles gracefully (missing trailing fields default sensibly).
    # tail_parts[1] is the old LoRa field's position - always skipped over
    # (never parsed into anything) so ble/heading/steps stay correctly
    # aligned for both old files that have real data there and new ones that
    # don't - see module docstring.
    tail_parts = tail.split("_", 4)
    wifi_str = tail_parts[0] if len(tail_parts) > 0 else ""
    ble_str = tail_parts[2] if len(tail_parts) > 2 else ""
    heading_str = tail_parts[3] if len(tail_parts) > 3 else "NA"
    steps_str = tail_parts[4] if len(tail_parts) > 4 else "NA"

    try:
        lat = float(lat_str)
        lon = float(lon_str)
    except ValueError:
        lat = None
        lon = None

    speed_kmh = None
    if speed_str not in ("NA", "", "N/A"):
        try:
            speed_kmh = float(speed_str)
        except ValueError:
            speed_kmh = None

    heading_deg = None
    if heading_str not in ("NA", "", "N/A"):
        try:
            heading_deg = float(heading_str)
        except ValueError:
            heading_deg = None

    steps = None
    if steps_str not in ("NA", "", "N/A"):
        try:
            steps = int(steps_str)
        except ValueError:
            steps = None

    ssids = [s.strip() for s in wifi_str.split(",") if s.strip()]
    ble_entries = [s.strip() for s in ble_str.split(",") if s.strip()]

    ts = None
    try:
        h, mi, s = (int(x) for x in time_str.split(":"))
        if file_date is not None:
            ts = file_date.replace(hour=h, minute=mi, second=s).isoformat()
        else:
            ts = time_str  # fall back to just the time-of-day string
    except (ValueError, AttributeError):
        ts = time_str

    return {
        "ts": ts,
        "lat": lat,
        "lon": lon,
        "status": status,
        "speed_kmh": speed_kmh,
        "networks": [parse_wifi_entry(w) for w in ssids],
        "ble_sightings": [parse_ble_entry(b) for b in ble_entries],
        "heading_deg": heading_deg,
        "steps": steps,
    }


def parse_wifi_entry(entry: str):
    """
    Parses one wifi list entry into {ssid, bssid, rssi}.

    Supports both the current format "ssid|bssid|rssi" and, for backward
    compatibility, older log files that only contain a bare SSID (no "|").
    In that case bssid/rssi are None.
    """
    parts = entry.split("|")
    if len(parts) == 3:
        ssid, bssid, rssi_str = parts
        try:
            rssi = int(rssi_str)
        except ValueError:
            rssi = None
        return {"ssid": ssid, "bssid": bssid or None, "rssi": rssi}
    # old format: just a bare SSID string
    return {"ssid": entry, "bssid": None, "rssi": None}


def parse_ble_entry(entry: str):
    """Parses one ble list entry "<mac>|rssi" into {mac, rssi}."""
    parts = entry.split("|")
    mac = parts[0] if parts else entry
    rssi = None
    if len(parts) > 1:
        try:
            rssi = int(parts[1])
        except ValueError:
            rssi = None
    return {"mac": mac, "rssi": rssi}


def parse_file(fileobj, filename: str):
    """
    Parses an uploaded file object (text mode or bytes). Returns a list of parsed
    line dicts (see parse_line).
    """
    file_date = parse_filename_date(filename)

    if hasattr(fileobj, "read"):
        content = fileobj.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        lines = content.splitlines()
    else:
        lines = fileobj

    parsed = []
    for line in lines:
        row = parse_line(line, file_date)
        if row is not None:
            parsed.append(row)
    return parsed
