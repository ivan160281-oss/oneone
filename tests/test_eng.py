import json

from tests.test_app import SD_LOG, SHARED, client, login, make_client  # noqa: F401


def test_engineering_part_is_admin_only(client):
    r = client.get("/eng/map", follow_redirects=False)
    assert r.status_code in (302, 303) and r.headers["location"] == "/"
    assert client.get("/eng/api/networks").status_code == 401

    pw = make_client(client)
    login(client, "ivan", pw)
    assert client.get("/eng/map", follow_redirects=False).headers["location"] == "/"
    assert client.get("/eng/api/sessions").status_code == 401
    client.post("/api/auth/logout")
    client.cookies.clear()

    login(client, "admin", "admin-pass")
    r = client.get("/eng/map")
    assert r.status_code == 200
    # page scripts get the /eng prefix added to their fetch('/api/...') calls
    assert '"/eng"' in r.text
    for page in ("/eng/reports", "/eng/cleanup", "/eng/algolab", "/eng/admin", "/eng/admin/devices",
                 "/eng/tracker", "/eng/tracker/flash", "/eng/roadmap", "/eng/upload"):
        assert client.get(page).status_code == 200, page
    assert client.get("/eng/static/vendor/leaflet/js/leaflet.js").status_code == 200
    assert "/eng/map" in client.get("/admin").text


def test_device_data_reaches_engineering_db(client):
    from eng import db as eng_db
    from eng import feed

    imei = "860000000000777"  # the engineering DB is shared by all tests: use a fresh device
    headers = {"X-Sync-Password": SHARED, "X-Device-IMEI": imei}
    pts = [{"seq": i + 1, "ts": f"2026-09-30T10:{i:02d}:00", "lat": 55.75 + i * 0.001, "lon": 37.62, "status": "ok",
            "wifi": [{"ssid": "EngNet", "bssid": "ee:bb:cc:dd:ee:01", "rssi": -50}], "ble": []}
           for i in range(3)]
    assert client.post("/api/device/points", content=json.dumps({"points": pts}), headers=headers).status_code == 200

    fname = "log_20260930_120000.txt"
    r = client.post("/api/upload", files={"logfiles": (fname, SD_LOG.encode(), "text/plain")},
                    headers={"X-Sync-Password": SHARED})
    assert r.status_code == 200, r.text
    # the full processing pass runs fine over what arrived
    feed.log_files([(fname, SD_LOG.encode())], background=False)

    with eng_db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM devices WHERE imei = ?", (imei,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM observations WHERE ssid = 'EngNet'").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE filename = ?", (fname,)).fetchone()[0] == 1

    login(client, "admin", "admin-pass")
    r = client.get("/eng/api/networks")
    assert r.status_code == 200 and isinstance(r.json(), list)
    assert client.get("/eng/api/sessions").status_code == 200
