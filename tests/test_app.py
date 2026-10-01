import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

SHARED = "shared-secret"
IMEI = "860000000000001"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ALTGEO_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("WIFIGPS_PASSWORD", SHARED)
    monkeypatch.setenv("ADMIN_LOGIN", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-pass")
    from app import main
    main.throttle.fails.clear()
    with TestClient(main.app) as c:
        yield c


def login(c, user, pw):
    r = c.post("/api/auth/login", json={"login": user, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()


def make_client(c, name="ivan"):
    login(c, "admin", "admin-pass")
    r = c.post("/api/admin/clients", json={"login": name, "name": "Иван"})
    assert r.status_code == 201
    pw = r.json()["password"]
    c.post("/api/auth/logout")
    c.cookies.clear()
    return pw


def gsm_points(points):
    return {"points": points}


def test_login_page_and_bad_password(client):
    assert "Войти" in client.get("/").text
    r = client.post("/api/auth/login", json={"login": "admin", "password": "nope"})
    assert r.status_code == 401
    assert client.get("/api/me").status_code == 401


def test_login_throttle(client):
    for _ in range(10):
        client.post("/api/auth/login", json={"login": "admin", "password": "x"})
    r = client.post("/api/auth/login", json={"login": "admin", "password": "admin-pass"})
    assert r.status_code == 429


def test_form_post_rejected(client):
    r = client.post("/api/auth/login", data={"login": "admin", "password": "admin-pass"})
    assert r.status_code == 415


def test_admin_creates_client_and_client_logs_in(client):
    pw = make_client(client)
    assert login(client, "ivan", pw)["redirect"] == "/cabinet"
    assert client.get("/", follow_redirects=False).headers["location"] == "/cabinet"
    assert client.get("/admin", follow_redirects=False).status_code == 303
    assert client.get("/api/admin/clients").status_code == 403


def test_blocked_client_cannot_login(client):
    pw = make_client(client)
    login(client, "admin", "admin-pass")
    cid = client.get("/api/admin/clients").json()[0]["id"]
    client.patch(f"/api/admin/clients/{cid}", json={"is_active": False})
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"login": "ivan", "password": pw})
    assert r.status_code == 401


def test_gsm_tracker_track_has_no_wifi_or_ble(client):
    pw = make_client(client)
    login(client, "ivan", pw)
    r = client.post("/api/my/devices", json={"name": "Машина", "kind": "imei", "imei": IMEI})
    assert r.status_code == 201
    dev = r.json()

    headers = {"X-Sync-Password": SHARED, "X-Device-IMEI": IMEI}
    pts = [
        {"seq": i, "ts": f"2026-09-30T10:{i:02d}:00", "lat": 55.75 + i * 0.001, "lon": 37.62, "status": "ok",
         "wifi": [{"ssid": "Home", "bssid": "aa:bb:cc:dd:ee:01", "rssi": -50}], "ble": [{"mac": "11:22:33:44:55:66", "rssi": -70}]}
        for i in range(5)
    ]
    r = client.post("/api/device/points", content=json.dumps(gsm_points(pts)), headers=headers)
    assert r.json() == {"acked_seqs": [0, 1, 2, 3, 4]}
    # retry of the same batch is acknowledged again, without duplicates
    r = client.post("/api/device/points", content=json.dumps(gsm_points(pts)), headers=headers)
    assert r.json()["acked_seqs"] == [0, 1, 2, 3, 4]

    start = int(datetime(2026, 9, 30, 10, tzinfo=timezone.utc).timestamp())
    r = client.get(f"/api/my/devices/{dev['id']}/track", params={"t_from": start, "t_to": start + 3600})
    body = r.json()
    assert len(body["track"]) == 5
    text = r.text.lower()
    for word in ("wifi", "ble", "bssid", "rssi", "ssid", "aa:bb"):
        assert word not in text
    assert set(body["track"][0]) == {"t", "lat", "lon"}
    assert body["summary"]["distance_km"] > 0.4

    live = client.get("/api/my/live").json()
    assert live[0]["track"] and "wifi" not in json.dumps(live).lower()


def test_gps_gap_filled_from_wifi(client):
    pw = make_client(client)
    login(client, "ivan", pw)
    dev = client.post("/api/my/devices", json={"name": "Машина", "kind": "imei", "imei": IMEI}).json()
    headers = {"X-Sync-Password": SHARED, "X-Device-IMEI": IMEI}
    ap = [{"ssid": 'Kafe "Ромашка"', "bssid": "aa:bb:cc:dd:ee:02", "rssi": -45}]
    pts = [
        {"seq": 1, "ts": "2026-09-30T10:00:00", "lat": 55.0, "lon": 37.0, "status": "ok", "wifi": ap, "ble": []},
        {"seq": 2, "ts": "2026-09-30T10:00:30", "lat": None, "lon": None, "status": "bad_gps", "wifi": ap, "ble": []},
    ]
    # Same raw text the firmware sends: SSID quotes are not escaped.
    body = json.dumps(gsm_points(pts), separators=(",", ":"), ensure_ascii=False).replace('\\"', '"')
    r = client.post("/api/device/points", content=body, headers=headers)
    assert r.json()["acked_seqs"] == [1, 2]
    start = int(datetime(2026, 9, 30, 10, tzinfo=timezone.utc).timestamp())
    track = client.get(f"/api/my/devices/{dev['id']}/track", params={"t_from": start, "t_to": start + 60}).json()["track"]
    assert len(track) == 2
    assert track[1]["lat"] == pytest.approx(55.0) and track[1]["lon"] == pytest.approx(37.0)


def test_other_client_cannot_see_device(client):
    pw1 = make_client(client, "one")
    pw2 = make_client(client, "two")
    login(client, "one", pw1)
    dev = client.post("/api/my/devices", json={"name": "A", "kind": "imei", "imei": IMEI}).json()
    client.cookies.clear()
    login(client, "two", pw2)
    assert client.get(f"/api/my/devices/{dev['id']}/track", params={"t_from": 0, "t_to": 100}).status_code == 404
    assert client.delete(f"/api/my/devices/{dev['id']}", headers={"Content-Type": "application/json"}).status_code == 404
    r = client.post("/api/my/devices", json={"name": "B", "kind": "imei", "imei": IMEI})
    assert r.status_code == 409


SD_LOG = "\n".join([
    "23:59:00_55.750000_37.620000_ok_NA_My_Net|AA:BB:CC:DD:EE:03|-60,Other|AA:BB:CC:DD:EE:04|-80__11:22:33:44:55:77|-70_123.5_10",
    "23:59:30_0.000000_0.000000_bad_gps_NA_My_Net|AA:BB:CC:DD:EE:03|-60___NA_NA",
    "00:00:00_55.751000_37.620000_ok_NA____NA_NA",
])


def test_sd_tracker_with_own_sync_password(client):
    pw = make_client(client)
    login(client, "ivan", pw)
    dev = client.post("/api/my/devices", json={"name": "Пейджер", "kind": "sd"}).json()
    token = dev["sync_password"]
    assert token

    h = {"X-Sync-Password": token}
    fname = "log_20260930_233000.txt"
    r = client.post("/api/sync/check", json={"filenames": [fname, "../evil.txt"]}, headers=h)
    assert r.json() == {"missing": [fname]}
    r = client.post("/api/upload", files={"logfiles": (fname, SD_LOG.encode(), "text/plain")}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["stored"][0]["points"] == 3
    assert client.post("/api/sync/check", json={"filenames": [fname]}, headers=h).json() == {"missing": []}

    start = int(datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc).timestamp())
    track = client.get(f"/api/my/devices/{dev['id']}/track", params={"t_from": start, "t_to": start + 7200}).json()["track"]
    assert [p["t"] - start for p in track] == [3540, 3570, 3600]  # midnight rolled over to Oct 1


def test_sync_password_checks(client):
    assert client.post("/api/sync/check", json={"filenames": []}, headers={"X-Sync-Password": "bad"}).status_code == 401
    r = client.post("/api/sync/check", json={"filenames": ["log_uptime_12.txt"]}, headers={"X-Sync-Password": SHARED})
    assert r.json() == {"missing": ["log_uptime_12.txt"]}
    r = client.post("/api/device/points", content="{}", headers={"X-Sync-Password": "bad", "X-Device-IMEI": IMEI})
    assert r.status_code == 401
    # Unparseable batch: acknowledged anyway so it can't block the device queue forever.
    r = client.post("/api/device/points", content='{"points":[{"seq":7,"ts":"x"broken}]}'.encode(),
                    headers={"X-Sync-Password": SHARED, "X-Device-IMEI": IMEI})
    assert r.json() == {"acked_seqs": [7]}


def test_gpx_export(client):
    pw = make_client(client)
    login(client, "ivan", pw)
    dev = client.post("/api/my/devices", json={"name": "Машина", "kind": "imei", "imei": IMEI}).json()
    client.post("/api/device/points", headers={"X-Sync-Password": SHARED, "X-Device-IMEI": IMEI},
                content=json.dumps(gsm_points([{"seq": 1, "ts": "2026-09-30T10:00:00", "lat": 55.0, "lon": 37.0,
                                                "status": "ok", "wifi": [], "ble": []}])))
    start = int(datetime(2026, 9, 30, 10, tzinfo=timezone.utc).timestamp())
    r = client.get(f"/api/my/devices/{dev['id']}/track.gpx", params={"t_from": start - 60, "t_to": start + 60})
    assert '<trkpt lat="55.0" lon="37.0"><time>2026-09-30T10:00:00Z</time>' in r.text
