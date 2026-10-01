#!/usr/bin/env python3
"""
Makes an immediate, safe backup of the live database - independent of
whether the app/scheduler is running. Meant to be run by hand right before
any risky manual work (e.g. before starting a Claude Code session with
direct server access), in addition to the automatic daily backup already
built into the app itself (see db.backup_database, called from jobs.py's
scheduler and from the "Сделать бэкап сейчас" button on /admin).

Uses sqlite3's own online backup API, not a plain file copy - safe to run
even while the app is actively serving requests and writing to the database.

Usage (run from the same directory as docker-compose.yml):
    python3 backup_now.py --data-dir ./data
"""
import argparse
import os
import sqlite3
from datetime import datetime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="./data")
    parser.add_argument("--keep", type=int, default=14, help="how many backups to keep (oldest deleted first)")
    args = parser.parse_args()

    db_path = os.path.join(args.data_dir, "wifigps.db")
    if not os.path.isfile(db_path):
        print(f"База не найдена по пути {db_path} - запустите из папки с docker-compose.yml, "
              f"или укажите --data-dir.")
        return

    backup_dir = os.path.normpath(os.path.join(args.data_dir, "..", "backups"))
    os.makedirs(backup_dir, exist_ok=True)

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dest_path = os.path.join(backup_dir, f"wifigps_{timestamp}.db")

    source_conn = sqlite3.connect(db_path)
    dest_conn = sqlite3.connect(dest_path)
    try:
        source_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        source_conn.close()

    size_mb = os.path.getsize(dest_path) / (1024 * 1024)
    print(f"Бэкап создан: {dest_path} ({size_mb:.2f} МБ)")

    backups = sorted(
        f for f in os.listdir(backup_dir) if f.startswith("wifigps_") and f.endswith(".db")
    )
    excess = len(backups) - args.keep
    for old in backups[:max(excess, 0)]:
        os.remove(os.path.join(backup_dir, old))
        print(f"Удалён старый бэкап: {old}")

    print(f"Всего хранится бэкапов: {min(len(backups), args.keep)}")


if __name__ == "__main__":
    main()
