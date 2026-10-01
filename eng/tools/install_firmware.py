#!/usr/bin/env python3
"""
Registers a firmware .bin as the current version for a device type, unless
that version is already there. Used by .github/workflows/firmware.yml, which
fetches the latest builds from the firmware repositories.

    python3 install_firmware.py --data-dir ~/altgeo-data/eng \
        --key devtr --version 2026-09-17-abc1234 --file /path/to/x.merged.bin
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seed_firmware import seed_one  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--notes", default="")
    a = p.parse_args()

    conn = sqlite3.connect(os.path.join(a.data_dir, "wifigps.db"), timeout=30)
    try:
        have = conn.execute(
            "SELECT 1 FROM device_firmwares f JOIN device_types t ON t.id = f.device_type_id "
            "WHERE t.key = ? AND f.version = ?", (a.key, a.version)).fetchone()
        if have:
            print(f"  {a.key} {a.version}: already installed")
            return
        if not seed_one(conn, os.path.join(a.data_dir, "firmware"), a.key, a.version, a.file, a.notes):
            sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
