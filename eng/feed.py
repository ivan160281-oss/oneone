"""Hands device data received by the main site to the engineering database.

Devices post to the main site only (/api/upload, /api/device/points). The
engineering part keeps its own full copy with every WiFi/BLE sighting; these
functions write that copy. They never raise: a failure here must not make a
device resend data the main site has already stored.
"""
import logging
import os
import threading

log = logging.getLogger("altgeo.eng")
_processing = threading.Lock()


class _File:
    """The bit of werkzeug's FileStorage that web._process_uploaded_files uses."""

    def __init__(self, filename, data):
        self.filename = filename
        self._data = data

    def save(self, path):
        with open(path, "wb") as f:
            f.write(self._data)


def _run_processing():
    # One full pass at a time; a pass started meanwhile would redo the same work.
    if not _processing.acquire(blocking=False):
        return
    try:
        from . import jobs
        jobs.run_processing()
    except Exception:
        log.exception("engineering processing failed")
    finally:
        _processing.release()


def log_files(files, chip_id=None, background=True):
    """files: list of (filename, bytes) from an SD tracker upload."""
    try:
        from . import db, web
        with db.get_conn() as conn:
            for name, data in files:
                # A re-sent file replaces the earlier copy, as on the main site.
                conn.execute("DELETE FROM sessions WHERE filename = ?", (name,))
            web._process_uploaded_files(conn, [_File(n, d) for n, d in files], device_chip_id=chip_id)
    except Exception:
        log.exception("engineering copy of log files failed")
        return
    if os.environ.get("ALTGEO_ENG_JOBS", "1") == "0" and background:
        return
    if background:
        threading.Thread(target=_run_processing, daemon=True).start()
    else:
        _run_processing()


def realtime_points(imei, points):
    """points: the parsed {"points": [...]} list from a GSM tracker."""
    try:
        from . import db, web  # noqa: F401  (web creates the tables)
        with db.get_conn() as conn:
            device = db.get_or_create_device_by_imei(conn, imei)
            db.ingest_realtime_points(conn, device["id"], points)
    except Exception:
        log.exception("engineering copy of realtime points failed")
