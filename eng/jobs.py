"""
Daily processing job.

Runs a full, fully-automatic cleanup + recompute pass over ALL uploaded data:
1. fix_implausible_timestamps - flags points with a GPS cold-start bogus date
2. auto_clean_speed_outliers - flags points from jamming/spoofing-style speed
   excursions, using the one global threshold (db.get_speed_threshold_kmh)
3. detect_cotraveling_devices - detects AND excludes WiFi/BLE devices that
   travel WITH the tracker (car hotspot, earbuds, in-car multimedia) rather
   than being a real roadside reference point
4. recomputes `networks` / `ble_devices` (now correctly excluding everything
   flagged by steps 1-3)
5. detect_network_far_from_track (WiFi, then BLE) - excludes any network/
   device whose averaged position doesn't land near ANY point of the actual
   GPS track - almost always a sign the average itself is polluted (unrelated
   sightings from disjoint sessions/locations, e.g. the same BSSID/MAC seen
   once on two completely unrelated trips), not a useful reference point.
   Each kind has its own distance threshold (WiFi's default is larger - see
   db.get_wifi_track_distance_m)
6. recomputes `networks` / `ble_devices` again, so anything newly excluded
   in step 5 actually disappears from the live aggregates
7. purge_bad_gps_points - PERMANENTLY DELETES every point currently marked
   'bad_gps' (from the firmware's own no-fix rows, or any of steps 1-2 above).
   IRREVERSIBLE. A summary (session, reason, count, rough centroid) is kept
   in deletion_log for the Reports page, but the actual point data is gone.
8. recomputes `estimated_positions` (GPS-independent estimate for every
   remaining - i.e. 'ok' - point, RSSI-weighted where possible)
9. compute_stable_points - recomputes stable_networks/stable_ble_devices
   (networks/devices re-confirmed across multiple distinct sessions, with a
   tight position cluster - see db.compute_stable_points). This used to be
   manually triggered from Algorithm Lab only; it now runs automatically so
   the bright "stable" layer on the main Map stays current without a person
   remembering to click the button
10. records today's daily_stats snapshot (network/device growth, powers the
   header chart)

Runs automatically after every upload, once every 24 hours (see
start_scheduler), and can also be triggered on demand via the
/admin/reprocess endpoint in app.py. This is also the retroactive path for
cleaning up data uploaded before any of steps 1-3/5/7 existed - running it
once applies everything to the whole existing database.
"""

from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler

import os
import threading

from . import db

_scheduler = None


def run_processing():
    """Runs one full cleanup + recompute pass. Returns a summary dict."""
    started_at = datetime.now(timezone.utc).isoformat()
    status = "ok"
    networks_count = 0
    ble_devices_count = 0
    estimated_count = 0
    timestamps_fixed = 0
    speed_outliers_fixed = 0
    cotravel_wifi = 0
    cotravel_ble = 0
    wifi_far_from_track = 0
    ble_far_from_track = 0
    bad_gps_purged = 0
    stable_networks_count = 0
    stable_ble_count = 0

    try:
        with db.get_conn() as conn:
            timestamps_fixed = db.fix_implausible_timestamps(conn)
            speed_outliers_fixed = db.auto_clean_speed_outliers(conn)
            cotravel_wifi, cotravel_ble = db.detect_cotraveling_devices(conn)

            db.recompute_networks(conn)
            db.recompute_ble_devices(conn)

            # Drop any WiFi network / BLE device whose averaged position isn't
            # near the actual driven track, then rebuild the aggregates once
            # more so the live tables reflect it immediately.
            wifi_far_from_track = db.detect_network_far_from_track(conn, "wifi")
            ble_far_from_track = db.detect_network_far_from_track(conn, "ble")
            db.recompute_networks(conn)
            db.recompute_ble_devices(conn)

            # Permanently deletes everything currently bad_gps - must run
            # AFTER the network/device aggregates above (which only ever use
            # 'ok' points anyway) and BEFORE estimated_positions, since a
            # purged point has nothing left to estimate.
            bad_gps_purged = db.purge_bad_gps_points(conn)

            networks_count = conn.execute("SELECT COUNT(*) AS c FROM networks").fetchone()["c"]
            ble_devices_count = conn.execute("SELECT COUNT(*) AS c FROM ble_devices").fetchone()["c"]
            estimated_count = db.recompute_estimated_positions(conn)

            # Powers the bright "stable networks" layer on the main Map -
            # networks/devices re-confirmed across multiple distinct
            # sessions with a tight position cluster (see db.py docstring).
            stable_networks_count, stable_ble_count = db.compute_stable_points(conn)

            db.record_daily_snapshot(conn)
    except Exception as e:  # noqa: BLE001 - want to log any failure, not crash the scheduler
        status = f"error: {e}"

    finished_at = datetime.now(timezone.utc).isoformat()
    cotravel_excluded = cotravel_wifi + cotravel_ble
    db.log_processing_run(started_at, finished_at, networks_count,
                           ble_devices_count, estimated_count, status,
                           timestamps_fixed, speed_outliers_fixed, cotravel_excluded,
                           ble_far_from_track, wifi_far_from_track, bad_gps_purged,
                           stable_networks_count, stable_ble_count)

    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "networks_count": networks_count,
        "ble_devices_count": ble_devices_count,
        "estimated_count": estimated_count,
        "timestamps_fixed": timestamps_fixed,
        "speed_outliers_fixed": speed_outliers_fixed,
        "cotravel_wifi_excluded": cotravel_wifi,
        "cotravel_ble_excluded": cotravel_ble,
        "wifi_far_from_track_excluded": wifi_far_from_track,
        "ble_far_from_track_excluded": ble_far_from_track,
        "bad_gps_purged": bad_gps_purged,
        "stable_networks_count": stable_networks_count,
        "stable_ble_count": stable_ble_count,
        "status": status,
    }


def start_scheduler():
    """
    Starts the background scheduler that runs run_processing() once every 24
    hours. Also runs it once immediately, so a freshly started container (or
    one that's been upgraded) doesn't wait a full day for its first pass -
    this is what applies all the cleanup automation retroactively to
    whatever's already in the database on first deploy.
    """
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    run_processing()
    db.backup_database()  # one on startup too, not just the schedule below

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(run_processing, "interval", hours=24, id="daily_processing")
    _scheduler.add_job(db.backup_database, "interval", hours=24, id="daily_backup")
    _scheduler.start()
    return _scheduler


_lock_file = None


def start_scheduler_in_background():
    """
    Starts the scheduler from a web process without blocking it. The hosting
    runs several web processes; a lock file makes sure only one of them runs
    the jobs. Set ALTGEO_ENG_JOBS=0 to switch the jobs off (tests do).
    """
    global _lock_file
    if os.environ.get("ALTGEO_ENG_JOBS", "1") == "0" or _lock_file is not None:
        return
    try:
        import fcntl
        f = open(os.path.join(db.DB_DIR, "scheduler.lock"), "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, OSError):
        return  # another process already runs the jobs
    _lock_file = f
    threading.Thread(target=start_scheduler, name="eng-scheduler", daemon=True).start()
