"""
WiFi/GPS tracker server.

- Upload page: accepts .txt log files from the T-LoRa Pager tracker, parses them,
  and stores every point + WiFi observation in a SQLite database
  (cumulative - each new upload adds to the existing data, nothing is overwritten).
- Map page: shows the accumulated track (red line, like on the device) and the
  known WiFi networks on a Leaflet map centered on Moscow region.
- Track detail page (/track/<id>): full point-by-point telemetry for one session -
  which WiFi signals were seen at which point, plus the GPS-independent
  position estimate for that point (see db.py / jobs.py).
- Admin page (/admin): processing run history + a button to trigger the daily
  recompute job on demand.
- /api/locate: given currently visible WiFi identifiers, returns a rough
  position estimate (centroid of matching known networks/devices) - the "GPS-
  independent" positioning mode. Accuracy depends entirely on how much matching
  data has been uploaded (see db.py docstring for details/caveats).

- Login page: single shared password protects the whole app (this is a
  personal-scale tool, not a multi-user service) - the tracker's automatic
  WiFi sync can't do a browser login, so it authenticates the same password
  via a request header instead (see /api/upload, /api/sync/check).

A background job (jobs.py) recomputes the network/device aggregates and the
per-point position estimates once every 24 hours across ALL uploaded data (also
runs once immediately on startup). This is what "learns" enough over time to
reconstruct a track for stretches with no GPS fix at all.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://<server>:5000/ (or run under Docker - see README.md).
"""

from flask import Flask, request, render_template, jsonify, redirect, url_for, flash, session, send_from_directory
from werkzeug.utils import secure_filename
import hashlib
import os
import sqlite3

from . import db
from . import parser
from . import jobs

app = Flask(__name__)
# Flask's own cookie session only carries flash messages here; who may enter
# is decided by the ALTGEO admin session (see require_admin).
app.secret_key = os.environ.get("WIFIGPS_SECRET_KEY") or hashlib.sha256(
    ("altgeo-eng:" + os.environ.get("ADMIN_PASSWORD", "")).encode()).hexdigest()
app.config["SESSION_COOKIE_NAME"] = "altgeo_eng_flash"

UPLOAD_DIR = os.path.join(db.DB_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Firmware for consumer devices (see Monitoring > Download) lives in the same
# persistent data directory as the database, so it survives container rebuilds.
FIRMWARE_DIR = os.path.join(db.DB_DIR, "firmware")
os.makedirs(FIRMWARE_DIR, exist_ok=True)

MAX_UPLOAD_MB = 40  # headroom for full merged-flash firmware images (e.g. T-Pager's is ~16MB)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

@app.before_request
def require_admin():
    """
    The engineering part is reachable only by an ALTGEO administrator: the
    same login as the admin page of the main site (cookie altgeo_session).
    Clients and anonymous visitors are sent back to the main login page.
    Devices never come here - they post to the main site's /api/*, which
    feeds this database too (see eng/feed.py).
    """
    if request.endpoint == "static":
        return None
    from app import auth as core_auth, db as core_db
    conn = core_db.connect()
    try:
        user = core_auth.session_user(conn, request.cookies.get(core_auth.SESSION_COOKIE))
    finally:
        conn.close()
    if user and user["role"] == "admin":
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return redirect("/")


@app.context_processor
def inject_security_warning():
    return {"using_default_password": False}


db.init_db()
jobs.start_scheduler_in_background()


@app.route("/")
def index():
    return redirect(url_for("map_view"))


@app.route("/map")
def map_view():
    """The internal engineering Map tool - a separate module from the public
    site, reached by logging in at /login (see require_login below)."""
    return render_template("index.html")


def _process_uploaded_files(conn, files, device_chip_id=None):
    """
    Shared logic for both the HTML upload page and the firmware's automatic
    sync client: saves, parses, and inserts each file's rows/observations,
    and updates session counters. If device_chip_id is given (sent by
    updated firmware as the X-Device-Chip-ID header), auto-registers/looks up
    that device and links every new session to it - this is what powers the
    Monitoring page and Admin's device list. Returns a list of per-file result
    dicts. Does NOT run cleanup/recompute itself - the caller runs the full
    jobs.run_processing() pipeline afterwards, once this transaction has
    committed (see /upload and /api/upload).
    """
    device_id = None
    if device_chip_id:
        device_id = db.get_or_create_device(conn, device_chip_id)["id"]

    results = []
    for f in files:
        if not f.filename:
            continue
        if not f.filename.lower().endswith(".txt"):
            results.append({"filename": f.filename, "error": "Not a .txt file, skipped."})
            continue

        saved_path = os.path.join(UPLOAD_DIR, f.filename)
        f.save(saved_path)

        with open(saved_path, "r", encoding="utf-8", errors="replace") as fh:
            rows = parser.parse_file(fh, f.filename)

        file_date = parser.parse_filename_date(f.filename)

        cur = conn.execute(
            "INSERT INTO sessions (filename, file_date, device_id) VALUES (?, ?, ?)",
            (f.filename, file_date.date().isoformat() if file_date else None, device_id),
        )
        session_id = cur.lastrowid

        points_ok = 0
        points_bad = 0
        for row in rows:
            pcur = conn.execute(
                "INSERT INTO points (session_id, ts, lat, lon, status, speed_kmh, heading_deg, steps) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, row["ts"], row["lat"], row["lon"], row["status"], row["speed_kmh"],
                 row.get("heading_deg"), row.get("steps")),
            )
            point_id = pcur.lastrowid
            if row["status"] == "ok":
                points_ok += 1
            else:
                points_bad += 1
            for net in row["networks"]:
                conn.execute(
                    "INSERT INTO observations (point_id, ssid, bssid, rssi) VALUES (?, ?, ?, ?)",
                    (point_id, net["ssid"], net["bssid"], net["rssi"]),
                )
            for ble in row.get("ble_sightings", []):
                conn.execute(
                    "INSERT INTO ble_observations (point_id, mac, rssi) VALUES (?, ?, ?)",
                    (point_id, ble["mac"], ble["rssi"]),
                )

        conn.execute(
            "UPDATE sessions SET points_total=?, points_ok=?, points_bad_gps=? WHERE id=?",
            (len(rows), points_ok, points_bad, session_id),
        )

        results.append({
            "filename": f.filename,
            "session_id": session_id,
            "points_total": len(rows),
            "points_ok": points_ok,
            "points_bad_gps": points_bad,
        })
    return results


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "GET":
        return render_template("upload.html")

    files = request.files.getlist("logfiles")
    if not files or all(f.filename == "" for f in files):
        flash("No files selected.")
        return redirect(url_for("upload"))

    with db.get_conn() as conn:
        results = _process_uploaded_files(conn, files)

    # Runs after the insert transaction has committed, so the new points are
    # visible to it: implausible-timestamp fix, speed-outlier cleanup,
    # co-traveling device detection, and the network/device aggregate rebuild.
    summary = jobs.run_processing()

    with db.get_conn() as conn:
        for r in results:
            row = conn.execute(
                "SELECT points_ok, points_bad_gps FROM sessions WHERE id=?", (r["session_id"],)
            ).fetchone()
            r["points_ok"] = row["points_ok"]
            r["points_bad_gps"] = row["points_bad_gps"]

    return render_template("upload_result.html", results=results, summary=summary)


@app.route("/api/daily_stats")
def api_daily_stats():
    days = request.args.get("days", default=14, type=int)
    with db.get_conn() as conn:
        rows = db.get_daily_stats(conn, days)
    return jsonify(rows)


@app.route("/lab")
def point_lab():
    return render_template("lab.html")


@app.route("/api/lab/analyze", methods=["POST"])
def api_lab_analyze():
    """
    Point Lab: parses an uploaded .txt file and, for every line, computes up
    to four position views WITHOUT storing anything in the database:
    - gps: the file's own recorded lat/lon (only for 'ok' lines)
    - wifi_only: position estimate from just that line's WiFi sightings,
      matched against the current network database
    - ble_only: same, but just BLE sightings
    - combined: both together
    This is a scratch/preview tool - nothing here is saved, so you can safely
    try a file with missing/jammed GPS and see what a reconstruction would
    look like before deciding whether it's worth uploading "for real" via the
    Upload page.
    """
    f = request.files.get("logfile")
    if not f or not f.filename:
        return jsonify({"error": "No file provided (field name: logfile)"}), 400

    use_stable = request.form.get("use_stable") == "true"
    rows = parser.parse_file(f, f.filename)

    result_points = []
    with db.get_conn() as conn:
        for row in rows:
            match = db.match_position(conn, row["networks"],
                                       row.get("ble_sightings", []), use_stable)
            result_points.append({
                "ts": row["ts"],
                "status": row["status"],
                "gps": {"lat": row["lat"], "lon": row["lon"]} if row["status"] == "ok" else None,
                "wifi_only": match["wifi_estimate"],
                "ble_only": match["ble_estimate"],
                "combined": match["combined_estimate"],
                "wifi_seen": len(row["networks"]),
                "ble_seen": len(row.get("ble_sightings", [])),
                "heading_deg": row.get("heading_deg"),
                "steps": row.get("steps"),
            })

    return jsonify({"filename": f.filename, "points": result_points})


@app.route("/algolab")
def algo_lab():
    with db.get_conn() as conn:
        stable_networks = db.get_stable_networks(conn)
        stable_ble = db.get_stable_ble_devices(conn)
    return render_template("algolab.html", stable_networks=stable_networks,
                           stable_ble=stable_ble)


@app.route("/api/algolab/run", methods=["POST"])
def api_algolab_run():
    """Manually triggers the stability computation (see db.compute_stable_points docstring)."""
    min_sessions = request.get_json(silent=True) or {}
    min_sessions = min_sessions.get("min_sessions", 2)
    with db.get_conn() as conn:
        networks_count, ble_count = db.compute_stable_points(conn, min_sessions=min_sessions)
    return jsonify({"stable_networks_count": networks_count,
                     "stable_ble_count": ble_count})


@app.route("/api/stable_networks")
def api_stable_networks():
    with db.get_conn() as conn:
        return jsonify(db.get_stable_networks(conn))


@app.route("/api/stable_ble_devices")
def api_stable_ble_devices():
    with db.get_conn() as conn:
        return jsonify(db.get_stable_ble_devices(conn))


@app.route("/track/<int:session_id>")
def track_detail(session_id):
    with db.get_conn() as conn:
        session = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not session:
        flash(f"Session {session_id} not found.")
        return redirect(url_for("map_view"))
    return render_template("track_detail.html", session=dict(session))


@app.route("/api/track_detail/<int:session_id>")
def api_track_detail(session_id):
    """
    Full per-point telemetry for one session: real (or estimated, for
    bad_gps points) position, speed, IMU heading/steps, and every WiFi/BLE
    signal seen at that point. Powers the /track/<id> page.
    """
    with db.get_conn() as conn:
        session = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            return jsonify({"error": "session not found"}), 404

        points = conn.execute("""
            SELECT p.id, p.ts, p.lat, p.lon, p.status, p.speed_kmh, p.heading_deg, p.steps,
                   ep.est_lat, ep.est_lon, ep.matched_wifi, ep.matched_ble,
                   ep.error_meters
            FROM points p
            LEFT JOIN estimated_positions ep ON ep.point_id = p.id
            WHERE p.session_id = ?
            ORDER BY p.id
        """, (session_id,)).fetchall()

        result_points = []
        for p in points:
            wifi_obs = conn.execute(
                "SELECT ssid, bssid, rssi FROM observations WHERE point_id=? ORDER BY rssi DESC",
                (p["id"],)
            ).fetchall()
            ble_obs = conn.execute(
                "SELECT mac, rssi FROM ble_observations WHERE point_id=? ORDER BY rssi DESC",
                (p["id"],)
            ).fetchall()
            row = dict(p)
            row["wifi"] = [dict(w) for w in wifi_obs]
            row["ble"] = [dict(b) for b in ble_obs]
            result_points.append(row)

    return jsonify({"session": dict(session), "points": result_points})


@app.route("/admin")
def admin():
    with db.get_conn() as conn:
        runs = conn.execute(
            "SELECT * FROM processing_runs ORDER BY id DESC LIMIT 20"
        ).fetchall()
        networks_count = conn.execute("SELECT COUNT(*) AS c FROM networks").fetchone()["c"]
        ble_devices_count = conn.execute("SELECT COUNT(*) AS c FROM ble_devices").fetchone()["c"]
        estimated_count = conn.execute("SELECT COUNT(*) AS c FROM estimated_positions").fetchone()["c"]
        speed_threshold = db.get_speed_threshold_kmh(conn)
        ble_track_distance = db.get_ble_track_distance_m(conn)
        wifi_track_distance = db.get_wifi_track_distance_m(conn)
    backups = db.list_backups()
    return render_template("admin.html", runs=[dict(r) for r in runs],
                           networks_count=networks_count,
                           ble_devices_count=ble_devices_count,
                           estimated_count=estimated_count,
                           speed_threshold=speed_threshold,
                           ble_track_distance=ble_track_distance,
                           wifi_track_distance=wifi_track_distance,
                           backups=backups)


@app.route("/admin/backup_now", methods=["POST"])
def admin_backup_now():
    path = db.backup_database()
    if path:
        flash(f"Бэкап создан: {os.path.basename(path)}")
    else:
        flash("Не удалось создать бэкап - база данных ещё не существует?")
    return redirect(url_for("admin"))


@app.route("/admin/settings", methods=["POST"])
def admin_settings():
    """Updates the global speed-outlier threshold and the WiFi/BLE far-from-track
    distance thresholds (all apply consistently across the hint, manual Cleanup
    actions where relevant, and the automatic cleanup pass)."""
    try:
        threshold = float(request.form.get("speed_threshold_kmh", 150))
        ble_distance = float(request.form.get("ble_track_distance_m", 200))
        wifi_distance = float(request.form.get("wifi_track_distance_m", 500))
    except (TypeError, ValueError):
        flash("Invalid setting value.")
        return redirect(url_for("admin"))
    with db.get_conn() as conn:
        db.set_setting(conn, "speed_threshold_kmh", threshold)
        db.set_setting(conn, "ble_track_distance_m", ble_distance)
        db.set_setting(conn, "wifi_track_distance_m", wifi_distance)
    flash(f"Speed-outlier threshold set to {threshold:.0f} km/h, "
          f"WiFi track distance set to {wifi_distance:.0f} m, "
          f"BLE track distance set to {ble_distance:.0f} m.")
    return redirect(url_for("admin"))


# ---------------------------------------------------------------------------
# Admin management of Monitoring accounts, devices, and firmware distribution
# (all protected by the regular engineering login, not the Monitoring one)
# ---------------------------------------------------------------------------

@app.route("/admin/users")
def admin_users():
    with db.get_conn() as conn:
        users = db.get_all_monitor_users(conn)
        stats = db.get_user_stats(conn)
    return render_template("admin_users.html", users=users, stats=stats)


@app.route("/admin/users/create", methods=["POST"])
def admin_users_create():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    display_name = request.form.get("display_name", "").strip()
    if not username or not password:
        flash("Username and password are required.")
        return redirect(url_for("admin_users"))
    try:
        with db.get_conn() as conn:
            db.create_monitor_user(conn, username, password, display_name)
        flash(f"Monitoring account '{username}' created.")
    except sqlite3.IntegrityError:
        flash(f"Username '{username}' is already taken.")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/password", methods=["POST"])
def admin_users_password(user_id):
    password = request.form.get("password", "")
    if not password:
        flash("Password is required.")
        return redirect(url_for("admin_users"))
    with db.get_conn() as conn:
        db.set_monitor_user_password(conn, user_id, password)
    flash("Password updated.")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
def admin_users_delete(user_id):
    with db.get_conn() as conn:
        db.delete_monitor_user(conn, user_id)
    flash("Account deleted (its devices are now unassigned, not deleted).")
    return redirect(url_for("admin_users"))


@app.route("/admin/devices")
def admin_devices():
    with db.get_conn() as conn:
        devices = db.get_all_devices(conn)
        users = db.get_all_monitor_users(conn)
        device_types = db.get_all_device_types(conn)
    return render_template("admin_devices.html", devices=devices, users=users, device_types=device_types)


@app.route("/admin/devices/<int:device_id>/update", methods=["POST"])
def admin_devices_update(device_id):
    display_name = request.form.get("display_name", "").strip() or None
    device_type_raw = request.form.get("device_type_id", "")
    device_type_id = int(device_type_raw) if device_type_raw else None
    owner_raw = request.form.get("owner_user_id", "")
    with db.get_conn() as conn:
        if owner_raw == "":
            db.update_device(conn, device_id, display_name=display_name, device_type_id=device_type_id,
                              clear_owner=True)
        else:
            db.update_device(conn, device_id, display_name=display_name, device_type_id=device_type_id,
                              owner_user_id=int(owner_raw))
    flash("Device updated.")
    return redirect(url_for("admin_devices"))


@app.route("/admin/firmware")
def admin_firmware():
    """Contact/partner requests from the public site's form - firmware
    management itself now lives on the Tracker page (see tracker_page)."""
    with db.get_conn() as conn:
        contact_requests = db.get_contact_requests(conn)
    return render_template("admin_firmware.html", contact_requests=contact_requests)


def _firmware_dir_for_type(device_type_id):
    path = os.path.join(FIRMWARE_DIR, str(device_type_id))
    os.makedirs(path, exist_ok=True)
    return path


@app.route("/tracker")
def tracker_page():
    """Flashing a tracker from the browser (Web Serial, esptool-js). The
    firmware itself arrives from the GitHub builds (.github/workflows/
    firmware.yml), so the page offers no file uploads or downloads."""
    with db.get_conn() as conn:
        device_types = db.get_all_device_types(conn)
        for dt in device_types:
            dt["current"] = db.get_current_firmware(conn, dt["id"])
    return render_template("tracker.html", device_types=device_types)


@app.route("/tracker/device_types/add", methods=["POST"])
def tracker_device_type_add():
    if not True:
        flash("Only engineers can add device types.")
        return redirect(url_for("tracker_page"))

    key = request.form.get("key", "").strip().lower().replace(" ", "-")
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    identifies_by = request.form.get("identifies_by", "chip_id")
    if identifies_by not in ("chip_id", "imei"):
        identifies_by = "chip_id"

    if not key or not name:
        flash("Key and name are required.")
        return redirect(url_for("tracker_page"))

    with db.get_conn() as conn:
        try:
            db.create_device_type(conn, key, name, description, identifies_by)
            flash(f"Device type '{name}' added.")
        except sqlite3.IntegrityError:
            flash(f"A device type with key '{key}' already exists.")
    return redirect(url_for("tracker_page"))


@app.route("/tracker/firmware/upload", methods=["POST"])
def tracker_firmware_upload():
    if not True:
        flash("Only engineers can upload firmware.")
        return redirect(url_for("tracker_page"))

    device_type_id = request.form.get("device_type_id", "")
    version = request.form.get("version", "").strip()
    notes = request.form.get("notes", "").strip()
    f = request.files.get("firmware_file")

    if not device_type_id or not version or not f or not f.filename:
        flash("Device type, version, and a firmware file are all required.")
        return redirect(url_for("tracker_page"))

    device_type_id = int(device_type_id)
    filename = secure_filename(f.filename)
    # Prefix with version so multiple uploads never collide on disk, even if
    # someone re-uploads a file with the exact same original name.
    stored_name = f"{version}__{filename}"
    dest_dir = _firmware_dir_for_type(device_type_id)
    f.save(os.path.join(dest_dir, stored_name))

    with db.get_conn() as conn:
        db.add_firmware(conn, device_type_id, version, stored_name, notes, set_current=True)
    flash(f"Firmware {version} uploaded and set as current.")
    return redirect(url_for("tracker_page"))


@app.route("/tracker/firmware/<int:firmware_id>/set_current", methods=["POST"])
def tracker_firmware_set_current(firmware_id):
    if not True:
        flash("Only engineers can change the current firmware.")
        return redirect(url_for("tracker_page"))
    with db.get_conn() as conn:
        fw = db.get_firmware(conn, firmware_id)
        if fw:
            db.set_current_firmware(conn, fw["device_type_id"], firmware_id)
            flash(f"Version {fw['version']} is now current.")
    return redirect(url_for("tracker_page"))


@app.route("/tracker/firmware/<int:firmware_id>/download")
def tracker_firmware_download(firmware_id):
    with db.get_conn() as conn:
        fw = db.get_firmware(conn, firmware_id)
    if not fw:
        flash("Firmware not found.")
        return redirect(url_for("tracker_page"))
    dest_dir = _firmware_dir_for_type(fw["device_type_id"])
    return send_from_directory(dest_dir, fw["filename"], as_attachment=True)


@app.route("/tracker/flash")
def tracker_flash_page():
    return redirect(url_for("tracker_page"))


@app.route("/admin/reprocess", methods=["POST"])
def admin_reprocess():
    summary = jobs.run_processing()
    if summary["status"] == "ok":
        flash(f"Reprocessing done: {summary['networks_count']} WiFi networks, "
              f"{summary['ble_devices_count']} BLE devices, "
              f"{summary['estimated_count']} position estimates computed, "
              f"{summary['timestamps_fixed']} implausible-timestamp + "
              f"{summary['speed_outliers_fixed']} speed-outlier point(s) flagged, "
              f"{summary['bad_gps_purged']} bad_gps point(s) permanently deleted, "
              f"{summary['cotravel_wifi_excluded']} WiFi + {summary['cotravel_ble_excluded']} BLE "
              f"co-traveling device(s) excluded, "
              f"{summary['wifi_far_from_track_excluded']} WiFi + "
              f"{summary['ble_far_from_track_excluded']} BLE excluded as far-from-track, "
              f"{summary['stable_networks_count']} stable WiFi + "
              f"{summary['stable_ble_count']} stable BLE (map's bright layer).")
    else:
        flash(f"Reprocessing failed: {summary['status']}")
    return redirect(url_for("admin"))


@app.route("/reports")
def reports():
    return render_template("reports.html")


@app.route("/roadmap")
def roadmap():
    with db.get_conn() as conn:
        sections = db.get_roadmap_items(conn)
    return render_template("roadmap.html", sections=sections)


@app.route("/roadmap/add", methods=["POST"])
def roadmap_add():
    section = request.form.get("section", "").strip()
    title = request.form.get("title", "").strip()
    body = request.form.get("body", "").strip() or None
    kind = request.form.get("kind", "task")
    author = request.form.get("author", "").strip() or None
    if not title or section not in dict(db.ROADMAP_SECTIONS):
        flash("Заполните раздел и заголовок.")
        return redirect(url_for("roadmap"))
    with db.get_conn() as conn:
        db.add_roadmap_item(conn, section, title, body, kind=kind, author=author)
    return redirect(url_for("roadmap"))


@app.route("/roadmap/<int:item_id>/status", methods=["POST"])
def roadmap_set_status(item_id):
    status = request.form.get("status", "todo")
    with db.get_conn() as conn:
        db.update_roadmap_item_status(conn, item_id, status)
    return redirect(url_for("roadmap"))


@app.route("/roadmap/<int:item_id>/delete", methods=["POST"])
def roadmap_delete(item_id):
    with db.get_conn() as conn:
        db.delete_roadmap_item(conn, item_id)
    return redirect(url_for("roadmap"))


@app.route("/api/reports/candidates")
def api_reports_candidates():
    """Currently detected co-traveling WiFi/BLE candidates (see excluded_devices)."""
    with db.get_conn() as conn:
        return jsonify(db.get_excluded_devices(conn))


@app.route("/api/reports/detect", methods=["POST"])
def api_reports_detect():
    """
    Runs co-traveling device detection now (manual trigger - it also runs
    automatically after every upload and daily). Any newly detected
    candidate is excluded immediately.
    """
    data = request.get_json(silent=True) or {}
    threshold = data.get("presence_ratio_threshold")
    with db.get_conn() as conn:
        wifi_count, ble_count = db.detect_cotraveling_devices(conn, presence_ratio_threshold=threshold)
    return jsonify({"wifi_candidates": wifi_count, "ble_candidates": ble_count})


@app.route("/api/reports/toggle_exclude", methods=["POST"])
def api_reports_toggle_exclude():
    """
    {key, excluded} - actually including/excluding a candidate from the live
    networks/ble_devices aggregates IS "deploying to the main database" -
    there's no separate test-mode switch (see excluded_devices docstring).
    Triggers an immediate reprocessing pass so the change takes effect right away.
    """
    data = request.get_json(force=True)
    key = data.get("key")
    excluded = bool(data.get("excluded"))
    if not key:
        return jsonify({"error": "key is required"}), 400
    with db.get_conn() as conn:
        db.set_device_excluded(conn, key, excluded)
    jobs.run_processing()
    return jsonify({"key": key, "excluded": excluded})


@app.route("/api/reports/session_comparison")
def api_reports_session_comparison():
    """Old-vs-new algorithm comparison for one session - powers the Reports page."""
    session_id = request.args.get("session_id", type=int)
    if not session_id:
        return jsonify({"error": "session_id is required"}), 400
    with db.get_conn() as conn:
        session = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            return jsonify({"error": "session not found"}), 404
        report = db.compute_session_report(conn, session_id)
    return jsonify({"session": dict(session), **report})


@app.route("/api/reports/removal_summary")
def api_reports_removal_summary():
    """How many points are currently bad_gps by reason, and devices excluded - stat cards."""
    with db.get_conn() as conn:
        return jsonify(db.get_removal_summary(conn))


@app.route("/api/reports/removal_log")
def api_reports_removal_log():
    """Chronological log of automatic/manual cleanup actions - points and device exclusions."""
    limit = request.args.get("limit", default=100, type=int)
    with db.get_conn() as conn:
        return jsonify(db.get_removal_log(conn, limit))


@app.route("/api/reports/removal_points")
def api_reports_removal_points():
    """Individual removed points (with real coordinates) for the Reports map."""
    with db.get_conn() as conn:
        return jsonify(db.get_removal_points_for_map(conn))


@app.route("/api/reports/restore_device", methods=["POST"])
def api_reports_restore_device():
    """Un-excludes a device (e.g. a false positive from co-traveling detection) and
    pins that choice so future auto-detection won't silently re-exclude it."""
    data = request.get_json(force=True)
    key = data.get("key")
    if not key:
        return jsonify({"error": "key is required"}), 400
    with db.get_conn() as conn:
        db.set_device_excluded(conn, key, False)
    jobs.run_processing()
    return jsonify({"key": key, "excluded": False})


@app.route("/cleanup")
def cleanup():
    with db.get_conn() as conn:
        speed_threshold = db.get_speed_threshold_kmh(conn)
    return render_template("cleanup.html", speed_threshold=speed_threshold)


@app.route("/api/suspicious_jumps")
def api_suspicious_jumps():
    """
    Heuristic hint (not an automatic decision): consecutive 'ok' points whose
    implied speed is implausible (default threshold: the one global setting,
    see db.get_speed_threshold_kmh). See db.py docstring - this only
    surfaces candidates for a human to inspect on the Cleanup page.
    """
    session_id = request.args.get("session_id", type=int)
    with db.get_conn() as conn:
        default_threshold = db.get_speed_threshold_kmh(conn)
        threshold = request.args.get("threshold_kmh", default=default_threshold, type=float)
        jumps = db.find_suspicious_jumps(conn, session_id, threshold)
    return jsonify(jumps)


@app.route("/api/cleanup/expand_excursion", methods=["POST"])
def api_cleanup_expand_excursion():
    """
    Given one clicked suspicious segment (two adjacent point ids in the same
    session), expands it into the full run of consecutive points that belong
    to the same corrupted excursion - see db.find_excursion docstring for why
    this matters (a single jamming event usually corrupts several consecutive
    fixes, not just one).
    """
    data = request.get_json(force=True)
    try:
        session_id = int(data["session_id"])
        point_id_a = int(data["point_id_a"])
        point_id_b = int(data["point_id_b"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "session_id, point_id_a, point_id_b (integers) are required"}), 400
    with db.get_conn() as conn:
        threshold = data.get("threshold_kmh") or db.get_speed_threshold_kmh(conn)
        result = db.find_excursion(conn, session_id, point_id_a, point_id_b, threshold)

    if result is None:
        return jsonify({"error": "those points aren't adjacent 'ok' points in that session"}), 400
    return jsonify(result)


@app.route("/api/cleanup/preview", methods=["POST"])
def api_cleanup_preview():
    """
    Returns how many 'ok' points match the selection and which WiFi networks
    were seen at them - so the person can check this looks like the jamming
    artifact they meant to select before doing anything to it.

    Selection is either:
    - a bounding box: {min_lat, max_lat, min_lon, max_lon, session_id?}
    - an explicit list of points (e.g. from clicking a track segment/point
      directly on the Cleanup page): {point_ids: [...]}
    """
    data = request.get_json(force=True)

    if data.get("point_ids"):
        try:
            point_ids = [int(pid) for pid in data["point_ids"]]
        except (TypeError, ValueError):
            return jsonify({"error": "point_ids must be a list of integers"}), 400
        with db.get_conn() as conn:
            wifi, ble = db.preview_bbox_networks(conn, point_ids)
        return jsonify({
            "point_count": len(point_ids),
            "point_ids": point_ids,
            "wifi_networks": wifi,
            "ble_devices": ble,
        })

    try:
        min_lat, max_lat = float(data["min_lat"]), float(data["max_lat"])
        min_lon, max_lon = float(data["min_lon"]), float(data["max_lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({
            "error": "either point_ids, or min_lat/max_lat/min_lon/max_lon (numbers), are required"
        }), 400
    session_id = data.get("session_id")

    with db.get_conn() as conn:
        points = db.find_points_in_bbox(conn, min_lat, max_lat, min_lon, max_lon, session_id)
        point_ids = [p["id"] for p in points]
        wifi, ble = db.preview_bbox_networks(conn, point_ids)

    return jsonify({
        "point_count": len(point_ids),
        "point_ids": point_ids,
        "wifi_networks": wifi,
        "ble_devices": ble,
    })


@app.route("/api/cleanup/apply", methods=["POST"])
def api_cleanup_apply():
    """
    Applies an action to a set of points (normally the point_ids returned by
    /api/cleanup/preview for a bounding box the person just drew):
    - action="mark_bad_gps" (recommended, reversible - see db.py docstring)
    - action="delete" (permanent)
    Refreshes the network/device aggregates immediately afterwards so the
    fix takes effect right away rather than waiting for the next daily run.
    """
    data = request.get_json(force=True)
    point_ids = data.get("point_ids") or []
    action = data.get("action")
    if not point_ids:
        return jsonify({"error": "point_ids (non-empty list) is required"}), 400
    if action not in ("mark_bad_gps", "delete"):
        return jsonify({"error": "action must be 'mark_bad_gps' or 'delete'"}), 400

    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT session_id FROM points WHERE id IN ({','.join('?' for _ in point_ids)})",
            point_ids,
        ).fetchall()
        affected_sessions = [r["session_id"] for r in rows]

        if action == "mark_bad_gps":
            db.mark_points_bad_gps(conn, point_ids)
        else:
            db.delete_points(conn, point_ids)

        db.refresh_session_counters(conn, affected_sessions)

    jobs.run_processing()  # refresh networks/ble_devices/estimated_positions right away

    return jsonify({"affected_points": len(point_ids), "action": action})


@app.route("/api/cleanup/restore", methods=["POST"])
def api_cleanup_restore():
    """Undo: flips the given points back to 'ok' (only works for mark_bad_gps, not delete)."""
    data = request.get_json(force=True)
    point_ids = data.get("point_ids") or []
    if not point_ids:
        return jsonify({"error": "point_ids (non-empty list) is required"}), 400

    with db.get_conn() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT session_id FROM points WHERE id IN ({','.join('?' for _ in point_ids)})",
            point_ids,
        ).fetchall()
        affected_sessions = [r["session_id"] for r in rows]
        db.restore_points_ok(conn, point_ids)
        db.refresh_session_counters(conn, affected_sessions)

    jobs.run_processing()

    return jsonify({"restored_points": len(point_ids)})


@app.route("/api/track/historical")
def api_track_historical():
    """
    The historical background layer for the Map (see templates/index.html) -
    returns a deduplicated ROAD GRAPH, not one polyline per session.

    Snapping points onto a shared grid (previous approach) makes two
    sessions through the same corridor land on identical coordinates, but
    that alone still draws one polyline PER SESSION - if a hundred sessions
    passed through the same junction with even slightly different paths (a
    different lane, a different turn, or just enough GPS noise to zig-zag
    across a grid boundary once), the result is still a hundred crossing
    line segments layered on top of each other, which is exactly the "mess
    of many lines" this was supposed to solve (confirmed from real map
    screenshots showing precisely that at a busy junction).

    So this goes one step further: after snapping, every session's path
    becomes a sequence of grid-cell visits; each CONSECUTIVE PAIR of cells
    is one graph edge. Across all sessions, edges are collected into a SET,
    keyed by the (unordered) pair of cells - so no matter how many hundreds
    of sessions traversed the exact same physical segment, it is a single
    entry in that set and gets drawn exactly once. This is the literal,
    structural version of "не наслаиваем" (don't stack overlapping lines) -
    not hoping polylines happen to coincide pixel-for-pixel, but guaranteeing
    it by only ever emitting one segment per physical stretch of road,
    regardless of history size. Genuinely different routes (a real fork, not
    just noise) still show up as their own distinct edges, as they should -
    this doesn't force divergent paths into one fictitious line, it only
    ever removes true duplication.

    The "last 20 sessions" thick red layer (see /api/track/recent) is
    deliberately drawn from raw, un-snapped points instead, so it stays
    visually distinct from this deduplicated background on top of already
    being a different color/weight.
    """
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT session_id, lat, lon FROM points "
            "WHERE status='ok' AND lat IS NOT NULL AND lon IS NOT NULL ORDER BY session_id, id"
        ).fetchall()

    grid_size = 0.0004  # ~35-44m at mid-latitudes - comfortably wider than
    # typical consumer-GPS noise (often 5-20m, more under tree cover/urban
    # canyon), so a point sitting near a cell boundary doesn't randomly
    # "spill over" into a neighboring cell on one pass and not another.

    def grid_key(lat, lon):
        return (round(lat / grid_size), round(lon / grid_size))

    # Pass 1: one global centroid per occupied cell, using every session
    # that ever touched it - this is what makes different sessions snap to
    # IDENTICAL coordinates for the same physical spot.
    cells = {}
    for r in rows:
        gk = grid_key(r["lat"], r["lon"])
        c = cells.setdefault(gk, [0.0, 0.0, 0])
        c[0] += r["lat"]; c[1] += r["lon"]; c[2] += 1
    centroids = {gk: (c[0] / c[2], c[1] / c[2]) for gk, c in cells.items()}

    # Pass 2: walk each session in order, collapse consecutive same-cell
    # points, and record one graph edge per consecutive DISTINCT cell pair.
    # Edges are undirected (A-B and B-A are the same physical segment) and
    # deduplicated via a set, keyed by the sorted cell-pair - this is the
    # actual dedup mechanism, not the grid-snapping alone.
    #
    # Edges longer than MAX_EDGE_METERS are dropped outright rather than
    # drawn. Deduplication only cleans up *consistent* repeated noise around
    # a real path - it can't turn genuinely erratic data (GPS jamming/
    # spoofing that was never cleaned up on the Cleanup page, or any other
    # spurious jump) into a sensible line, because there's no real path
    # there to converge on in the first place. A jump this large between
    # consecutive points is exactly what the app already treats as
    # suspicious elsewhere (see get_speed_threshold_kmh / suspicious jumps) -
    # here it's simplest to just never draw it, rather than let a handful of
    # bad points fan out into dozens of stray lines converging on one spot.
    MAX_EDGE_METERS = 300

    edges = set()
    last_key_per_session = {}
    for r in rows:
        gk = grid_key(r["lat"], r["lon"])
        sid = r["session_id"]
        prev = last_key_per_session.get(sid)
        if prev is not None and prev != gk:
            a, b = centroids[prev], centroids[gk]
            if db._haversine_meters(a[0], a[1], b[0], b[1]) <= MAX_EDGE_METERS:
                edges.add(tuple(sorted((prev, gk))))
        last_key_per_session[sid] = gk

    result = [
        [list(centroids[a]), list(centroids[b])]
        for a, b in edges
    ]
    return jsonify(result)


@app.route("/api/track")
def api_track():
    """Returns all 'ok' points (id/session_id/lat/lon/ts) as JSON, for drawing the red track."""
    session_id = request.args.get("session_id", type=int)
    with db.get_conn() as conn:
        if session_id:
            rows = conn.execute(
                "SELECT id, session_id, lat, lon, ts FROM points WHERE status='ok' AND session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, session_id, lat, lon, ts FROM points WHERE status='ok' ORDER BY id"
            ).fetchall()
    return jsonify([{"id": r["id"], "session_id": r["session_id"], "lat": r["lat"],
                      "lon": r["lon"], "ts": r["ts"]} for r in rows])


@app.route("/api/track/recent")
def api_track_recent():
    """
    GPS points for only the N most recently uploaded sessions (default 20) -
    the "thick red" recent-tracks layer on the Map. As new sessions arrive
    and push older ones out of this window, those older sessions simply stop
    appearing here - they're still covered by /api/track's full-history line,
    so nothing needs to be explicitly "moved" or recomputed; the two-layer
    split does that on its own every time the map reloads this endpoint.
    """
    limit = request.args.get("limit", default=20, type=int)
    with db.get_conn() as conn:
        session_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM sessions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]
        if not session_ids:
            return jsonify([])
        placeholders = ",".join("?" * len(session_ids))
        rows = conn.execute(
            f"SELECT id, session_id, lat, lon, ts FROM points "
            f"WHERE status='ok' AND session_id IN ({placeholders}) ORDER BY session_id, id",
            session_ids,
        ).fetchall()
    return jsonify([{"id": r["id"], "session_id": r["session_id"], "lat": r["lat"],
                      "lon": r["lon"], "ts": r["ts"]} for r in rows])


@app.route("/api/track/showroom")
def api_track_showroom():
    """
    "Showroom" mode data: for the N most recently uploaded sessions, the
    route reconstructed using ONLY confirmed stable WiFi/BLE reference
    points (see db.compute_showroom_track) - i.e. what the route would look
    like for a device with no GPS at all, relying only on the reliable part
    of the network database. Lines only, by design.
    """
    limit = request.args.get("limit", default=20, type=int)
    with db.get_conn() as conn:
        points = db.compute_showroom_track(conn, limit=limit)
    return jsonify(points)


@app.route("/api/networks")
def api_networks():
    """Returns all known WiFi networks with their averaged estimated position."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT ssid, bssid, avg_lat, avg_lon, avg_rssi, observation_count, first_seen, last_seen "
            "FROM networks ORDER BY observation_count DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/ble_devices")
def api_ble_devices():
    """Returns all known BLE device sightings with their averaged estimated position."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT mac, avg_lat, avg_lon, avg_rssi, observation_count, first_seen, last_seen "
            "FROM ble_devices ORDER BY observation_count DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/sessions")
def api_sessions():
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, filename, uploaded_at, file_date, points_total, points_ok, points_bad_gps "
            "FROM sessions ORDER BY id DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/locate")
def api_locate():
    """
    GPS-independent position estimate from WiFi and/or BLE sightings.

    Query params (at least one required):
    - ssids=SSID1,SSID2,...       match WiFi by network name (less precise - names collide)
    - bssids=AA:BB:..,CC:DD:..    match WiFi by MAC address (precise - use when available)
    - ble_macs=AA:BB:..,CC:DD:..  match BLE by device MAC address

    Returns the observation-count-weighted centroid of all matching known networks/
    devices, plus how many were actually matched. No RSSI is available from this
    endpoint (the person just pastes names/ids), so it can't do the distance-based
    weighting used elsewhere (see db.match_position / db.recompute_estimated_positions).
    """
    ssids_param = request.args.get("ssids", "")
    bssids_param = request.args.get("bssids", "")
    ble_macs_param = request.args.get("ble_macs", "")
    ssids = [s.strip() for s in ssids_param.split(",") if s.strip()]
    bssids = [b.strip() for b in bssids_param.split(",") if b.strip()]
    ble_macs = [m.strip() for m in ble_macs_param.split(",") if m.strip()]

    if not ssids and not bssids and not ble_macs:
        return jsonify({
            "error": "Provide ?ssids=..., ?bssids=..., and/or ?ble_macs=... (at least one)"
        }), 400

    with db.get_conn() as conn:
        rows = []
        if bssids:
            placeholders = ",".join("?" for _ in bssids)
            rows += [dict(r) for r in conn.execute(
                f"SELECT ssid, bssid, avg_lat, avg_lon, observation_count FROM networks "
                f"WHERE bssid IN ({placeholders})",
                bssids,
            ).fetchall()]
        if ssids:
            placeholders = ",".join("?" for _ in ssids)
            rows += [dict(r) for r in conn.execute(
                f"SELECT ssid, bssid, avg_lat, avg_lon, observation_count FROM networks "
                f"WHERE ssid IN ({placeholders})",
                ssids,
            ).fetchall()]
        if ble_macs:
            placeholders = ",".join("?" for _ in ble_macs)
            ble_rows = conn.execute(
                f"SELECT mac, avg_lat, avg_lon, observation_count FROM ble_devices "
                f"WHERE mac IN ({placeholders})",
                ble_macs,
            ).fetchall()
            for r in ble_rows:
                rows.append({
                    "ssid": f"BLE {r['mac']}",
                    "bssid": r["mac"],
                    "avg_lat": r["avg_lat"],
                    "avg_lon": r["avg_lon"],
                    "observation_count": r["observation_count"],
                })

    # de-duplicate (a network could match both an ssid and a bssid filter)
    seen_keys = set()
    unique_rows = []
    for r in rows:
        k = r["bssid"] or r["ssid"]
        if k in seen_keys:
            continue
        seen_keys.add(k)
        unique_rows.append(r)
    rows = unique_rows

    requested = len(set(ssids) | set(bssids) | set(ble_macs))

    if not rows:
        return jsonify({
            "matched": 0,
            "requested": requested,
            "estimate": None,
            "message": "None of the given networks/devices are in the database yet.",
        })

    total_weight = sum(r["observation_count"] for r in rows)
    weighted_lat = sum(r["avg_lat"] * r["observation_count"] for r in rows) / total_weight
    weighted_lon = sum(r["avg_lon"] * r["observation_count"] for r in rows) / total_weight

    return jsonify({
        "matched": len(rows),
        "requested": requested,
        "estimate": {"lat": weighted_lat, "lon": weighted_lon},
        "matched_networks": rows,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
