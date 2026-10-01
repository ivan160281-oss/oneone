#!/usr/bin/env python3
"""
Registers a firmware .bin as the current version for a device type, directly
in the database - the same end state as using the "Загрузить новую версию
прошивки" form on /tracker as an engineer, just scriptable for a one-time
bulk import instead of clicking through the form once per file.

Does not need the Flask app's own Python environment - pure sqlite3 +
shutil, so it runs fine directly on the host (against the bind-mounted
./data folder from docker-compose.yml) without going through the container.

Usage (run from the same directory as docker-compose.yml):
    python3 seed_firmware.py --data-dir ./data \\
        --key devtr        --version 1.0.0 --file /path/to/gps_wifi_tracker_ino_merged.bin \\
        --key altgeo-gsm   --version 1.0.0 --file /path/to/altgeo_gsm_tracker_ino_merged.bin

Or just edit FIRMWARE_TO_SEED below and run with no arguments - whichever's
less typing. Safe to re-run: uploading the same (key, version) pair again
just overwrites that version's file and re-marks it current, no duplicates.
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

# Edit this and just run `python3 seed_firmware.py` with no arguments, or
# pass --key/--version/--file triples on the command line instead - either
# works, command-line arguments take priority if given.
FIRMWARE_TO_SEED = [
    {"key": "devtr", "version": "1.0.0", "file": "./gps_wifi_tracker_ino_merged.bin",
     "notes": "Initial production build."},
    {"key": "altgeo-gsm", "version": "1.0.0", "file": "./altgeo_gsm_tracker_ino_merged.bin",
     "notes": "Initial production build."},
]


def seed_one(conn, firmware_dir, key, version, src_path, notes):
    if not os.path.isfile(src_path):
        print(f"  SKIP: file not found: {src_path}")
        return False

    row = conn.execute("SELECT id, name FROM device_types WHERE key = ?", (key,)).fetchone()
    if not row:
        print(f"  SKIP: no device type with key '{key}' exists yet "
              f"(add it on /tracker first, or check the spelling).")
        return False
    device_type_id, type_name = row

    filename = os.path.basename(src_path)
    stored_name = f"{version}__{filename}"
    dest_dir = os.path.join(firmware_dir, str(device_type_id))
    try:
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, stored_name)
        shutil.copyfile(src_path, dest_path)
    except PermissionError:
        print(f"  SKIP: permission denied writing to {dest_dir}")
        print(f"        This folder was created by the container (usually root) and this")
        print(f"        script is running as a different user - fix once with:")
        print(f"          sudo chown -R $(id -u):$(id -g) {os.path.dirname(os.path.dirname(dest_dir)) or '.'}")
        print(f"        then re-run this script. The container can still write there fine")
        print(f"        afterwards - root bypasses normal file ownership checks.")
        return False
    size_mb = os.path.getsize(dest_path) / (1024 * 1024)

    now_iso = datetime.now(timezone.utc).isoformat()
    cur = conn.execute("""
        INSERT INTO device_firmwares (device_type_id, version, filename, notes, is_current, uploaded_at)
        VALUES (?, ?, ?, ?, 0, ?)
    """, (device_type_id, version, stored_name, notes, now_iso))
    firmware_id = cur.lastrowid
    conn.execute("UPDATE device_firmwares SET is_current = 0 WHERE device_type_id = ?", (device_type_id,))
    conn.execute("UPDATE device_firmwares SET is_current = 1 WHERE id = ?", (firmware_id,))
    conn.commit()

    print(f"  OK: {type_name} ({key}) -> version {version}, {size_mb:.1f} MB, now current")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default="./data", help="path to the bind-mounted data folder (default: ./data)")
    parser.add_argument("--key", action="append", default=[])
    parser.add_argument("--version", action="append", default=[])
    parser.add_argument("--file", action="append", default=[])
    parser.add_argument("--notes", action="append", default=[])
    args = parser.parse_args()

    db_path = os.path.join(args.data_dir, "wifigps.db")
    firmware_dir = os.path.join(args.data_dir, "firmware")
    if not os.path.isfile(db_path):
        print(f"Database not found at {db_path} - run this from the same directory "
              f"as docker-compose.yml, or pass --data-dir.")
        sys.exit(1)

    if args.key:
        if not (len(args.key) == len(args.version) == len(args.file)):
            print("Pass the same number of --key, --version, and --file arguments.")
            sys.exit(1)
        entries = [
            {"key": k, "version": v, "file": f, "notes": (args.notes[i] if i < len(args.notes) else "")}
            for i, (k, v, f) in enumerate(zip(args.key, args.version, args.file))
        ]
    else:
        entries = FIRMWARE_TO_SEED

    conn = sqlite3.connect(db_path)
    print(f"Seeding {len(entries)} firmware file(s) into {db_path}\n")
    ok_count = 0
    for e in entries:
        if seed_one(conn, firmware_dir, e["key"], e["version"], e["file"], e.get("notes", "")):
            ok_count += 1
    conn.close()
    print(f"\nDone: {ok_count}/{len(entries)} succeeded. Check the result on /tracker.")


if __name__ == "__main__":
    main()
