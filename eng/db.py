"""
SQLite storage layer for the WiFi/GPS tracker database.

Tables:
- sessions          one row per uploaded .txt file
- points            one row per log line (raw GPS/speed/status observation)
- observations      one row per WiFi network seen at a given point (many-to-many)
- networks          aggregated view: one row per known WiFi network, keyed by BSSID
                     (MAC address) when available - this is what makes GPS-independent
                     WiFi-only positioning meaningful, since BSSID is unique per access
                     point while SSID (name) commonly is not. Falls back to using the
                     SSID as the key for older log lines that have no BSSID.
- estimated_positions  one row per point, with a GPS-independent position estimate
                     computed purely from that point's matching WiFi/BLE
                     sightings against the `networks`/`ble_devices` tables. Rebuilt
                     by the daily job (jobs.py) across ALL points (both 'ok' and
                     'bad_gps') - for 'ok' points this doubles as a validation check
                     (compare estimate vs the real recorded position); for 'bad_gps'
                     points it's the only position estimate available at all.
                     CAVEAT: this is NOT a leave-one-out estimate - an 'ok' point's
                     own observations contributed to the network/device averages it's
                     then matched against, so its estimate error is optimistic (a
                     lower bound on real-world accuracy, not a realistic one).
"""

import sqlite3
import os
import math
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from contextlib import contextmanager

# WIFIGPS_DATA_DIR lets Docker point this at a persistent volume (see
# docker-compose.yml); defaults to this file's own directory for local runs.
DB_DIR = os.environ.get("WIFIGPS_DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "wifigps.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
    file_date TEXT,           -- date parsed from the log_YYYYMMDD_HHMMSS.txt filename
    points_total INTEGER DEFAULT 0,
    points_ok INTEGER DEFAULT 0,
    points_bad_gps INTEGER DEFAULT 0,
    device_id INTEGER REFERENCES devices(id)  -- NULL for uploads from firmware that
                                                -- doesn't send a chip id yet (older
                                                -- builds) - see devices table below
);

-- Monitoring accounts (separate from the single shared engineering password -
-- see app.py's monitoring blueprint). Each user only ever sees devices they own.
-- Self-registered via email (see register_monitor_user) - username is kept
-- (set equal to the email at registration) for backward compatibility with
-- code paths that predate email-based accounts.
CREATE TABLE IF NOT EXISTS monitor_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per known device model/hardware revision - lets engineers add a new
-- device type (and its firmware) without a code change. identifies_by tells
-- the claim flow (and the firmware upload's own header) whether this type is
-- looked up by chip_id (no GSM - e.g. DEVTR) or imei (has a GSM modem).
CREATE TABLE IF NOT EXISTS device_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,          -- short slug, e.g. 'devtr', 'altgeo-gsm'
    name TEXT NOT NULL,                -- display name, e.g. "DEVTR (T-Pager)"
    description TEXT,
    identifies_by TEXT NOT NULL DEFAULT 'chip_id',  -- 'chip_id' or 'imei'
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Firmware binaries available for a device type. Engineers upload these on
-- the Tracker page; is_current marks which one a user's download button
-- fetches by default (older ones stay listed/downloadable for rollback).
CREATE TABLE IF NOT EXISTS device_firmwares (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_type_id INTEGER NOT NULL REFERENCES device_types(id) ON DELETE CASCADE,
    version TEXT NOT NULL,
    filename TEXT NOT NULL,            -- stored filename under FIRMWARE_DIR/<device_type_id>/
    notes TEXT,
    is_current INTEGER NOT NULL DEFAULT 0,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Real-time GSM ingestion watermark (see db.ingest_realtime_points /
-- app.py's /api/device/points). Each GSM-connected device keeps a local,
-- monotonically increasing sequence number per point; this table remembers
-- the highest one this server has ever durably stored for that device, so a
-- retried/duplicate batch (e.g. the device never received the ack over a
-- flaky connection, even though the points DID arrive) is recognized and
-- skipped on re-insert rather than double-counted - while still being
-- reported back as acknowledged, so the device's local queue is freed either
-- way. This is what makes "at least once, no duplicates, no data loss even
-- with GSM dropouts" possible without a heavier message-queue system.
CREATE TABLE IF NOT EXISTS device_sync_state (
    device_id INTEGER PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    last_acked_seq INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);

-- One row per physical tracker. Auto-registered the first time a device
-- uploads/syncs (keyed by its ESP32 chip id, which is unique and burned into
-- the hardware - no per-device configuration needed before shipping it out).
-- `owner_user_id` starts NULL ("unclaimed") until a user claims it by
-- chip_id/imei (see claim_device) or an engineer assigns it on Admin.
-- Exactly one of chip_id/imei is expected to be set, matching the owning
-- device_type's identifies_by - enforced in application code, not a SQL
-- constraint (SQLite's column-level CHECK support makes a clean OR-based
-- constraint awkward, and the extra flexibility costs nothing here).
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chip_id TEXT UNIQUE,            -- e.g. ESP32 efuse MAC, hex string - no-GSM devices
    imei TEXT UNIQUE,               -- GSM module IMEI - devices with cellular
    display_name TEXT,              -- set by owner/admin - "Ivanov's car", etc; NULL = unnamed yet
    device_type_id INTEGER REFERENCES device_types(id),
    device_type TEXT DEFAULT 'unknown',  -- legacy free-text fallback, kept for old rows/back-compat
    owner_user_id INTEGER REFERENCES monitor_users(id),
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT
);

CREATE TABLE IF NOT EXISTS points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ts TEXT,                  -- ISO-ish timestamp, best-effort (file date + HH:MM:SS)
    lat REAL,
    lon REAL,
    status TEXT,              -- 'ok' or 'bad_gps'
    speed_kmh REAL,           -- NULL if not available ("NA" in the log)
    heading_deg REAL,         -- IMU (BHI260AP) fused compass heading; NULL if not available
    steps INTEGER,            -- IMU cumulative hardware step counter; NULL if not available
    bad_gps_reason TEXT,      -- NULL (firmware's own bad_gps, or never touched), or
                               -- 'speed_outlier' / 'implausible_timestamp' / 'manual_cleanup'
    bad_gps_at TEXT           -- when an automatic/manual cleanup flipped this point, for the
                               -- Reports removal log; NULL if never touched by our cleanup
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    ssid TEXT NOT NULL,
    bssid TEXT,               -- MAC address, e.g. "AA:BB:CC:DD:EE:FF"; NULL for old logs
    rssi INTEGER              -- signal strength in dBm; NULL for old logs
);

CREATE TABLE IF NOT EXISTS networks (
    key TEXT PRIMARY KEY,     -- bssid when known, otherwise falls back to ssid
    ssid TEXT NOT NULL,
    bssid TEXT,
    avg_lat REAL,
    avg_lon REAL,
    avg_rssi REAL,
    observation_count INTEGER DEFAULT 0,
    first_seen TEXT,
    last_seen TEXT
);

CREATE TABLE IF NOT EXISTS ble_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    point_id INTEGER NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    mac TEXT NOT NULL,        -- BLE device address (many phones/wearables rotate this
                               -- for privacy - only genuinely fixed devices stay useful)
    rssi INTEGER
);

CREATE TABLE IF NOT EXISTS ble_devices (
    key TEXT PRIMARY KEY,     -- the mac address itself
    mac TEXT NOT NULL,
    avg_lat REAL,
    avg_lon REAL,
    avg_rssi REAL,
    observation_count INTEGER DEFAULT 0,
    first_seen TEXT,
    last_seen TEXT
);

CREATE TABLE IF NOT EXISTS estimated_positions (
    point_id INTEGER PRIMARY KEY REFERENCES points(id) ON DELETE CASCADE,
    est_lat REAL,
    est_lon REAL,
    matched_wifi INTEGER DEFAULT 0,
    matched_ble INTEGER DEFAULT 0,
    error_meters REAL,        -- distance to the real recorded position, NULL for bad_gps points
    computed_at TEXT
);

CREATE TABLE IF NOT EXISTS processing_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    networks_count INTEGER,
    ble_devices_count INTEGER,
    estimated_count INTEGER,
    timestamps_fixed INTEGER,
    speed_outliers_fixed INTEGER,
    cotravel_excluded INTEGER,
    ble_far_from_track_excluded INTEGER,
    wifi_far_from_track_excluded INTEGER,
    stable_networks_count INTEGER,
    stable_ble_count INTEGER,
    bad_gps_purged INTEGER,
    status TEXT               -- 'ok' or 'error: ...'
);

-- One row per calendar day (UTC), recorded after every upload AND every
-- daily/manual processing run - powers the small growth chart in the header.
-- "total_*" fields combine WiFi networks + BLE devices into one figure (the
-- header shows one overall count, not per-type) - GPS points themselves are
-- not part of this count, only known networks/devices.
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,     -- YYYY-MM-DD
    networks_count INTEGER,
    networks_added INTEGER,    -- new WiFi network keys vs. the previous recorded day
    networks_removed INTEGER,  -- WiFi network keys that disappeared vs. the previous recorded day
    total_devices_count INTEGER,  -- WiFi + BLE combined
    total_added INTEGER,          -- combined new keys vs. the previous recorded day
    total_removed INTEGER,        -- combined disappeared keys vs. the previous recorded day
    points_total INTEGER,
    points_ok INTEGER,
    points_bad_gps INTEGER
);

-- Full snapshot of which network keys (WiFi/BLE, prefixed by type
-- so the same raw key text can never collide across types) existed on a
-- given day, used only to diff consecutive days into the added/removed
-- counts above. Superseded snapshots aren't needed for anything else.
CREATE TABLE IF NOT EXISTS network_snapshot (
    date TEXT NOT NULL,
    key TEXT NOT NULL,
    PRIMARY KEY (date, key)
);

-- Simple key-value store for global settings (currently just the
-- speed-outlier cleanup threshold - see auto_clean_speed_outliers).
-- Requests submitted through the public landing page's contact form
-- (company/logistics leads, technology-partner inquiries). Read on Admin.
CREATE TABLE IF NOT EXISTS contact_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    company TEXT,
    email TEXT,
    phone TEXT,
    kind TEXT,          -- 'customer' or 'partner'
    message TEXT,
    submitted_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The project's shared, living roadmap - shown on /roadmap (engineering
-- tool). Both the engineer (Ivan) and Claude use this the same way: each
-- entry is a task, a note, or a suggestion, filed under a section that
-- mirrors the project's own structure (principles, per-mode status, gaps,
-- staged plan, open questions). Nothing here is inferred automatically -
-- every row is something a person (or Claude, on their behalf) explicitly
-- wrote down.
CREATE TABLE IF NOT EXISTS roadmap_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section TEXT NOT NULL,     -- e.g. 'principles', 'mode1', 'mode2', 'mode3',
                                -- 'gaps', 'stage_a', 'stage_b', 'stage_c', 'open_questions'
    kind TEXT NOT NULL DEFAULT 'task',   -- 'task' | 'note' | 'suggestion'
    title TEXT NOT NULL,
    body TEXT,
    status TEXT NOT NULL DEFAULT 'todo', -- 'todo' | 'in_progress' | 'done'
    author TEXT,                -- free text, e.g. 'Иван' or 'Claude'
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Audit trail for purge_bad_gps_points(): since bad_gps points are now
-- permanently DELETED (not just flagged) as the final step of every
-- processing pass, this is the only place their removal is still visible
-- afterwards - the Reports removal log/summary reads from here, not from
-- points.bad_gps_reason (which no longer exists for anything that's been
-- purged).
CREATE TABLE IF NOT EXISTS deletion_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    filename TEXT,
    reason TEXT,        -- 'speed_outlier' / 'implausible_timestamp' / 'manual_cleanup' /
                         -- 'unknown_or_original' (bad_gps from the firmware itself)
    point_count INTEGER,
    avg_lat REAL,       -- centroid of the deleted points' (real, non-placeholder) coordinates,
    avg_lon REAL,       -- for a rough "where" on the Reports map - NULL if none had real coords
    deleted_at TEXT
);

-- "Algorithm Lab" output: networks/nodes that reappear at essentially the
-- same place across multiple DIFFERENT sessions are more likely to be a real
-- static access point/node than a one-off (or moving) sighting. This is a
-- separate, manually-triggered, prioritized database of higher-confidence
-- reference points - see algolab.py.
CREATE TABLE IF NOT EXISTS stable_networks (
    key TEXT PRIMARY KEY,
    ssid TEXT NOT NULL,
    bssid TEXT,
    avg_lat REAL,
    avg_lon REAL,
    distinct_sessions INTEGER, -- how many different uploaded sessions saw this network
    spread_meters REAL,        -- std-dev-like spread of its observed positions
    stability_score REAL,      -- higher = more confidently a fixed, real point
    observation_count INTEGER,
    computed_at TEXT
);

CREATE TABLE IF NOT EXISTS stable_ble_devices (
    key TEXT PRIMARY KEY,
    mac TEXT NOT NULL,
    avg_lat REAL,
    avg_lon REAL,
    distinct_sessions INTEGER,
    spread_meters REAL,
    stability_score REAL,
    observation_count INTEGER,
    computed_at TEXT
);

-- Co-traveling device detection (WiFi/BLE only, per user request): a device
-- that's physically WITH the tracker (car's own hotspot, driver's earbuds/
-- watch, in-car multimedia) gets seen in almost every point of a session,
-- not just a brief passing window like a real roadside network/beacon would.
-- Fully automatic: detection runs after every upload, daily, and on manual
-- reprocess, and any newly detected candidate is excluded (`excluded=1`)
-- immediately - there's no manual review step anymore. The Reports page
-- shows the resulting log; a device can be un-excluded there if detection
-- got it wrong (fully reversible, same as bad_gps).
CREATE TABLE IF NOT EXISTS excluded_devices (
    key TEXT PRIMARY KEY,       -- same key format as networks.key / ble_devices.key
    kind TEXT NOT NULL,         -- 'wifi' or 'ble'
    reason TEXT DEFAULT 'co_traveling', -- 'co_traveling' or 'far_from_track' (both wifi
                                         -- and ble - see detect_network_far_from_track)
    label TEXT NOT NULL,        -- ssid/bssid or mac, for display
    max_presence_ratio REAL,    -- highest (points seen / points in session) across any one session
    flagged_sessions INTEGER,   -- how many distinct sessions exceeded the threshold
    observation_count INTEGER,
    excluded INTEGER DEFAULT 0, -- 0 = not currently excluded, 1 = excluded from aggregates
    user_override INTEGER DEFAULT 0, -- 1 = a person explicitly restored this on the Reports page;
                                       -- re-detection will not auto-re-exclude it while this is set
    detected_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_points_session ON points(session_id);
CREATE INDEX IF NOT EXISTS idx_observations_point ON observations(point_id);
CREATE INDEX IF NOT EXISTS idx_observations_ssid ON observations(ssid);
CREATE INDEX IF NOT EXISTS idx_observations_bssid ON observations(bssid);
CREATE INDEX IF NOT EXISTS idx_ble_obs_point ON ble_observations(point_id);
CREATE INDEX IF NOT EXISTS idx_ble_obs_mac ON ble_observations(mac);
-- Added for the multi-user/multi-device stage: sessions.device_id and
-- devices.owner_user_id are now on the hot path for every user-dashboard
-- track load and every Admin user-stats query, at a scale (50 devices,
-- 100 users) where a full table scan per request stops being free.
CREATE INDEX IF NOT EXISTS idx_sessions_device ON sessions(device_id);
CREATE INDEX IF NOT EXISTS idx_devices_owner ON devices(owner_user_id);
-- Added after profiling detect_network_far_from_track at the 50-device pilot
-- scale: without this, its bounding-box WHERE clause has no index to use at
-- all, so it full-scans every point for every single network/device being
-- checked (tens of thousands of times per processing run - by far the
-- dominant cost once recompute_networks's own correlated-subquery bug was
-- fixed, see that function's docstring).
CREATE INDEX IF NOT EXISTS idx_points_status_lat_lon ON points(status, lat, lon);
"""


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_schema(conn)
        _remove_lora_support(conn)
        _migrate_devices_table(conn)
        _seed_device_types(conn)


def _ensure_column(conn, table, column, coltype):
    """
    Adds `column` to `table` if it doesn't already exist. Needed because
    `CREATE TABLE IF NOT EXISTS` (used above) does nothing to a table that's
    already there from a previous deployment - it does NOT add new columns
    introduced by a later version of this schema. Safe to call every startup.
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def _drop_column_if_exists(conn, table, column):
    """
    Drops `column` from `table` if it currently exists. Requires SQLite
    3.35+ (ALTER TABLE ... DROP COLUMN); if the installed SQLite is older
    than that, the column is just left in place (unused) rather than
    crashing the whole migration - a harmless leftover column beats a
    startup failure.
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column in existing:
        try:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        except sqlite3.OperationalError:
            pass


def _remove_lora_support(conn):
    """
    One-time migration: LoRa/Meshtastic support was removed from this
    project entirely - drops its tables and columns from any existing
    database so it's genuinely gone, not just unused going forward. Safe to
    call on every startup (each statement is a no-op once already applied).
    Old `network_snapshot` rows with a "lora:"-prefixed key are harmless,
    orphaned leftovers - record_daily_snapshot never writes that prefix
    again, and they age out of the added/removed diff naturally as soon as a
    new day's snapshot is recorded, so there's no need to explicitly purge them.
    """
    conn.execute("DROP TABLE IF EXISTS lora_observations")
    conn.execute("DROP TABLE IF EXISTS lora_devices")
    conn.execute("DROP TABLE IF EXISTS stable_lora_devices")
    _drop_column_if_exists(conn, "estimated_positions", "matched_lora")
    _drop_column_if_exists(conn, "processing_runs", "lora_devices_count")
    _drop_column_if_exists(conn, "daily_stats", "lora_devices_count")


def _migrate_devices_table(conn):
    """
    One-time migration: the original `devices` table had chip_id as UNIQUE
    NOT NULL and no imei/device_type_id columns. GSM-capable devices are
    identified by IMEI instead of chip_id (see claim_device), and chip_id
    must therefore become nullable - SQLite has no ALTER COLUMN for dropping
    a NOT NULL constraint, so this rebuilds the table (rename, recreate with
    the new schema, copy rows across, drop the renamed original) rather than
    altering it in place. Safe to call on every startup - a quick column
    check makes it a no-op once already applied.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(devices)").fetchall()}
    if "imei" in cols and "device_type_id" in cols:
        chip_col = next(r for r in conn.execute("PRAGMA table_info(devices)").fetchall()
                         if r["name"] == "chip_id")
        if chip_col["notnull"] == 0:
            return  # already migrated

    conn.execute("ALTER TABLE devices RENAME TO devices_old")
    conn.execute("""
        CREATE TABLE devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chip_id TEXT UNIQUE,
            imei TEXT UNIQUE,
            display_name TEXT,
            device_type_id INTEGER REFERENCES device_types(id),
            device_type TEXT DEFAULT 'unknown',
            owner_user_id INTEGER REFERENCES monitor_users(id),
            first_seen TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen TEXT
        )
    """)
    old_cols = {r["name"] for r in conn.execute("PRAGMA table_info(devices_old)").fetchall()}
    common = ["id", "chip_id", "display_name", "device_type", "owner_user_id",
              "first_seen", "last_seen"]
    common = [c for c in common if c in old_cols]
    col_list = ", ".join(common)
    conn.execute(f"INSERT INTO devices ({col_list}) SELECT {col_list} FROM devices_old")
    conn.execute("DROP TABLE devices_old")


def _seed_device_types(conn):
    """
    Fresh installs (and upgrades from before device_types existed) get the
    two device types already in production use pre-registered, matching the
    free-text values the old devices.device_type column used to hold
    ('engineering' / 'consumer') - existing devices aren't touched here
    (that's a separate, explicit Admin action), this just makes sure the
    types exist to assign going forward.
    """
    existing = conn.execute("SELECT COUNT(*) AS c FROM device_types").fetchone()["c"]
    if existing > 0:
        return
    conn.execute("""
        INSERT INTO device_types (key, name, description, identifies_by)
        VALUES ('devtr', 'DEVTR (T-Pager)', 'Engineering tracker on LILYGO T-Pager - GPS+WiFi+BLE, SD card, WiFi sync. No GSM modem, identified by ESP32 chip id.', 'chip_id')
    """)
    conn.execute("""
        INSERT INTO device_types (key, name, description, identifies_by)
        VALUES ('altgeo-gsm', 'ALTGEO Tracker (T-Call A7670)', 'Consumer tracker on LILYGO T-Call A7670 - GPS+WiFi+BLE+4G/2G, no SD card, real-time sync over GSM. Identified by the modem''s IMEI.', 'imei')
    """)


def _migrate_schema(conn):
    """
    Adds columns that were introduced after some tables already existed in
    older deployments. Each call is a no-op if the column is already there,
    so this is safe (and cheap) to run on every startup.
    """
    _ensure_column(conn, "points", "heading_deg", "REAL")
    _ensure_column(conn, "points", "steps", "INTEGER")
    _ensure_column(conn, "points", "bad_gps_reason", "TEXT")
    _ensure_column(conn, "points", "bad_gps_at", "TEXT")
    _ensure_column(conn, "monitor_users", "email", "TEXT")
    _ensure_column(conn, "processing_runs", "ble_devices_count", "INTEGER")
    _ensure_column(conn, "processing_runs", "timestamps_fixed", "INTEGER")
    _ensure_column(conn, "processing_runs", "speed_outliers_fixed", "INTEGER")
    _ensure_column(conn, "processing_runs", "cotravel_excluded", "INTEGER")
    _ensure_column(conn, "daily_stats", "total_devices_count", "INTEGER")
    _ensure_column(conn, "daily_stats", "total_added", "INTEGER")
    _ensure_column(conn, "daily_stats", "total_removed", "INTEGER")
    _ensure_column(conn, "excluded_devices", "user_override", "INTEGER")
    _ensure_column(conn, "estimated_positions", "matched_ble", "INTEGER")
    _ensure_column(conn, "sessions", "device_id", "INTEGER")
    _ensure_column(conn, "excluded_devices", "reason", "TEXT")
    conn.execute("UPDATE excluded_devices SET reason = 'co_traveling' WHERE reason IS NULL")
    _ensure_column(conn, "processing_runs", "ble_far_from_track_excluded", "INTEGER")
    _ensure_column(conn, "processing_runs", "wifi_far_from_track_excluded", "INTEGER")
    _ensure_column(conn, "processing_runs", "stable_networks_count", "INTEGER")
    _ensure_column(conn, "processing_runs", "stable_ble_count", "INTEGER")
    _ensure_column(conn, "processing_runs", "bad_gps_purged", "INTEGER")


@contextmanager
def get_conn():
    # Several web processes (and the background job) share this file.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Monitoring accounts (separate from the single shared engineering password)
# ---------------------------------------------------------------------------

def create_monitor_user(conn, username, password, display_name=None):
    """Raises sqlite3.IntegrityError if the username is already taken."""
    conn.execute(
        "INSERT INTO monitor_users (username, password_hash, display_name) VALUES (?, ?, ?)",
        (username, generate_password_hash(password), display_name or username),
    )


def register_monitor_user(conn, email, password, display_name=None):
    """
    Self-service registration (see app.py's /monitor/register). username is
    set equal to email so the existing username-based login/lookup code
    keeps working unchanged - email is also stored in its own column for
    clarity and any future email-specific use (password reset, etc).
    Returns None on success, or a user-facing error string if the email is
    already registered (checked here rather than only relying on a DB
    constraint, since the email column on upgraded - pre-existing - databases
    isn't UNIQUE at the SQLite level; see _migrate_schema).
    """
    email = email.strip().lower()
    existing = conn.execute(
        "SELECT id FROM monitor_users WHERE username = ? OR email = ?", (email, email)
    ).fetchone()
    if existing:
        return "Этот email уже зарегистрирован."
    conn.execute(
        "INSERT INTO monitor_users (username, email, password_hash, display_name) VALUES (?, ?, ?, ?)",
        (email, email, generate_password_hash(password), display_name or email),
    )
    return None


def verify_monitor_user(conn, username, password):
    """Returns the user row (dict) if the password is correct, else None."""
    row = conn.execute("SELECT * FROM monitor_users WHERE username = ?", (username,)).fetchone()
    if not row:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    return dict(row)


def get_monitor_user(conn, user_id):
    row = conn.execute("SELECT * FROM monitor_users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_all_monitor_users(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM monitor_users ORDER BY username"
    ).fetchall()]


def get_user_stats(conn):
    """
    For the Admin > Users page: headline numbers plus a per-user breakdown
    (device count, points logged, last activity) - the whole point of this
    is to see at a glance how the pilot's user base is actually using the
    system, not just that they signed up.
    """
    totals = conn.execute("SELECT COUNT(*) AS c FROM monitor_users").fetchone()
    total_users = totals["c"]
    total_devices = conn.execute("SELECT COUNT(*) AS c FROM devices").fetchone()["c"]
    claimed_devices = conn.execute(
        "SELECT COUNT(*) AS c FROM devices WHERE owner_user_id IS NOT NULL"
    ).fetchone()["c"]
    users_with_device = conn.execute(
        "SELECT COUNT(DISTINCT owner_user_id) AS c FROM devices WHERE owner_user_id IS NOT NULL"
    ).fetchone()["c"]

    per_user = conn.execute("""
        SELECT u.id, u.username, u.email, u.display_name, u.created_at,
               COUNT(DISTINCT d.id) AS device_count,
               MAX(d.last_seen) AS last_device_activity,
               COALESCE(SUM(pc.point_count), 0) AS total_points
        FROM monitor_users u
        LEFT JOIN devices d ON d.owner_user_id = u.id
        LEFT JOIN (
            SELECT s.device_id AS device_id, COUNT(*) AS point_count
            FROM points p JOIN sessions s ON s.id = p.session_id
            WHERE s.device_id IS NOT NULL
            GROUP BY s.device_id
        ) pc ON pc.device_id = d.id
        GROUP BY u.id
        ORDER BY u.created_at DESC
    """).fetchall()

    return {
        "total_users": total_users,
        "total_devices": total_devices,
        "claimed_devices": claimed_devices,
        "unclaimed_devices": total_devices - claimed_devices,
        "users_with_device": users_with_device,
        "users_without_device": total_users - users_with_device,
        "per_user": [dict(r) for r in per_user],
    }


def set_monitor_user_password(conn, user_id, new_password):
    conn.execute(
        "UPDATE monitor_users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password), user_id),
    )


def delete_monitor_user(conn, user_id):
    conn.execute("UPDATE devices SET owner_user_id = NULL WHERE owner_user_id = ?", (user_id,))
    conn.execute("DELETE FROM monitor_users WHERE id = ?", (user_id,))


# ---------------------------------------------------------------------------
# Devices - one row per physical tracker, auto-registered by chip id
# ---------------------------------------------------------------------------

def get_or_create_device(conn, chip_id):
    """
    Looks up a device by its (unique, hardware-burned) chip id, auto-
    registering a new row on first contact if it doesn't exist yet - no
    per-device configuration is needed before shipping a tracker out.
    Updates last_seen either way. Returns the device row (dict).
    """
    now_iso = datetime.utcnow().isoformat()
    row = conn.execute("SELECT * FROM devices WHERE chip_id = ?", (chip_id,)).fetchone()
    if row:
        conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now_iso, row["id"]))
        return dict(row)

    cur = conn.execute(
        "INSERT INTO devices (chip_id, first_seen, last_seen) VALUES (?, ?, ?)",
        (chip_id, now_iso, now_iso),
    )
    return {
        "id": cur.lastrowid, "chip_id": chip_id, "display_name": None,
        "device_type": "unknown", "owner_user_id": None,
        "first_seen": now_iso, "last_seen": now_iso,
    }


def get_or_create_device_by_imei(conn, imei):
    """Same idea as get_or_create_device, but for GSM-capable devices that
    identify themselves by IMEI instead of chip_id (see device_types.identifies_by)."""
    now_iso = datetime.utcnow().isoformat()
    row = conn.execute("SELECT * FROM devices WHERE imei = ?", (imei,)).fetchone()
    if row:
        conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now_iso, row["id"]))
        return dict(row)

    cur = conn.execute(
        "INSERT INTO devices (imei, first_seen, last_seen) VALUES (?, ?, ?)",
        (imei, now_iso, now_iso),
    )
    return {
        "id": cur.lastrowid, "chip_id": None, "imei": imei, "display_name": None,
        "device_type_id": None, "device_type": "unknown", "owner_user_id": None,
        "first_seen": now_iso, "last_seen": now_iso,
    }


def _get_or_create_today_session(conn, device_id):
    """
    GSM devices push small batches in near-real-time rather than uploading
    one discrete file at the end of a trip (see ingest_realtime_points) - so
    there's no natural file boundary to key a session on. One session per
    device per UTC calendar day is used instead; the first point of a new day
    creates it, everything else that day reuses it.
    """
    today = datetime.utcnow().strftime("%Y-%m-%d")
    filename = f"realtime_{device_id}_{today}"
    row = conn.execute("SELECT id FROM sessions WHERE filename = ?", (filename,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO sessions (filename, file_date, device_id) VALUES (?, ?, ?)",
        (filename, today, device_id),
    )
    return cur.lastrowid


def ingest_realtime_points(conn, device_id, points):
    """
    Stores a batch of points pushed live by a GSM-connected device (see
    app.py's POST /api/device/points). Each point in `points` is a dict with
    at least {seq, ts, lat, lon, status}, plus optional {wifi: [...], ble:
    [...], heading_deg, steps} - wifi/ble entries are {ssid?, bssid?/mac,
    rssi}.

    Idempotent against retries: points with seq <= this device's
    last_acked_seq are assumed already stored from a previous attempt and are
    skipped on insert, but still reported back as acknowledged (see
    device_sync_state) - the device should purge them from its local queue
    either way, since the server already has them durably.

    Returns the list of seqs that are now safely acknowledged (both
    newly-inserted and previously-stored ones), so the caller can tell the
    device exactly what it's safe to drop.
    """
    row = conn.execute(
        "SELECT last_acked_seq FROM device_sync_state WHERE device_id = ?", (device_id,)
    ).fetchone()
    last_acked = row["last_acked_seq"] if row else 0

    session_id = _get_or_create_today_session(conn, device_id)
    now_iso = datetime.utcnow().isoformat()
    acked_seqs = []
    highest_new = last_acked

    for p in sorted(points, key=lambda x: x.get("seq", 0)):
        seq = p.get("seq")
        if seq is None:
            continue
        if seq <= last_acked:
            acked_seqs.append(seq)  # already stored from a previous attempt - ack again, don't re-insert
            continue

        pcur = conn.execute(
            "INSERT INTO points (session_id, ts, lat, lon, status, speed_kmh, heading_deg, steps) "
            "VALUES (?, ?, ?, ?, ?, 'NA', ?, ?)",
            (session_id, p.get("ts"), p.get("lat"), p.get("lon"), p.get("status", "bad_gps"),
             p.get("heading_deg"), p.get("steps")),
        )
        point_id = pcur.lastrowid
        for net in p.get("wifi", []):
            conn.execute(
                "INSERT INTO observations (point_id, ssid, bssid, rssi) VALUES (?, ?, ?, ?)",
                (point_id, net.get("ssid"), net.get("bssid"), net.get("rssi")),
            )
        for b in p.get("ble", []):
            conn.execute(
                "INSERT INTO ble_observations (point_id, mac, rssi) VALUES (?, ?, ?)",
                (point_id, b.get("mac"), b.get("rssi")),
            )
        acked_seqs.append(seq)
        highest_new = max(highest_new, seq)

    if highest_new > last_acked:
        conn.execute("""
            INSERT INTO device_sync_state (device_id, last_acked_seq, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET last_acked_seq = excluded.last_acked_seq,
                                                   updated_at = excluded.updated_at
        """, (device_id, highest_new, now_iso))

    conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now_iso, device_id))
    return acked_seqs


def claim_device(conn, user_id, identifier, id_type):
    """
    Lets a user attach a device to their account by chip_id or IMEI (see the
    Add Device form on the user dashboard) - id_type is 'chip_id' or 'imei'.
    Works whether the device has ever connected before or not: if no row
    exists yet for that identifier, one is created pre-assigned to this user,
    so the device is already linked the first time it actually uploads data.

    Returns (device_dict, None) on success, or (None, error_message) if the
    identifier is already claimed by a different account.
    """
    identifier = identifier.strip()
    column = "chip_id" if id_type == "chip_id" else "imei"
    row = conn.execute(f"SELECT * FROM devices WHERE {column} = ?", (identifier,)).fetchone()

    if row:
        row = dict(row)
        if row["owner_user_id"] is not None and row["owner_user_id"] != user_id:
            return None, "Это устройство уже привязано к другому аккаунту."
        if row["owner_user_id"] == user_id:
            return row, None  # already theirs - idempotent
        conn.execute("UPDATE devices SET owner_user_id = ? WHERE id = ?", (user_id, row["id"]))
        row["owner_user_id"] = user_id
        return row, None

    now_iso = datetime.utcnow().isoformat()
    cur = conn.execute(
        f"INSERT INTO devices ({column}, owner_user_id, first_seen, last_seen) VALUES (?, ?, ?, ?)",
        (identifier, user_id, now_iso, now_iso),
    )
    return {
        "id": cur.lastrowid, "chip_id": identifier if column == "chip_id" else None,
        "imei": identifier if column == "imei" else None, "display_name": None,
        "device_type_id": None, "device_type": "unknown", "owner_user_id": user_id,
        "first_seen": now_iso, "last_seen": None,
    }, None


def get_all_devices(conn):
    """For the Admin page: every registered device, with its owner's username if assigned."""
    return [dict(r) for r in conn.execute("""
        SELECT d.*, u.username AS owner_username, dt.name AS device_type_name
        FROM devices d
        LEFT JOIN monitor_users u ON u.id = d.owner_user_id
        LEFT JOIN device_types dt ON dt.id = d.device_type_id
        ORDER BY d.last_seen DESC
    """).fetchall()]


def get_devices_for_user(conn, user_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM devices WHERE owner_user_id = ? ORDER BY last_seen DESC", (user_id,)
    ).fetchall()]


def get_device(conn, device_id):
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    return dict(row) if row else None


def update_device(conn, device_id, display_name=None, device_type=None, device_type_id=None,
                   owner_user_id=None, clear_owner=False):
    """Admin-only edits. Pass clear_owner=True to unassign a device (set owner to NULL)."""
    if display_name is not None:
        conn.execute("UPDATE devices SET display_name = ? WHERE id = ?", (display_name, device_id))
    if device_type is not None:
        conn.execute("UPDATE devices SET device_type = ? WHERE id = ?", (device_type, device_id))
    if device_type_id is not None:
        conn.execute("UPDATE devices SET device_type_id = ? WHERE id = ?", (device_type_id, device_id))
    if clear_owner:
        conn.execute("UPDATE devices SET owner_user_id = NULL WHERE id = ?", (device_id,))
    elif owner_user_id is not None:
        conn.execute("UPDATE devices SET owner_user_id = ? WHERE id = ?", (owner_user_id, device_id))


# ---------------------------------------------------------------------------
# Device types & firmware - the "Tracker" page (engineer-managed catalog of
# hardware models and the firmware binaries available for each)
# ---------------------------------------------------------------------------

def get_all_device_types(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM device_types ORDER BY name").fetchall()]


def get_device_type(conn, device_type_id):
    row = conn.execute("SELECT * FROM device_types WHERE id = ?", (device_type_id,)).fetchone()
    return dict(row) if row else None


def create_device_type(conn, key, name, description, identifies_by):
    """Raises sqlite3.IntegrityError if key is already taken."""
    cur = conn.execute(
        "INSERT INTO device_types (key, name, description, identifies_by) VALUES (?, ?, ?, ?)",
        (key, name, description, identifies_by),
    )
    return cur.lastrowid


def get_firmwares_for_type(conn, device_type_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM device_firmwares WHERE device_type_id = ? ORDER BY uploaded_at DESC",
        (device_type_id,)
    ).fetchall()]


def get_current_firmware(conn, device_type_id):
    row = conn.execute(
        "SELECT * FROM device_firmwares WHERE device_type_id = ? AND is_current = 1", (device_type_id,)
    ).fetchone()
    return dict(row) if row else None


def add_firmware(conn, device_type_id, version, filename, notes, set_current=True):
    cur = conn.execute("""
        INSERT INTO device_firmwares (device_type_id, version, filename, notes, is_current)
        VALUES (?, ?, ?, ?, 0)
    """, (device_type_id, version, filename, notes))
    firmware_id = cur.lastrowid
    if set_current:
        set_current_firmware(conn, device_type_id, firmware_id)
    return firmware_id


def set_current_firmware(conn, device_type_id, firmware_id):
    conn.execute("UPDATE device_firmwares SET is_current = 0 WHERE device_type_id = ?", (device_type_id,))
    conn.execute("UPDATE device_firmwares SET is_current = 1 WHERE id = ?", (firmware_id,))


def get_firmware(conn, firmware_id):
    row = conn.execute("SELECT * FROM device_firmwares WHERE id = ?", (firmware_id,)).fetchone()
    return dict(row) if row else None


def get_device_latest_point(conn, device_id):
    """
    Most recent point for this device across all its sessions - prefers a
    real 'ok' fix, but falls back to the GPS-independent estimate if the
    latest point has no real fix. Returns None if the device has no data at all.
    """
    row = conn.execute("""
        SELECT p.id, p.ts, p.lat, p.lon, p.status, p.speed_kmh,
               ep.est_lat, ep.est_lon
        FROM points p
        JOIN sessions s ON s.id = p.session_id
        LEFT JOIN estimated_positions ep ON ep.point_id = p.id
        WHERE s.device_id = ?
        ORDER BY p.id DESC
        LIMIT 1
    """, (device_id,)).fetchone()
    if not row:
        return None
    row = dict(row)
    if row["status"] == "ok":
        return {"lat": row["lat"], "lon": row["lon"], "ts": row["ts"], "source": "gps"}
    if row["est_lat"] is not None:
        return {"lat": row["est_lat"], "lon": row["est_lon"], "ts": row["ts"], "source": "estimate"}
    return None


def get_device_track(conn, device_id, limit=5000):
    """All 'ok' points for this device, across all its sessions, oldest first - for the
    monitoring page's history view."""
    rows = conn.execute("""
        SELECT p.lat, p.lon, p.ts
        FROM points p
        JOIN sessions s ON s.id = p.session_id
        WHERE s.device_id = ? AND p.status = 'ok' AND p.lat IS NOT NULL AND p.lon IS NOT NULL
        ORDER BY p.id
        LIMIT ?
    """, (device_id, limit)).fetchall()
    return [dict(r) for r in rows]


def recompute_networks(conn):
    """
    Recomputes the `networks` aggregate table from scratch based on all
    observations linked to points with a valid ('ok') GPS fix. Networks are
    keyed by BSSID (unique MAC address) when available; observations that have
    no BSSID (older log files) fall back to being keyed by SSID instead, which
    is much less precise (see module docstring).

    Skips any key currently flagged `excluded=1` in `excluded_devices` (see
    detect_cotraveling_devices) - a device that travels WITH the tracker
    (car hotspot, driver's earbuds, in-car multimedia) would otherwise
    contaminate the position average for nearly every point it appears in.

    Picking the most-common ssid text per key (in case it varies) is done via
    a window function over one pass of `observations`, not a per-row
    correlated subquery - at real data volumes (tens of thousands of
    observations) a correlated subquery here re-scans the whole table once
    per output row, which is the difference between this finishing in under
    a second and taking minutes. See recompute_ble_devices, which has no such
    subquery (BLE has no name field to disambiguate) and was always fast -
    that asymmetry was the tell.
    """
    conn.execute("DELETE FROM networks")
    conn.execute("""
        INSERT INTO networks (key, ssid, bssid, avg_lat, avg_lon, avg_rssi,
                               observation_count, first_seen, last_seen)
        SELECT
            agg.key, ssid_pick.ssid, agg.bssid, agg.avg_lat, agg.avg_lon,
            agg.avg_rssi, agg.observation_count, agg.first_seen, agg.last_seen
        FROM (
            SELECT
                COALESCE(o.bssid, 'ssid:' || o.ssid) AS key,
                o.bssid AS bssid,
                AVG(p.lat) AS avg_lat,
                AVG(p.lon) AS avg_lon,
                AVG(o.rssi) AS avg_rssi,
                COUNT(*) AS observation_count,
                MIN(p.ts) AS first_seen,
                MAX(p.ts) AS last_seen
            FROM observations o
            JOIN points p ON p.id = o.point_id
            WHERE p.status = 'ok' AND p.lat IS NOT NULL AND p.lon IS NOT NULL
              AND COALESCE(o.bssid, 'ssid:' || o.ssid) NOT IN (
                  SELECT key FROM excluded_devices WHERE excluded = 1 AND kind = 'wifi'
              )
            GROUP BY COALESCE(o.bssid, 'ssid:' || o.ssid)
        ) agg
        JOIN (
            SELECT key, ssid FROM (
                SELECT
                    COALESCE(o.bssid, 'ssid:' || o.ssid) AS key,
                    o.ssid AS ssid,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(o.bssid, 'ssid:' || o.ssid)
                        ORDER BY COUNT(*) DESC
                    ) AS rn
                FROM observations o
                JOIN points p ON p.id = o.point_id
                WHERE p.status = 'ok' AND p.lat IS NOT NULL AND p.lon IS NOT NULL
                GROUP BY COALESCE(o.bssid, 'ssid:' || o.ssid), o.ssid
            ) ranked WHERE rn = 1
        ) ssid_pick ON ssid_pick.key = agg.key
    """)


def recompute_ble_devices(conn):
    """
    Recomputes the `ble_devices` aggregate table from scratch, based on all
    ble_observations linked to points with a valid ('ok') GPS fix. Keyed by
    the BLE MAC address itself. Note that many phones/wearables rotate their
    BLE address every ~15 min for privacy, so they never build up enough
    repeat sightings to matter here - this table ends up dominated by
    genuinely fixed devices (smart-home gear, beacons, etc), which is exactly
    what's useful for positioning.

    Skips any MAC currently flagged `excluded=1` in `excluded_devices` (see
    detect_cotraveling_devices) - a BLE device riding along with the tracker
    itself (car's own hotspot, driver's earbuds/watch, in-car multimedia)
    would otherwise get an enormous, misleadingly "confident" observation
    count and contaminate the position average for nearly every point.
    """
    conn.execute("DELETE FROM ble_devices")
    conn.execute("""
        INSERT INTO ble_devices (key, mac, avg_lat, avg_lon, avg_rssi,
                                  observation_count, first_seen, last_seen)
        SELECT
            bo.mac AS key,
            bo.mac AS mac,
            AVG(p.lat) AS avg_lat,
            AVG(p.lon) AS avg_lon,
            AVG(bo.rssi) AS avg_rssi,
            COUNT(*) AS observation_count,
            MIN(p.ts) AS first_seen,
            MAX(p.ts) AS last_seen
        FROM ble_observations bo
        JOIN points p ON p.id = bo.point_id
        WHERE p.status = 'ok' AND p.lat IS NOT NULL AND p.lon IS NOT NULL
          AND bo.mac NOT IN (
              SELECT key FROM excluded_devices WHERE excluded = 1 AND kind = 'ble'
          )
        GROUP BY bo.mac
    """)


def _haversine_meters(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# RSSI -> rough distance -> weight (a step up from plain observation-count
# averaging, without needing real trilateration/ranging hardware)
#
# Standard log-distance path loss model: distance = 10^((A - RSSI) / (10*n))
# where A is the (very approximate) RSSI at 1 meter and n is the path-loss
# exponent. These constants vary a lot by hardware/environment - this is
# deliberately a rough estimate, used only to weight closer-looking sightings
# more heavily than distant ones, not as a precise range measurement.
# ---------------------------------------------------------------------------
RSSI_AT_1M = -50.0
PATH_LOSS_EXPONENT = 2.5


def _rssi_to_distance_m(rssi):
    if rssi is None:
        return None
    return 10 ** ((RSSI_AT_1M - rssi) / (10 * PATH_LOSS_EXPONENT))


def _match_weight(rssi, observation_count):
    """
    Combines "how many times has this network's own average been confirmed"
    (observation_count) with "how strong (i.e. probably close) was THIS
    specific sighting" (rssi, if available) into one weight for a position
    estimate. Falls back to observation_count alone when no RSSI is on hand
    (e.g. very old log lines, or the WiFi-only-locate tool where the person
    just pastes SSID names).
    """
    base = max(observation_count or 1, 1)
    dist = _rssi_to_distance_m(rssi)
    if dist is None:
        return base
    return base / (max(dist, 1.0) ** 2)


def compute_showroom_track(conn, limit=20):
    """
    Builds the "Showroom" track (see app.py's /api/track/showroom): for the
    N most recently uploaded sessions, a GPS-independent position estimate
    for each point using ONLY confirmed stable networks/devices
    (stable_networks / stable_ble_devices) as reference points - not the
    full, noisier networks/ble_devices tables that the regular
    estimated_positions table is built from (see
    recompute_estimated_positions). This is deliberately stricter: a
    showroom demo should show off what's achievable with reliable,
    multi-session-confirmed reference points, not be dragged down by
    one-off noisy sightings that happened to get swept up in the same trip.
    A point with no stable-network coverage at all simply has no showroom
    position - left out entirely rather than falling back to a worse estimate.

    Computed on the fly (not persisted) since it's already scoped to a small,
    bounded window (the last `limit` sessions) rather than the whole
    database - same cost profile as recompute_estimated_positions's per-point
    matching, just over far fewer points.
    """
    session_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM sessions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()]
    if not session_ids:
        return []
    placeholders = ",".join("?" * len(session_ids))
    points = conn.execute(
        f"SELECT id, session_id FROM points WHERE session_id IN ({placeholders}) ORDER BY session_id, id",
        session_ids,
    ).fetchall()

    results = []
    for p in points:
        wifi_matches = conn.execute("""
            SELECT sn.avg_lat, sn.avg_lon, sn.observation_count, o.rssi AS obs_rssi
            FROM observations o
            JOIN stable_networks sn ON sn.key = COALESCE(o.bssid, 'ssid:' || o.ssid)
            WHERE o.point_id = ?
        """, (p["id"],)).fetchall()

        ble_matches = conn.execute("""
            SELECT sb.avg_lat, sb.avg_lon, sb.observation_count, bo.rssi AS obs_rssi
            FROM ble_observations bo
            JOIN stable_ble_devices sb ON sb.key = bo.mac
            WHERE bo.point_id = ?
        """, (p["id"],)).fetchall()

        all_matches = list(wifi_matches) + list(ble_matches)
        weights = [_match_weight(m["obs_rssi"], m["observation_count"]) for m in all_matches]
        total_weight = sum(weights)
        if total_weight <= 0:
            continue  # no stable coverage here - honestly leave this point out

        est_lat = sum(m["avg_lat"] * w for m, w in zip(all_matches, weights)) / total_weight
        est_lon = sum(m["avg_lon"] * w for m, w in zip(all_matches, weights)) / total_weight
        results.append({"id": p["id"], "session_id": p["session_id"], "lat": est_lat, "lon": est_lon})

    return results


def recompute_estimated_positions(conn):
    """
    Recomputes the `estimated_positions` table from scratch: for EVERY point
    (both 'ok' and 'bad_gps'), matches its WiFi/BLE observations against the
    current `networks`/`ble_devices` tables and stores a weighted centroid as
    the GPS-independent estimate. Weighting combines each matched network's
    observation_count with a rough distance estimate from that specific
    sighting's RSSI (see _match_weight) - this is a step up from plain
    averaging, though still far short of real trilateration (no ranging
    hardware here, just RSSI). For 'ok' points, also stores the distance
    (meters) between the estimate and the real recorded position, as a
    (optimistic - see module docstring) accuracy check.

    Returns the number of points an estimate was computed for.
    """
    conn.execute("DELETE FROM estimated_positions")

    points = conn.execute("SELECT id, lat, lon, status FROM points").fetchall()
    now_iso = datetime.utcnow().isoformat()
    estimated_count = 0

    for p in points:
        wifi_matches = conn.execute("""
            SELECT n.avg_lat, n.avg_lon, n.observation_count, o.rssi AS obs_rssi
            FROM observations o
            JOIN networks n ON n.key = COALESCE(o.bssid, 'ssid:' || o.ssid)
            WHERE o.point_id = ?
        """, (p["id"],)).fetchall()

        ble_matches = conn.execute("""
            SELECT bd.avg_lat, bd.avg_lon, bd.observation_count, bo.rssi AS obs_rssi
            FROM ble_observations bo
            JOIN ble_devices bd ON bd.key = bo.mac
            WHERE bo.point_id = ?
        """, (p["id"],)).fetchall()

        all_matches = list(wifi_matches) + list(ble_matches)
        weights = [_match_weight(m["obs_rssi"], m["observation_count"]) for m in all_matches]
        total_weight = sum(weights)
        if total_weight <= 0:
            continue

        est_lat = sum(m["avg_lat"] * w for m, w in zip(all_matches, weights)) / total_weight
        est_lon = sum(m["avg_lon"] * w for m, w in zip(all_matches, weights)) / total_weight

        error_m = None
        if p["status"] == "ok" and p["lat"] is not None and p["lon"] is not None:
            error_m = _haversine_meters(p["lat"], p["lon"], est_lat, est_lon)

        conn.execute("""
            INSERT INTO estimated_positions
                (point_id, est_lat, est_lon, matched_wifi, matched_ble,
                 error_meters, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (p["id"], est_lat, est_lon, len(wifi_matches), len(ble_matches),
              error_m, now_iso))
        estimated_count += 1

    return estimated_count


def log_processing_run(started_at, finished_at, networks_count,
                        ble_devices_count, estimated_count, status, timestamps_fixed=0,
                        speed_outliers_fixed=0, cotravel_excluded=0, ble_far_from_track_excluded=0,
                        wifi_far_from_track_excluded=0, bad_gps_purged=0,
                        stable_networks_count=0, stable_ble_count=0):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO processing_runs
                (started_at, finished_at, networks_count,
                 ble_devices_count, estimated_count, status, timestamps_fixed,
                 speed_outliers_fixed, cotravel_excluded, ble_far_from_track_excluded,
                 wifi_far_from_track_excluded, bad_gps_purged,
                 stable_networks_count, stable_ble_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (started_at, finished_at, networks_count,
              ble_devices_count, estimated_count, status, timestamps_fixed,
              speed_outliers_fixed, cotravel_excluded, ble_far_from_track_excluded,
              wifi_far_from_track_excluded, bad_gps_purged,
              stable_networks_count, stable_ble_count))


# ---------------------------------------------------------------------------
# Manual geo-zone cleanup (for GPS jamming / spoofing artifacts)
#
# Electronic warfare in the area sometimes substitutes fake coordinates into
# the GPS fix - there's no reliable algorithm to detect this automatically
# (a spoofed fix looks just as "valid" to the receiver as a real one; only a
# human looking at the map can tell a route suddenly teleported somewhere
# implausible). So instead of guessing, this lets a person draw a bounding
# box around an obviously-wrong cluster of points and act on just those.
#
# Default action is to flip status to 'bad_gps' WITHOUT touching lat/lon -
# this is fully reversible (see restore_points) and automatically removes
# those points from the networks/ble_devices aggregates (which only use
# status='ok' rows), without destroying the underlying WiFi/BLE
# observation data. Hard delete is offered as an explicit, irreversible
# alternative for when you'd rather purge the noise entirely.
# ---------------------------------------------------------------------------

def find_points_in_bbox(conn, min_lat, max_lat, min_lon, max_lon, session_id=None):
    """Returns all 'ok' points within the given box (optionally restricted to one session)."""
    query = """
        SELECT id, session_id, ts, lat, lon FROM points
        WHERE status = 'ok' AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
    """
    params = [min_lat, max_lat, min_lon, max_lon]
    if session_id:
        query += " AND session_id = ?"
        params.append(session_id)
    return conn.execute(query, params).fetchall()


def preview_bbox_networks(conn, point_ids):
    """Returns the distinct WiFi networks and BLE devices seen at the given points."""
    if not point_ids:
        return [], []
    placeholders = ",".join("?" for _ in point_ids)
    wifi = conn.execute(f"""
        SELECT ssid, bssid, COUNT(*) AS hits
        FROM observations WHERE point_id IN ({placeholders})
        GROUP BY ssid, bssid ORDER BY hits DESC
    """, point_ids).fetchall()
    ble = conn.execute(f"""
        SELECT mac, COUNT(*) AS hits
        FROM ble_observations WHERE point_id IN ({placeholders})
        GROUP BY mac ORDER BY hits DESC
    """, point_ids).fetchall()
    return [dict(r) for r in wifi], [dict(r) for r in ble]


def mark_points_bad_gps(conn, point_ids, reason="manual_cleanup"):
    """
    Flips status to 'bad_gps' for the given points, leaving lat/lon untouched
    (reversible). Records `reason` and the current time so the Reports page
    can show a removal log - reason is typically 'manual_cleanup' (Cleanup
    page actions), 'speed_outlier' (auto_clean_speed_outliers), or
    'implausible_timestamp' (fix_implausible_timestamps).
    """
    if not point_ids:
        return
    placeholders = ",".join("?" for _ in point_ids)
    now_iso = datetime.utcnow().isoformat()
    conn.execute(
        f"UPDATE points SET status = 'bad_gps', bad_gps_reason = ?, bad_gps_at = ? "
        f"WHERE id IN ({placeholders})",
        [reason, now_iso] + point_ids,
    )


def restore_points_ok(conn, point_ids):
    """Undo: flips status back to 'ok' for the given points (their lat/lon was never touched)."""
    if not point_ids:
        return
    placeholders = ",".join("?" for _ in point_ids)
    conn.execute(
        f"UPDATE points SET status = 'ok', bad_gps_reason = NULL, bad_gps_at = NULL "
        f"WHERE id IN ({placeholders})",
        point_ids,
    )


def delete_points(conn, point_ids):
    """Permanently deletes the given points (cascades to their observations via FK)."""
    if not point_ids:
        return
    placeholders = ",".join("?" for _ in point_ids)
    conn.execute(f"DELETE FROM points WHERE id IN ({placeholders})", point_ids)


def purge_bad_gps_points(conn):
    """
    Permanently deletes EVERY point currently marked 'bad_gps' - regardless of
    origin (the firmware's own no-fix rows, or any of our own automatic
    flags: speed_outlier, implausible_timestamp, manual_cleanup). This is the
    final step of every processing pass (see jobs.run_processing) - bad_gps
    is now a transient in-between state, not a permanent soft-tombstone.

    IRREVERSIBLE, unlike every other cleanup mechanism in this file - there
    is no undo once this runs. Before deleting, records a per (session,
    reason) summary into deletion_log, since that's the only place this
    removal stays visible afterwards - the Reports removal log/summary reads
    from deletion_log, not from points.bad_gps_reason (which won't exist for
    anything already purged).

    Deleting from `points` cascades (ON DELETE CASCADE) to observations /
    ble_observations / estimated_positions automatically.

    Returns how many points were deleted.
    """
    rows = conn.execute("""
        SELECT session_id, s.filename AS filename,
               COALESCE(bad_gps_reason, 'unknown_or_original') AS reason,
               COUNT(*) AS point_count,
               AVG(CASE WHEN NOT (p.lat = 0 AND p.lon = 0) THEN p.lat END) AS avg_lat,
               AVG(CASE WHEN NOT (p.lat = 0 AND p.lon = 0) THEN p.lon END) AS avg_lon
        FROM points p JOIN sessions s ON s.id = p.session_id
        WHERE p.status = 'bad_gps'
        GROUP BY session_id, COALESCE(bad_gps_reason, 'unknown_or_original')
    """).fetchall()

    if not rows:
        return 0

    now_iso = datetime.utcnow().isoformat()
    total = 0
    affected_sessions = set()
    for r in rows:
        conn.execute("""
            INSERT INTO deletion_log (session_id, filename, reason, point_count, avg_lat, avg_lon, deleted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (r["session_id"], r["filename"], r["reason"], r["point_count"], r["avg_lat"], r["avg_lon"], now_iso))
        total += r["point_count"]
        affected_sessions.add(r["session_id"])

    conn.execute("DELETE FROM points WHERE status = 'bad_gps'")
    refresh_session_counters(conn, list(affected_sessions))

    return total


def refresh_session_counters(conn, session_ids):
    """Recomputes points_total/points_ok/points_bad_gps for the given sessions."""
    for sid in session_ids:
        row = conn.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_count,
                   SUM(CASE WHEN status='bad_gps' THEN 1 ELSE 0 END) AS bad_count
            FROM points WHERE session_id = ?
        """, (sid,)).fetchone()
        conn.execute("""
            UPDATE sessions SET points_total=?, points_ok=?, points_bad_gps=? WHERE id=?
        """, (row["total"] or 0, row["ok_count"] or 0, row["bad_count"] or 0, sid))


# ---------------------------------------------------------------------------
# Implausible-timestamp detection (GPS cold-start artifact)
#
# A GPS module can send a syntactically well-formed but bogus date during a
# cold start (classically something like 1999-11-30, or the NMEA epoch
# default) even while the firmware's TinyGPSPlus isValid() check says
# "valid" - that check only confirms the sentence parsed correctly, not that
# the receiver has an actual time fix yet. Since a real satellite fix and a
# real time fix normally arrive together in the same navigation solution, an
# impossible year is a strong signal the WHOLE point - including its
# "satellites >= 3" coordinates - came from this cold-start window, not a
# genuine fix, even though the firmware wrote it as 'ok'. This intentionally
# stays entirely server-side (no firmware change) so it can be tuned/fixed
# without ever needing to reflash the tracker.
# ---------------------------------------------------------------------------

def fix_implausible_timestamps(conn, min_year=2024):
    """
    Flips any 'ok' point whose timestamp's year is earlier than min_year to
    'bad_gps' - reversible, in the same spirit as every other bad_gps flag
    (lat/lon are left untouched; the point is simply excluded from the
    network/device position averages from then on). Points with only a bare
    HH:MM:SS timestamp (no date - the fallback used when the filename itself
    couldn't be parsed for a date) are skipped, since there's no year to
    check. Refreshes affected sessions' counters. Returns how many points
    were fixed.
    """
    rows = conn.execute("SELECT id, session_id, ts FROM points WHERE status='ok'").fetchall()
    bad_ids = []
    affected_sessions = set()
    for r in rows:
        ts = r["ts"]
        if not ts:
            continue
        try:
            year = datetime.fromisoformat(ts).year
        except (ValueError, TypeError):
            continue  # bare HH:MM:SS or unparseable - nothing to check here
        if year < min_year:
            bad_ids.append(r["id"])
            affected_sessions.add(r["session_id"])

    if bad_ids:
        mark_points_bad_gps(conn, bad_ids, reason="implausible_timestamp")
        refresh_session_counters(conn, list(affected_sessions))

    return len(bad_ids)


# ---------------------------------------------------------------------------
# Suspicious-jump heuristic (a HINT, not an automatic decision)
#
# There's no reliable way to algorithmically prove a coordinate was spoofed by
# jamming equipment - a spoofed fix looks just as internally "valid" as a real
# one. But an implausible speed between two consecutive points (e.g. a track
# that "moves" 300 km/h through central Moscow) is a strong statistical
# indicator that something's wrong there, whether spoofing or a GPS glitch.
# This only surfaces candidates for a human to look at on the Cleanup page -
# it never marks or deletes anything by itself.
# ---------------------------------------------------------------------------

def _parse_ts_seconds(ts):
    """Best-effort: full ISO timestamp -> unix seconds, or bare HH:MM:SS -> seconds-of-day."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        pass
    try:
        t = datetime.strptime(ts, "%H:%M:%S")
        return t.hour * 3600 + t.minute * 60 + t.second
    except (ValueError, TypeError):
        return None


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("""
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (key, str(value)))


# --- Roadmap (see roadmap_items schema) ---------------------------------

ROADMAP_SECTIONS = [
    ("principles", "Принципы проекта (постоянные)"),
    ("mode1", "Режим 1 — Инженерный"),
    ("mode2", "Режим 2 — Клиентский"),
    ("mode3", "Режим 3 — Загрузчик ПО + авто-выгрузка"),
    ("gaps", "Обнаруженные пробелы"),
    ("stage_a", "Этап A — закрыть пробелы"),
    ("stage_b", "Этап B — проверка на реальном железе"),
    ("stage_c", "Этап C — подготовка к росту после 50 машин"),
    ("open_questions", "Открытые вопросы"),
]


def get_roadmap_items(conn):
    """Returns all roadmap items grouped by section, in ROADMAP_SECTIONS order."""
    rows = conn.execute(
        "SELECT * FROM roadmap_items ORDER BY position, id"
    ).fetchall()
    by_section = {key: [] for key, _ in ROADMAP_SECTIONS}
    for r in rows:
        by_section.setdefault(r["section"], []).append(dict(r))
    return [{"key": key, "label": label, "entries": by_section.get(key, [])}
            for key, label in ROADMAP_SECTIONS]


def add_roadmap_item(conn, section, title, body, kind="task", status="todo", author=None):
    now_iso = datetime.utcnow().isoformat()
    max_pos = conn.execute(
        "SELECT COALESCE(MAX(position), 0) AS m FROM roadmap_items WHERE section = ?", (section,)
    ).fetchone()["m"]
    cur = conn.execute("""
        INSERT INTO roadmap_items (section, kind, title, body, status, author, position, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (section, kind, title, body, status, author, max_pos + 1, now_iso))
    return cur.lastrowid


def update_roadmap_item_status(conn, item_id, status):
    conn.execute(
        "UPDATE roadmap_items SET status = ?, updated_at = ? WHERE id = ?",
        (status, datetime.utcnow().isoformat(), item_id),
    )


def delete_roadmap_item(conn, item_id):
    conn.execute("DELETE FROM roadmap_items WHERE id = ?", (item_id,))


# --- Database backups -----------------------------------------------------

BACKUP_DIR = os.path.join(os.path.dirname(DB_PATH), "..", "backups")
BACKUP_DIR = os.path.normpath(BACKUP_DIR)
BACKUP_KEEP_COUNT = 14  # ~2 weeks of daily backups


def backup_database():
    """
    Makes a safe, point-in-time copy of the live database into BACKUP_DIR
    (a sibling of the bind-mounted ./data directory, so it survives
    container rebuilds and lives on the host disk independently of the
    live data path). Uses sqlite3's own online backup API, NOT a plain file
    copy - a plain copy taken while the app is actively writing can capture
    a half-written page and produce a corrupt backup; the backup API is
    safe to run against a live, in-use database.

    Also deletes old backups beyond BACKUP_KEEP_COUNT, oldest first, so
    this can run on a schedule indefinitely without silently filling the
    disk. Returns the path of the new backup file, or None if the source
    database doesn't exist yet.
    """
    if not os.path.isfile(DB_PATH):
        return None

    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dest_path = os.path.join(BACKUP_DIR, f"wifigps_{timestamp}.db")

    source_conn = sqlite3.connect(DB_PATH)
    dest_conn = sqlite3.connect(dest_path)
    try:
        source_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        source_conn.close()

    _prune_old_backups()
    return dest_path


def _prune_old_backups():
    if not os.path.isdir(BACKUP_DIR):
        return
    backups = sorted(
        (f for f in os.listdir(BACKUP_DIR) if f.startswith("wifigps_") and f.endswith(".db")),
    )
    excess = len(backups) - BACKUP_KEEP_COUNT
    for old in backups[:max(excess, 0)]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
        except OSError:
            pass  # another process may have already removed it - not worth failing over


def list_backups():
    """Returns backups newest-first, each with filename, size in bytes, and mtime."""
    if not os.path.isdir(BACKUP_DIR):
        return []
    rows = []
    for f in os.listdir(BACKUP_DIR):
        if f.startswith("wifigps_") and f.endswith(".db"):
            full = os.path.join(BACKUP_DIR, f)
            rows.append({
                "filename": f,
                "size_bytes": os.path.getsize(full),
                "modified_at": datetime.fromtimestamp(os.path.getmtime(full)).isoformat(),
            })
    return sorted(rows, key=lambda r: r["filename"], reverse=True)


def get_speed_threshold_kmh(conn):
    """
    The one global speed-outlier threshold, used consistently by the
    suspicious-jumps hint, manual Cleanup actions, AND the automatic
    speed-outlier cleanup (auto_clean_speed_outliers) - "applies to the
    whole algorithm", not a per-request value. Defaults to 150 km/h.
    """
    val = get_setting(conn, "speed_threshold_kmh", "150")
    try:
        return float(val)
    except (ValueError, TypeError):
        return 150.0


def find_suspicious_jumps(conn, session_id=None, speed_threshold_kmh=150):
    """
    Returns a list of consecutive 'ok'-point pairs (within the same session)
    whose implied speed exceeds speed_threshold_kmh. Each entry:
    {session_id, from_point_id, to_point_id, from_lat, from_lon, to_lat, to_lon,
     distance_m, dt_seconds, speed_kmh}
    """
    query = "SELECT id, session_id, ts, lat, lon FROM points WHERE status='ok'"
    params = []
    if session_id:
        query += " AND session_id = ?"
        params.append(session_id)
    query += " ORDER BY session_id, id"
    rows = conn.execute(query, params).fetchall()

    jumps = []
    prev = None
    for r in rows:
        if prev is not None and prev["session_id"] == r["session_id"]:
            t1 = _parse_ts_seconds(prev["ts"])
            t2 = _parse_ts_seconds(r["ts"])
            dt = (t2 - t1) if (t1 is not None and t2 is not None) else None
            if dt and dt > 0 and prev["lat"] is not None and r["lat"] is not None:
                dist = _haversine_meters(prev["lat"], prev["lon"], r["lat"], r["lon"])
                speed_kmh = (dist / dt) * 3.6
                if speed_kmh >= speed_threshold_kmh:
                    jumps.append({
                        "session_id": r["session_id"],
                        "from_point_id": prev["id"], "to_point_id": r["id"],
                        "from_lat": prev["lat"], "from_lon": prev["lon"],
                        "to_lat": r["lat"], "to_lon": r["lon"],
                        "distance_m": dist, "dt_seconds": dt, "speed_kmh": speed_kmh,
                    })
        prev = r
    return jumps


def find_excursion(conn, session_id, point_id_a, point_id_b, speed_threshold_kmh=150):
    """
    Given one clicked "suspicious" segment (point_id_a -> point_id_b, adjacent
    in the same session's 'ok' sequence), returns the FULL run of consecutive
    points that are implausibly far from whichever of the two looks like the
    real anchor - not just the two clicked endpoints.

    Why this is needed: GPS jamming/spoofing usually corrupts a whole run of
    consecutive fixes for as long as it's active, not a single isolated point.
    Removing just one bad point still leaves the next one - the track then
    reconnects to it, producing a new (often near-identical-looking) long
    segment, and the person has to repeat the click over and over. Expanding
    from an anchor instead grabs the whole corrupted run in one go.

    Method: pick whichever of point_id_a/point_id_b has a plausible (short)
    segment to its OTHER neighbor as the "anchor" (i.e. it already reconnects
    smoothly to the rest of the track on its far side, so it's probably real).
    Then walk outward from the anchor, point by point, computing the speed
    implied by the straight-line distance from the anchor itself (not from
    the previous point in the walk) - as long as that stays implausible, the
    point is part of the same excursion; the first point where it drops back
    under the threshold is treated as the real return point and walking
    stops. This is still just a heuristic (see module docstring) - it can
    occasionally misjudge which side is the anchor, or under/over-shoot the
    true boundary, so the Cleanup page always shows a preview before
    anything is changed.

    Returns {"point_ids": [...], "anchor_point_id": ..., "excursion_count": N}
    or None if point_id_a/point_id_b aren't adjacent 'ok' points in that session.
    """
    rows = conn.execute(
        "SELECT id, ts, lat, lon FROM points WHERE status='ok' AND session_id=? ORDER BY id",
        (session_id,),
    ).fetchall()
    points = [dict(r) for r in rows]
    for p in points:
        p["ts_sec"] = _parse_ts_seconds(p["ts"])

    idx = {p["id"]: i for i, p in enumerate(points)}
    if point_id_a not in idx or point_id_b not in idx:
        return None
    i, j = idx[point_id_a], idx[point_id_b]
    if abs(i - j) != 1:
        return None
    i, j = min(i, j), max(i, j)  # i is the earlier point, j = i+1

    def segment_speed_kmh(a, b):
        if a["ts_sec"] is None or b["ts_sec"] is None:
            return None
        dt = abs(b["ts_sec"] - a["ts_sec"])
        if dt <= 0:
            return None
        dist = _haversine_meters(a["lat"], a["lon"], b["lat"], b["lon"])
        return (dist / dt) * 3.6

    # Decide which side looks like the real anchor: the one whose OTHER
    # (non-clicked) neighboring segment is still plausible.
    left_plausible = True
    if i - 1 >= 0:
        s = segment_speed_kmh(points[i - 1], points[i])
        left_plausible = (s is not None) and (s < speed_threshold_kmh)
    right_plausible = True
    if j + 1 < len(points):
        s = segment_speed_kmh(points[j], points[j + 1])
        right_plausible = (s is not None) and (s < speed_threshold_kmh)

    if left_plausible and not right_plausible:
        anchor_idx, direction = i, 1
    elif right_plausible and not left_plausible:
        anchor_idx, direction = j, -1
    else:
        # ambiguous (both or neither look plausible) - default to treating the
        # left/earlier point as the anchor and expanding forward
        anchor_idx, direction = i, 1

    anchor = points[anchor_idx]
    excursion_ids = []
    k = anchor_idx + direction
    while 0 <= k < len(points):
        speed = segment_speed_kmh(anchor, points[k])
        if speed is not None and speed >= speed_threshold_kmh:
            excursion_ids.append(points[k]["id"])
            k += direction
        else:
            break

    return {
        "point_ids": excursion_ids,
        "anchor_point_id": anchor["id"],
        "excursion_count": len(excursion_ids),
    }


def auto_clean_speed_outliers(conn, speed_threshold_kmh=None):
    """
    Fully automatic version of the interactive Cleanup segment-expansion
    tool: for every session, finds every suspicious jump (see
    find_suspicious_jumps), expands each one to its full corrupted run (see
    find_excursion), and marks the whole run 'bad_gps' with
    reason='speed_outlier' - no per-segment clicking needed. Runs after
    every upload, daily, and on manual reprocess (see jobs.py), always using
    the one global threshold (db.get_speed_threshold_kmh) unless a specific
    value is passed in. Returns how many points were fixed.
    """
    if speed_threshold_kmh is None:
        speed_threshold_kmh = get_speed_threshold_kmh(conn)

    session_ids = [r["id"] for r in conn.execute("SELECT id FROM sessions").fetchall()]
    total_fixed = 0

    for sid in session_ids:
        jumps = find_suspicious_jumps(conn, session_id=sid, speed_threshold_kmh=speed_threshold_kmh)
        already_handled = set()
        affected = []
        for j in jumps:
            if j["from_point_id"] in already_handled or j["to_point_id"] in already_handled:
                continue  # already swept up by a previous excursion in this same pass
            result = find_excursion(conn, sid, j["from_point_id"], j["to_point_id"], speed_threshold_kmh)
            if result and result["point_ids"]:
                affected.extend(result["point_ids"])
                already_handled.update(result["point_ids"])
        if affected:
            mark_points_bad_gps(conn, affected, reason="speed_outlier")
            refresh_session_counters(conn, [sid])
            total_fixed += len(affected)

    return total_fixed


# ---------------------------------------------------------------------------
# Daily network growth stats (for the small header chart)
# ---------------------------------------------------------------------------

def record_daily_snapshot(conn):
    """
    Records today's (UTC) full set of network/device keys (WiFi networks +
    BLE devices, each prefixed by type so the same raw key text can never
    collide across types), diffs it against the most recent previously-
    recorded day to get added/removed counts, and upserts both the snapshot
    and the daily_stats row for today. Safe to call more than once on the
    same day (e.g. every upload, or a manual reprocess) - just overwrites
    today's rows with the current truth. GPS points aren't part of this
    count at all - only known networks/devices.
    """
    today = datetime.utcnow().strftime("%Y-%m-%d")

    wifi_keys_raw = {r["key"] for r in conn.execute("SELECT key FROM networks").fetchall()}
    ble_keys_raw = {r["key"] for r in conn.execute("SELECT key FROM ble_devices").fetchall()}
    current_keys = ({"wifi:" + k for k in wifi_keys_raw} |
                     {"ble:" + k for k in ble_keys_raw})

    prev_date_row = conn.execute(
        "SELECT date FROM network_snapshot WHERE date < ? ORDER BY date DESC LIMIT 1", (today,)
    ).fetchone()
    if prev_date_row:
        prev_keys = {r["key"] for r in conn.execute(
            "SELECT key FROM network_snapshot WHERE date = ?", (prev_date_row["date"],)
        ).fetchall()}
    else:
        prev_keys = set()

    prev_wifi_keys = {k[5:] for k in prev_keys if k.startswith("wifi:")}
    total_added = len(current_keys - prev_keys)
    total_removed = len(prev_keys - current_keys)
    networks_added = len(wifi_keys_raw - prev_wifi_keys)
    networks_removed = len(prev_wifi_keys - wifi_keys_raw)

    conn.execute("DELETE FROM network_snapshot WHERE date = ?", (today,))
    conn.executemany(
        "INSERT INTO network_snapshot (date, key) VALUES (?, ?)",
        [(today, k) for k in current_keys],
    )

    networks_count = len(wifi_keys_raw)
    ble_devices_count = len(ble_keys_raw)
    total_devices_count = len(current_keys)
    totals = conn.execute("""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_count,
               SUM(CASE WHEN status='bad_gps' THEN 1 ELSE 0 END) AS bad_count
        FROM points
    """).fetchone()

    conn.execute("""
        INSERT INTO daily_stats (date, networks_count, networks_added,
                                  networks_removed, total_devices_count, total_added, total_removed,
                                  points_total, points_ok, points_bad_gps)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            networks_count=excluded.networks_count,
            networks_added=excluded.networks_added,
            networks_removed=excluded.networks_removed,
            total_devices_count=excluded.total_devices_count,
            total_added=excluded.total_added,
            total_removed=excluded.total_removed,
            points_total=excluded.points_total,
            points_ok=excluded.points_ok,
            points_bad_gps=excluded.points_bad_gps
    """, (today, networks_count, networks_added, networks_removed,
          total_devices_count, total_added, total_removed,
          totals["total"] or 0, totals["ok_count"] or 0, totals["bad_count"] or 0))


def get_daily_stats(conn, days=14):
    rows = conn.execute(
        "SELECT * FROM daily_stats ORDER BY date DESC LIMIT ?", (days,)
    ).fetchall()
    return list(reversed([dict(r) for r in rows]))


def get_public_stats(conn):
    """
    Safe, aggregate-only numbers for the public landing page - total known
    networks/devices and today's added/removed counts. No location data,
    no per-device or per-session detail - just the headline figures.
    """
    rows = get_daily_stats(conn, days=1)
    if not rows:
        return {"total": 0, "added_today": 0, "removed_today": 0}
    today = rows[-1]
    return {
        "total": today.get("total_devices_count") or 0,
        "added_today": today.get("total_added") or 0,
        "removed_today": today.get("total_removed") or 0,
    }


def create_contact_request(conn, name, company, email, phone, kind, message):
    conn.execute("""
        INSERT INTO contact_requests (name, company, email, phone, kind, message)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (name, company, email, phone, kind, message))


def get_contact_requests(conn, limit=100):
    rows = conn.execute(
        "SELECT * FROM contact_requests ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Removal log / summary - "what was removed and why" for the Reports page
# ---------------------------------------------------------------------------

REMOVAL_REASON_LABELS = {
    "speed_outlier": "Speed outlier (jamming/spoofing)",
    "implausible_timestamp": "Implausible GPS timestamp (cold start)",
    "manual_cleanup": "Manual Cleanup action",
}


def get_removal_summary(conn):
    """
    Aggregate counts for the Reports page: total points ever permanently
    deleted (from deletion_log - see purge_bad_gps_points; the points
    themselves no longer exist to query directly), broken down by reason,
    plus how many WiFi/BLE devices are currently excluded, broken down by
    reason (co-traveling vs far-from-track, for either kind).
    """
    point_rows = conn.execute("""
        SELECT reason, SUM(point_count) AS c FROM deletion_log GROUP BY reason
    """).fetchall()
    by_reason = {r["reason"]: r["c"] for r in point_rows}

    device_rows = conn.execute("""
        SELECT kind, reason, COUNT(*) AS c FROM excluded_devices WHERE excluded = 1
        GROUP BY kind, reason
    """).fetchall()
    devices_excluded = {}
    devices_by_kind = {"wifi": 0, "ble": 0}
    for r in device_rows:
        devices_excluded[f"{r['kind']}_{r['reason']}"] = r["c"]
        devices_by_kind[r["kind"]] = devices_by_kind.get(r["kind"], 0) + r["c"]

    return {
        "points_by_reason": by_reason,
        "devices_excluded": devices_by_kind,
        "devices_excluded_detail": devices_excluded,
        "total_bad_gps_points": sum(by_reason.values()),
        "total_devices_excluded": sum(devices_by_kind.values()),
    }


def get_removal_log(conn, limit=100):
    """
    Chronological log for the Reports page: point deletion events (from
    deletion_log - see purge_bad_gps_points; the points themselves are
    permanently gone by the time this is read) and device exclusion events.
    """
    point_events = conn.execute(f"""
        SELECT session_id, filename, reason, deleted_at AS at_minute, point_count
        FROM deletion_log
        ORDER BY deleted_at DESC
        LIMIT {int(limit)}
    """).fetchall()

    device_events = conn.execute(f"""
        SELECT key, kind, reason, label, max_presence_ratio, flagged_sessions, observation_count, detected_at
        FROM excluded_devices WHERE excluded = 1
        ORDER BY detected_at DESC
        LIMIT {int(limit)}
    """).fetchall()

    return {
        "point_events": [dict(r) for r in point_events],
        "device_events": [dict(r) for r in device_events],
    }


def get_removal_points_for_map(conn, limit=3000):
    """
    Approximate "where cleanup happened" markers for the Reports map, colored
    by reason on the frontend. Since bad_gps points are now permanently
    deleted (purge_bad_gps_points), individual point coordinates no longer
    exist - this shows one marker per (session, reason) cleanup event, at
    the centroid of whatever real coordinates that batch had.
    """
    rows = conn.execute(f"""
        SELECT session_id, filename, reason, avg_lat AS lat, avg_lon AS lon,
               point_count, deleted_at AS at
        FROM deletion_log
        WHERE avg_lat IS NOT NULL AND avg_lon IS NOT NULL
        ORDER BY deleted_at DESC
        LIMIT {int(limit)}
    """).fetchall()
    return [dict(r) for r in rows]



# ---------------------------------------------------------------------------
# Algorithm Lab: find STATIC/stable reference points
#
# The regular `networks`/`ble_devices` tables average every sighting of a
# network together, regardless of whether they came from one drive-by or
# twenty. A network seen only once is treated the same as one confirmed
# repeatedly, in the same spot, across many separate trips - but the latter
# is much more likely to be a real, fixed access point, and its averaged
# position is much more trustworthy. This manually-triggered pass builds a
# separate, prioritized table ranking networks/nodes by how many DISTINCT
# sessions re-confirmed them (more independent re-sightings = more
# confidence) and how tightly clustered those sightings are (a real fixed
# point should look the same every time; a wide spread suggests either a
# moving device, a common SSID shared by several different physical routers,
# or spoofed/noisy data).
# ---------------------------------------------------------------------------

def compute_stable_points(conn, min_sessions=2):
    """
    Recomputes stable_networks/stable_ble_devices from scratch. Only
    considers observations at points with a valid ('ok') GPS fix, exactly
    like the regular aggregate tables. Two sanity checks are applied that
    the regular aggregate tables already get but this one used to skip
    entirely (both confirmed as real gaps from map screenshots showing
    stray "spike" lines fanning out to a single bad point):

    1. Already-excluded co-traveling devices (car hotspot, earbuds, in-car
       multimedia - see detect_cotraveling_devices) are skipped here too. A
       co-traveler rides along on MANY trips by definition, so without this
       it would often satisfy "seen in >=2 distinct sessions" and read as
       "stable" despite never being a fixed point at all.
    2. Same far-from-track sanity check as detect_network_far_from_track:
       "confirmed in 2 sessions" only says those 2 sessions agreed with each
       other - it says nothing about whether that agreed-upon position is
       anywhere near THIS route. A tight, stable-looking average can still
       be geographically detached from every real GPS point in the
       database, which is exactly what produces the long stray lines
       fanning out to one point on the map.

    Returns (networks_count, ble_count).
    """
    now_iso = datetime.utcnow().isoformat()

    conn.execute("DELETE FROM stable_networks")
    wifi_rows = conn.execute("""
        SELECT COALESCE(o.bssid, 'ssid:' || o.ssid) AS key,
               o.ssid AS ssid, o.bssid AS bssid,
               p.session_id AS session_id, p.lat AS lat, p.lon AS lon
        FROM observations o
        JOIN points p ON p.id = o.point_id
        WHERE p.status = 'ok' AND p.lat IS NOT NULL AND p.lon IS NOT NULL
          AND COALESCE(o.bssid, 'ssid:' || o.ssid) NOT IN (
              SELECT key FROM excluded_devices WHERE excluded = 1 AND kind = 'wifi'
          )
    """).fetchall()
    _compute_stability_group(conn, wifi_rows, "stable_networks",
                              extra_cols=("ssid", "bssid"), min_sessions=min_sessions,
                              now_iso=now_iso, max_distance_m=get_wifi_track_distance_m(conn))

    conn.execute("DELETE FROM stable_ble_devices")
    ble_rows = conn.execute("""
        SELECT bo.mac AS key, bo.mac AS mac,
               p.session_id AS session_id, p.lat AS lat, p.lon AS lon
        FROM ble_observations bo
        JOIN points p ON p.id = bo.point_id
        WHERE p.status = 'ok' AND p.lat IS NOT NULL AND p.lon IS NOT NULL
          AND bo.mac NOT IN (
              SELECT key FROM excluded_devices WHERE excluded = 1 AND kind = 'ble'
          )
    """).fetchall()
    _compute_stability_group(conn, ble_rows, "stable_ble_devices",
                              extra_cols=("mac",), min_sessions=min_sessions,
                              now_iso=now_iso, max_distance_m=get_ble_track_distance_m(conn))

    networks_count = conn.execute("SELECT COUNT(*) AS c FROM stable_networks").fetchone()["c"]
    ble_count = conn.execute("SELECT COUNT(*) AS c FROM stable_ble_devices").fetchone()["c"]
    return networks_count, ble_count


def _compute_stability_group(conn, rows, table_name, extra_cols, min_sessions, now_iso, max_distance_m):
    """
    Shared grouping/scoring logic for both WiFi networks and BLE devices -
    groups the given rows by `key`, computes distinct session count, average
    position, and position spread, then inserts qualifying rows
    (distinct_sessions >= min_sessions AND near the real track - see
    _near_any_track_point) into table_name.
    """
    groups = {}
    for r in rows:
        g = groups.setdefault(r["key"], {"lats": [], "lons": [], "sessions": set(), "extra": None})
        g["lats"].append(r["lat"])
        g["lons"].append(r["lon"])
        g["sessions"].add(r["session_id"])
        if g["extra"] is None:
            g["extra"] = tuple(r[c] for c in extra_cols)

    for key, g in groups.items():
        distinct_sessions = len(g["sessions"])
        if distinct_sessions < min_sessions:
            continue
        avg_lat = sum(g["lats"]) / len(g["lats"])
        avg_lon = sum(g["lons"]) / len(g["lons"])
        if not _near_any_track_point(conn, avg_lat, avg_lon, max_distance_m):
            continue  # stable-looking average, but nowhere near any real point - reject
        # spread: average distance of each observation from the centroid (meters)
        spread = sum(_haversine_meters(avg_lat, avg_lon, lat, lon)
                     for lat, lon in zip(g["lats"], g["lons"])) / len(g["lats"])
        # score: more re-confirmations is good, a tight spread is good.
        # +1 in the denominator avoids div-by-zero for a perfectly tight cluster.
        stability_score = distinct_sessions / (1.0 + spread / 50.0)

        if table_name == "stable_networks":
            ssid, bssid = g["extra"]
            conn.execute("""
                INSERT INTO stable_networks
                    (key, ssid, bssid, avg_lat, avg_lon, distinct_sessions, spread_meters,
                     stability_score, observation_count, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (key, ssid, bssid, avg_lat, avg_lon, distinct_sessions, spread,
                  stability_score, len(g["lats"]), now_iso))
        else:  # stable_ble_devices
            (mac,) = g["extra"]
            conn.execute("""
                INSERT INTO stable_ble_devices
                    (key, mac, avg_lat, avg_lon, distinct_sessions, spread_meters,
                     stability_score, observation_count, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (key, mac, avg_lat, avg_lon, distinct_sessions, spread,
                  stability_score, len(g["lats"]), now_iso))


def get_stable_networks(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM stable_networks ORDER BY stability_score DESC"
    ).fetchall()]


def get_stable_ble_devices(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM stable_ble_devices ORDER BY stability_score DESC"
    ).fetchall()]


# ---------------------------------------------------------------------------
# Co-traveling device detection (WiFi/BLE) - see excluded_devices docstring
# ---------------------------------------------------------------------------

def detect_cotraveling_devices(conn, presence_ratio_threshold=None):
    """
    Detects AND excludes WiFi networks / BLE devices that look like they're
    traveling WITH the tracker itself (car's own hotspot, driver's earbuds/
    watch, in-car multimedia) rather than being a real roadside reference
    point: a real fixed point is only seen for a brief window as you pass
    it, while a co-traveling device gets seen across a large fraction of a
    whole session's points. For each key, computes (points where seen /
    points in that session) per session; if that ratio exceeds
    presence_ratio_threshold in ANY session, the key is excluded immediately
    (`excluded=1`) - fully automatic, no manual review step. If a person has
    since restored a device via the Reports page (`user_override=1`),
    re-detection leaves it alone instead of re-excluding it.

    presence_ratio_threshold defaults to the stored setting (0.6 if unset).
    Returns (wifi_candidates_count, ble_candidates_count).
    """
    if presence_ratio_threshold is None:
        val = get_setting(conn, "cotravel_presence_threshold", "0.6")
        try:
            presence_ratio_threshold = float(val)
        except (ValueError, TypeError):
            presence_ratio_threshold = 0.6

    now_iso = datetime.utcnow().isoformat()

    session_totals = {r["session_id"]: r["total"] for r in conn.execute(
        "SELECT session_id, COUNT(*) AS total FROM points WHERE status='ok' GROUP BY session_id"
    ).fetchall()}

    def _detect(rows, kind):
        per_key = {}
        for r in rows:
            key = r["key"]
            g = per_key.setdefault(key, {"label": r["label"], "sessions": {}, "obs_count": 0})
            g["obs_count"] += 1
            g["sessions"][r["session_id"]] = g["sessions"].get(r["session_id"], 0) + 1

        count = 0
        for key, g in per_key.items():
            max_ratio = 0.0
            flagged_sessions = 0
            for sid, seen_count in g["sessions"].items():
                total = session_totals.get(sid, 0)
                if total <= 0:
                    continue
                ratio = seen_count / total
                max_ratio = max(max_ratio, ratio)
                if ratio >= presence_ratio_threshold:
                    flagged_sessions += 1
            if flagged_sessions == 0:
                continue
            count += 1
            conn.execute("""
                INSERT INTO excluded_devices
                    (key, kind, label, max_presence_ratio, flagged_sessions,
                     observation_count, excluded, user_override, detected_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?)
                ON CONFLICT(key) DO UPDATE SET
                    label=excluded.label,
                    max_presence_ratio=excluded.max_presence_ratio,
                    flagged_sessions=excluded.flagged_sessions,
                    observation_count=excluded.observation_count,
                    detected_at=excluded.detected_at,
                    excluded = CASE WHEN user_override = 1 THEN excluded ELSE 1 END
            """, (key, kind, g["label"], max_ratio, flagged_sessions, g["obs_count"], now_iso))
        return count

    # Drop stale candidates that no longer qualify at all AND were never
    # touched by a person (rows with user_override=1 are kept regardless,
    # so a manual decision is never silently erased).
    conn.execute("DELETE FROM excluded_devices WHERE user_override = 0")

    wifi_rows = conn.execute("""
        SELECT COALESCE(o.bssid, 'ssid:' || o.ssid) AS key,
               COALESCE(o.bssid, o.ssid) AS label,
               p.session_id AS session_id
        FROM observations o JOIN points p ON p.id = o.point_id
        WHERE p.status = 'ok'
    """).fetchall()
    wifi_count = _detect(wifi_rows, "wifi")

    ble_rows = conn.execute("""
        SELECT bo.mac AS key, bo.mac AS label, p.session_id AS session_id
        FROM ble_observations bo JOIN points p ON p.id = bo.point_id
        WHERE p.status = 'ok'
    """).fetchall()
    ble_count = _detect(ble_rows, "ble")

    return wifi_count, ble_count


def get_excluded_devices(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM excluded_devices ORDER BY max_presence_ratio DESC"
    ).fetchall()]


def set_device_excluded(conn, key, excluded):
    """
    Any explicit call here (from the Reports page) counts as a human
    override - future auto-detection runs (detect_cotraveling_devices)
    will not silently undo it.
    """
    conn.execute(
        "UPDATE excluded_devices SET excluded = ?, user_override = 1 WHERE key = ?",
        (1 if excluded else 0, key),
    )


def get_cotravel_exclusion_keys(conn):
    """Returns {'wifi': set(keys), 'ble': set(keys)} currently marked excluded=1."""
    rows = conn.execute("SELECT key, kind FROM excluded_devices WHERE excluded = 1").fetchall()
    result = {"wifi": set(), "ble": set()}
    for r in rows:
        result[r["kind"]].add(r["key"])
    return result


def get_all_candidate_keys(conn):
    """Returns {'wifi': set(keys), 'ble': set(keys)} for ALL detected candidates (regardless
    of the excluded flag) - used by the Reports page to show the full effect of the new
    algorithm, not just whatever's already been toggled on."""
    rows = conn.execute("SELECT key, kind FROM excluded_devices").fetchall()
    result = {"wifi": set(), "ble": set()}
    for r in rows:
        result[r["kind"]].add(r["key"])
    return result


def get_user_overridden_keys(conn, kind):
    """Keys a person has explicitly restored/re-excluded on the Reports page for this
    kind - automatic detection passes must never silently override these."""
    rows = conn.execute(
        "SELECT key FROM excluded_devices WHERE kind = ? AND user_override = 1", (kind,)
    ).fetchall()
    return {r["key"] for r in rows}


def get_ble_track_distance_m(conn):
    """The one global 'how far from the actual driven track is too far' threshold for
    BLE devices (see detect_network_far_from_track). Defaults to 200 meters."""
    val = get_setting(conn, "ble_track_distance_m", "200")
    try:
        return float(val)
    except (ValueError, TypeError):
        return 200.0


def get_wifi_track_distance_m(conn):
    """Same idea as get_ble_track_distance_m, but for WiFi networks. Defaults to
    500 meters (larger than BLE's default) - legitimate fixed WiFi access points,
    especially higher-power ones near open roads, are routinely detectable from
    further away than a typical BLE beacon, so a tighter threshold would risk
    excluding real roadside networks, not just polluted averages."""
    val = get_setting(conn, "wifi_track_distance_m", "500")
    try:
        return float(val)
    except (ValueError, TypeError):
        return 500.0


def _near_any_track_point(conn, lat, lon, max_distance_m):
    """
    True if (lat, lon) falls within max_distance_m of at least one 'ok' GPS
    point anywhere in the database. Shared by detect_network_far_from_track
    (regular networks/devices) and _compute_stability_group (stable points) -
    both need the exact same "is this actually near anywhere we've really
    been" sanity check, just applied to a different position source. A
    stable-but-geographically-detached network is just as much a bad
    reference point as an unstable one - "confirmed in 2 sessions" says
    nothing about whether those 2 sessions were anywhere near THIS route
    (see the map screenshots that prompted this - a single bad average acts
    as a "magnet" pulling several real points' lines toward it in a fan
    pattern, whether that average came from the regular or stable table).
    """
    margin = (max_distance_m / 111000.0) * 2.0
    candidates = conn.execute("""
        SELECT lat, lon FROM points
        WHERE status = 'ok' AND lat IS NOT NULL AND lon IS NOT NULL
          AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
        LIMIT 2000
    """, (lat - margin, lat + margin, lon - margin, lon + margin)).fetchall()
    return any(_haversine_meters(lat, lon, p["lat"], p["lon"]) <= max_distance_m for p in candidates)


def detect_network_far_from_track(conn, kind, max_distance_m=None):
    """
    Both WiFi and BLE are susceptible to the same failure mode: a network/device's
    averaged position can end up nowhere near anywhere the tracker has actually
    been, if it was polluted by unrelated sightings from disjoint sessions/
    locations (e.g. the same BSSID/MAC seen once on one trip and once on a
    completely unrelated trip far away - the average lands somewhere between,
    on neither). If a network/device's averaged position doesn't fall near ANY
    point of the actual GPS track (across all sessions, not just one), it's
    excluded entirely - this is almost always a sign the average itself is bad,
    not a useful roadside reference point.

    kind: 'wifi' or 'ble'. Each has its own threshold setting (WiFi's is larger
    by default - see get_wifi_track_distance_m - since legitimate WiFi access
    points are routinely detectable from further away than a BLE beacon).

    Must be run AFTER recompute_networks/recompute_ble_devices (it reads the
    just-rebuilt aggregate table) and the caller must recompute it again
    afterwards so newly excluded networks/devices actually disappear from the
    live aggregate.

    Respects user_override (a person's explicit restore/re-exclude choice on Reports
    is never silently undone). Returns how many networks/devices were newly excluded.
    """
    if kind == "wifi":
        table, id_col = "networks", "bssid"
        if max_distance_m is None:
            max_distance_m = get_wifi_track_distance_m(conn)
    elif kind == "ble":
        table, id_col = "ble_devices", "mac"
        if max_distance_m is None:
            max_distance_m = get_ble_track_distance_m(conn)
    else:
        raise ValueError(f"unknown kind {kind!r}")

    pinned = get_user_overridden_keys(conn, kind)
    rows = conn.execute(
        f"SELECT key, {id_col} AS label, avg_lat, avg_lon, observation_count FROM {table}"
    ).fetchall()
    now_iso = datetime.utcnow().isoformat()
    excluded_count = 0

    for d in rows:
        if d["key"] in pinned:
            continue  # a person already decided to keep this one - leave it alone

        if _near_any_track_point(conn, d["avg_lat"], d["avg_lon"], max_distance_m):
            continue

        excluded_count += 1
        conn.execute("""
            INSERT INTO excluded_devices
                (key, kind, reason, label, max_presence_ratio, flagged_sessions,
                 observation_count, excluded, user_override, detected_at)
            VALUES (?, ?, 'far_from_track', ?, NULL, NULL, ?, 1, 0, ?)
            ON CONFLICT(key) DO UPDATE SET
                reason = 'far_from_track',
                label = excluded.label,
                observation_count = excluded.observation_count,
                detected_at = excluded.detected_at,
                excluded = CASE WHEN user_override = 1 THEN excluded ELSE 1 END
        """, (d["key"], kind, d["label"], d["observation_count"], now_iso))

    return excluded_count


# ---------------------------------------------------------------------------
# Reports: old-algorithm vs new-algorithm (co-traveling exclusion) comparison
# ---------------------------------------------------------------------------

def compute_adhoc_aggregate(conn, kind, exclude_keys=None):
    """
    Builds a {key: {avg_lat, avg_lon, observation_count}} aggregate straight
    from the raw observation tables (not the persisted networks/ble_devices
    tables), optionally skipping a given set of keys. Used by the Reports
    page to compare "old" (no exclusion) vs "new" (co-traveling devices
    excluded) without needing to touch the live aggregate tables.
    """
    exclude_keys = exclude_keys or set()
    if kind == "wifi":
        rows = conn.execute("""
            SELECT COALESCE(o.bssid, 'ssid:' || o.ssid) AS key, p.lat AS lat, p.lon AS lon
            FROM observations o JOIN points p ON p.id = o.point_id
            WHERE p.status = 'ok' AND p.lat IS NOT NULL AND p.lon IS NOT NULL
        """).fetchall()
    elif kind == "ble":
        rows = conn.execute("""
            SELECT bo.mac AS key, p.lat AS lat, p.lon AS lon
            FROM ble_observations bo JOIN points p ON p.id = bo.point_id
            WHERE p.status = 'ok' AND p.lat IS NOT NULL AND p.lon IS NOT NULL
        """).fetchall()
    else:
        raise ValueError(f"unknown kind {kind!r}")

    groups = {}
    for r in rows:
        if r["key"] in exclude_keys:
            continue
        g = groups.setdefault(r["key"], {"lats": [], "lons": []})
        g["lats"].append(r["lat"])
        g["lons"].append(r["lon"])

    return {
        k: {"avg_lat": sum(g["lats"]) / len(g["lats"]),
            "avg_lon": sum(g["lons"]) / len(g["lons"]),
            "observation_count": len(g["lats"])}
        for k, g in groups.items()
    }


def compute_session_report(conn, session_id):
    """
    Builds the data for the Reports page: for one session, computes GPS
    ground truth alongside WiFi/BLE estimates under both the OLD algorithm
    (no filtering) and the NEW algorithm (co-traveling candidates excluded -
    see detect_cotraveling_devices). Returns per-point tracks for a map plus
    an average-error (meters) summary for each variant, computed only where
    BOTH a real GPS fix and that variant's estimate exist for the same point.
    """
    candidates = get_all_candidate_keys(conn)
    wifi_new_agg = compute_adhoc_aggregate(conn, "wifi", exclude_keys=candidates["wifi"])
    wifi_old_agg = compute_adhoc_aggregate(conn, "wifi", exclude_keys=set())
    ble_new_agg = compute_adhoc_aggregate(conn, "ble", exclude_keys=candidates["ble"])
    ble_old_agg = compute_adhoc_aggregate(conn, "ble", exclude_keys=set())

    def centroid(pairs):
        if not pairs:
            return None
        weights = [_match_weight(rssi, row["observation_count"]) for row, rssi in pairs]
        total_w = sum(weights)
        if total_w <= 0:
            return None
        lat = sum(row["avg_lat"] * w for (row, _), w in zip(pairs, weights)) / total_w
        lon = sum(row["avg_lon"] * w for (row, _), w in zip(pairs, weights)) / total_w
        return {"lat": lat, "lon": lon}

    def match(entries, agg, key_fn):
        pairs = []
        for e in entries:
            row = agg.get(key_fn(e))
            if row:
                pairs.append((row, e["rssi"]))
        return centroid(pairs)

    points = conn.execute(
        "SELECT id, ts, lat, lon, status FROM points WHERE session_id=? ORDER BY id",
        (session_id,)
    ).fetchall()

    result_points = []
    err = {"wifi_old": [], "wifi_new": [], "ble_old": [], "ble_new": []}

    for p in points:
        wifi_obs = conn.execute(
            "SELECT bssid, ssid, rssi FROM observations WHERE point_id=?", (p["id"],)
        ).fetchall()
        ble_obs = conn.execute(
            "SELECT mac, rssi FROM ble_observations WHERE point_id=?", (p["id"],)
        ).fetchall()

        wifi_key_fn = lambda e: e["bssid"] if e["bssid"] else ("ssid:" + e["ssid"])
        ble_key_fn = lambda e: e["mac"]

        estimates = {
            "wifi_old": match(wifi_obs, wifi_old_agg, wifi_key_fn),
            "wifi_new": match(wifi_obs, wifi_new_agg, wifi_key_fn),
            "ble_old": match(ble_obs, ble_old_agg, ble_key_fn),
            "ble_new": match(ble_obs, ble_new_agg, ble_key_fn),
        }

        real = {"lat": p["lat"], "lon": p["lon"]} if p["status"] == "ok" else None
        if real:
            for name, est in estimates.items():
                if est:
                    err[name].append(_haversine_meters(real["lat"], real["lon"], est["lat"], est["lon"]))

        row_out = {"ts": p["ts"], "status": p["status"], "gps": real}
        row_out.update(estimates)
        result_points.append(row_out)

    def avg(lst):
        return (sum(lst) / len(lst)) if lst else None

    summary = {name: {"avg_error_m": avg(vals), "matched_points": len(vals)} for name, vals in err.items()}

    return {"points": result_points, "summary": summary}


# ---------------------------------------------------------------------------
# Point Lab support: match an arbitrary (not-yet-stored) point's WiFi/BLE
# observations against either the regular or the "stable" database.
# ---------------------------------------------------------------------------

def match_position(conn, wifi_entries, ble_entries=None, use_stable=False):
    """
    wifi_entries: list of {ssid, bssid, rssi} seen at one point (from parser.py)
    ble_entries: list of {mac, rssi} seen at one point (optional)

    Returns {wifi_estimate, ble_estimate, combined_estimate}, each either
    None (no match) or {lat, lon, matched_count}. Uses RSSI-based distance
    weighting when available (see _match_weight), not just a plain
    observation-count average.
    """
    ble_entries = ble_entries or []
    wifi_table = "stable_networks" if use_stable else "networks"
    ble_table = "stable_ble_devices" if use_stable else "ble_devices"

    wifi_matches = []  # (row, rssi) pairs
    for w in wifi_entries:
        key = w["bssid"] if w.get("bssid") else ("ssid:" + w["ssid"])
        row = conn.execute(
            f"SELECT avg_lat, avg_lon, observation_count FROM {wifi_table} WHERE key = ?", (key,)
        ).fetchone()
        if row:
            wifi_matches.append((row, w.get("rssi")))

    ble_matches = []
    for b in ble_entries:
        row = conn.execute(
            f"SELECT avg_lat, avg_lon, observation_count FROM {ble_table} WHERE key = ?", (b["mac"],)
        ).fetchone()
        if row:
            ble_matches.append((row, b.get("rssi")))

    def centroid(pairs):
        if not pairs:
            return None
        weights = [_match_weight(rssi, row["observation_count"]) for row, rssi in pairs]
        total_w = sum(weights)
        if total_w <= 0:
            return None
        lat = sum(row["avg_lat"] * w for (row, _), w in zip(pairs, weights)) / total_w
        lon = sum(row["avg_lon"] * w for (row, _), w in zip(pairs, weights)) / total_w
        return {"lat": lat, "lon": lon, "matched_count": len(pairs)}

    return {
        "wifi_estimate": centroid(wifi_matches),
        "ble_estimate": centroid(ble_matches),
        "combined_estimate": centroid(wifi_matches + ble_matches),
    }
