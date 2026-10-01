"""SQLite storage. One file, created on first start (path from ALTGEO_DB)."""
from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    login         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'client')),
    name          TEXT NOT NULL DEFAULT '',
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
);

-- A device is a data source owned by one client. source_key ties it to the
-- raw points: 'imei:<15 digits>' for the GSM tracker (it sends X-Device-IMEI),
-- 'sd:<random>' for the SD-card tracker (it authenticates with its own
-- sync token, since its firmware sends no identifier of its own).
CREATE TABLE IF NOT EXISTS devices (
    id              INTEGER PRIMARY KEY,
    owner_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('imei', 'sd')),
    source_key      TEXT NOT NULL UNIQUE,
    sync_token_hash TEXT UNIQUE,
    created_at      INTEGER NOT NULL
);

-- Everything the trackers send, including WiFi/BLE. Never returned to clients.
CREATE TABLE IF NOT EXISTS raw_points (
    id         INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    lat        REAL,
    lon        REAL,
    gps_ok     INTEGER NOT NULL,
    wifi       TEXT NOT NULL DEFAULT '[]',
    ble        TEXT NOT NULL DEFAULT '[]',
    heading    REAL,
    steps      INTEGER,
    seq        INTEGER,
    file       TEXT,
    received_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS raw_points_source_ts ON raw_points (source_key, ts);
CREATE UNIQUE INDEX IF NOT EXISTS raw_points_source_seq ON raw_points (source_key, seq) WHERE seq IS NOT NULL;

CREATE TABLE IF NOT EXISTS uploaded_files (
    source_key  TEXT NOT NULL,
    filename    TEXT NOT NULL,
    uploaded_at INTEGER NOT NULL,
    PRIMARY KEY (source_key, filename)
);

-- Learned positions of WiFi access points / BLE beacons, from every point
-- that had a GPS fix (all devices). Weighted running sums, so the mean and
-- spread can be read without rescanning history.
CREATE TABLE IF NOT EXISTS beacons (
    kind    TEXT NOT NULL,
    addr    TEXT NOT NULL,
    w_sum   REAL NOT NULL,
    lat_sum REAL NOT NULL,
    lon_sum REAL NOT NULL,
    lat2_sum REAL NOT NULL,
    lon2_sum REAL NOT NULL,
    n       INTEGER NOT NULL,
    PRIMARY KEY (kind, addr)
);
"""


def db_path() -> str:
    return os.environ.get("ALTGEO_DB", "altgeo.db")


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path(), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def get_conn():
    """FastAPI dependency: one connection per request."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
