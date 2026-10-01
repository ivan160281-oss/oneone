"""ALTGEO server: tracker ingest, client cabinet, admin."""
import hmac
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from . import auth, db, ingest, track

STATIC = Path(__file__).parent / "static"
LIVE_TAIL_S = 2 * 3600
MAX_REPORT_S = 31 * 24 * 3600
IMEI_RE = re.compile(r"^\d{15}$")
SHARED_SOURCE = "shared"

throttle = auth.LoginThrottle()


def bootstrap(conn) -> None:
    db.init(conn)
    login, password = os.environ.get("ADMIN_LOGIN"), os.environ.get("ADMIN_PASSWORD")
    has_admin = conn.execute("SELECT 1 FROM users WHERE role = 'admin'").fetchone()
    if login and password and not has_admin:
        conn.execute(
            "INSERT INTO users (login, password_hash, role, name, created_at) VALUES (?, ?, 'admin', ?, ?)",
            (login, auth.hash_password(password), "Администратор", int(time.time())),
        )
        conn.commit()


@asynccontextmanager
async def lifespan(_app):
    conn = db.connect()
    try:
        bootstrap(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="ALTGEO", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

if os.environ.get("ALTGEO_ENG", "1") != "0":
    # Engineering part (admin only, see eng/web.py require_admin).
    from a2wsgi import WSGIMiddleware

    from eng import feed as eng_feed
    from eng.web import app as eng_app

    app.mount("/eng", WSGIMiddleware(eng_app))
else:
    eng_feed = None


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def current_user(request: Request, conn=Depends(db.get_conn)):
    user = auth.session_user(conn, request.cookies.get(auth.SESSION_COOKIE))
    if user is None:
        raise HTTPException(401, "Нужно войти")
    return user


def require_json(request: Request):
    # State-changing calls only accept JSON: a cross-site HTML form can't
    # send that without a CORS preflight, which this server never allows.
    if request.method in ("POST", "PATCH", "PUT", "DELETE"):
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "Ожидается JSON")


def client_user(user=Depends(current_user), _=Depends(require_json)):
    if user["role"] != "client":
        raise HTTPException(403, "Только для клиентов")
    return user


def admin_user(user=Depends(current_user), _=Depends(require_json)):
    if user["role"] != "admin":
        raise HTTPException(403, "Только для администратора")
    return user


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index(request: Request, conn=Depends(db.get_conn)):
    user = auth.session_user(conn, request.cookies.get(auth.SESSION_COOKIE))
    if user is not None:
        return RedirectResponse("/admin" if user["role"] == "admin" else "/cabinet", 303)
    return FileResponse(STATIC / "index.html")


@app.get("/cabinet", include_in_schema=False)
def cabinet_page(request: Request, conn=Depends(db.get_conn)):
    user = auth.session_user(conn, request.cookies.get(auth.SESSION_COOKIE))
    if user is None or user["role"] != "client":
        return RedirectResponse("/", 303)
    return FileResponse(STATIC / "cabinet.html")


@app.get("/admin", include_in_schema=False)
def admin_page(request: Request, conn=Depends(db.get_conn)):
    user = auth.session_user(conn, request.cookies.get(auth.SESSION_COOKIE))
    if user is None or user["role"] != "admin":
        return RedirectResponse("/", 303)
    return FileResponse(STATIC / "admin.html")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class LoginIn(BaseModel):
    login: str = Field(max_length=100)
    password: str = Field(max_length=200)


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response, conn=Depends(db.get_conn), _=Depends(require_json)):
    ip = request.client.host if request.client else ""
    if throttle.blocked(body.login, ip):
        raise HTTPException(429, "Слишком много попыток, попробуйте через 15 минут")
    user = conn.execute("SELECT * FROM users WHERE login = ?", (body.login.strip(),)).fetchone()
    if user is None or not user["is_active"] or not auth.verify_password(body.password, user["password_hash"]):
        throttle.fail(body.login, ip)
        raise HTTPException(401, "Неверный логин или пароль")
    throttle.reset(body.login, ip)
    token = auth.create_session(conn, user["id"])
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_TTL, httponly=True,
                        samesite="lax", secure=_is_https(request))
    return {"role": user["role"], "redirect": "/admin" if user["role"] == "admin" else "/cabinet"}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, conn=Depends(db.get_conn)):
    auth.drop_session(conn, request.cookies.get(auth.SESSION_COOKIE))
    response.delete_cookie(auth.SESSION_COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return {"login": user["login"], "name": user["name"], "role": user["role"]}


class PasswordIn(BaseModel):
    old_password: str = Field(max_length=200)
    new_password: str = Field(min_length=6, max_length=200)


@app.post("/api/me/password")
def change_password(body: PasswordIn, user=Depends(current_user), conn=Depends(db.get_conn), _=Depends(require_json)):
    if not auth.verify_password(body.old_password, user["password_hash"]):
        raise HTTPException(400, "Текущий пароль указан неверно")
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (auth.hash_password(body.new_password), user["id"]))
    conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Client cabinet. Responses carry only names, times and track positions:
# no WiFi/BLE, no raw logs, no GPS status.
# ---------------------------------------------------------------------------

def _last_seen(conn, source_key):
    r = conn.execute("SELECT MAX(ts) AS t FROM raw_points WHERE source_key = ?", (source_key,)).fetchone()
    return r["t"]


def _device_out(conn, d):
    return {"id": d["id"], "name": d["name"], "kind": d["kind"],
            "imei": d["source_key"][5:] if d["kind"] == "imei" else None,
            "created_at": d["created_at"], "last_seen": _last_seen(conn, d["source_key"])}


def _own_device(conn, user, device_id: int):
    d = conn.execute("SELECT * FROM devices WHERE id = ? AND owner_id = ?", (device_id, user["id"])).fetchone()
    if d is None:
        raise HTTPException(404, "Устройство не найдено")
    return d


class DeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kind: str = Field(pattern="^(imei|sd)$")
    imei: Optional[str] = None


@app.get("/api/my/devices")
def my_devices(user=Depends(client_user), conn=Depends(db.get_conn)):
    rows = conn.execute("SELECT * FROM devices WHERE owner_id = ? ORDER BY name", (user["id"],)).fetchall()
    return [_device_out(conn, d) for d in rows]


@app.post("/api/my/devices", status_code=201)
def add_device(body: DeviceIn, user=Depends(client_user), conn=Depends(db.get_conn)):
    sync_token = None
    if body.kind == "imei":
        imei = (body.imei or "").strip()
        if not IMEI_RE.match(imei):
            raise HTTPException(400, "IMEI должен состоять из 15 цифр")
        source_key, token_hash = f"imei:{imei}", None
    else:
        sync_token = auth.new_sync_token()
        source_key, token_hash = f"sd:{auth.new_sync_token()}", auth.token_hash(sync_token)
    try:
        cur = conn.execute(
            "INSERT INTO devices (owner_id, name, kind, source_key, sync_token_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], body.name.strip(), body.kind, source_key, token_hash, int(time.time())),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Это устройство уже зарегистрировано")
    conn.commit()
    out = _device_out(conn, conn.execute("SELECT * FROM devices WHERE id = ?", (cur.lastrowid,)).fetchone())
    out["sync_password"] = sync_token
    return out


class DeviceRename(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@app.patch("/api/my/devices/{device_id}")
def rename_device(device_id: int, body: DeviceRename, user=Depends(client_user), conn=Depends(db.get_conn)):
    _own_device(conn, user, device_id)
    conn.execute("UPDATE devices SET name = ? WHERE id = ?", (body.name.strip(), device_id))
    conn.commit()
    return {"ok": True}


@app.delete("/api/my/devices/{device_id}")
def delete_device(device_id: int, user=Depends(client_user), conn=Depends(db.get_conn)):
    _own_device(conn, user, device_id)
    conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    conn.commit()
    return {"ok": True}


@app.post("/api/my/devices/{device_id}/sync-password")
def regenerate_sync_password(device_id: int, user=Depends(client_user), conn=Depends(db.get_conn)):
    d = _own_device(conn, user, device_id)
    if d["kind"] != "sd":
        raise HTTPException(400, "Пароль синхронизации нужен только трекеру с SD-картой")
    token = auth.new_sync_token()
    conn.execute("UPDATE devices SET sync_token_hash = ? WHERE id = ?", (auth.token_hash(token), device_id))
    conn.commit()
    return {"sync_password": token}


@app.get("/api/my/live")
def live(user=Depends(client_user), conn=Depends(db.get_conn)):
    out = []
    now = int(time.time())
    for d in conn.execute("SELECT * FROM devices WHERE owner_id = ? ORDER BY name", (user["id"],)):
        last = _last_seen(conn, d["source_key"])
        tail = track.build_track(conn, d["source_key"], last - LIVE_TAIL_S, last) if last else []
        out.append({"id": d["id"], "name": d["name"], "last_seen": last,
                    "online": bool(last and now - last < 10 * 60), "track": tail})
    return out


def _report_range(t_from: int, t_to: int):
    if t_to <= t_from:
        raise HTTPException(400, "Конец периода должен быть позже начала")
    if t_to - t_from > MAX_REPORT_S:
        raise HTTPException(400, "Период отчёта не больше 31 дня")


@app.get("/api/my/devices/{device_id}/track")
def device_track(device_id: int, t_from: int, t_to: int, user=Depends(client_user), conn=Depends(db.get_conn)):
    _report_range(t_from, t_to)
    d = _own_device(conn, user, device_id)
    points = track.build_track(conn, d["source_key"], t_from, t_to)
    return {"device": {"id": d["id"], "name": d["name"]}, "summary": track.summarize(points), "track": points}


@app.get("/api/my/devices/{device_id}/track.gpx")
def device_track_gpx(device_id: int, t_from: int, t_to: int, user=Depends(client_user), conn=Depends(db.get_conn)):
    _report_range(t_from, t_to)
    d = _own_device(conn, user, device_id)
    points = track.build_track(conn, d["source_key"], t_from, t_to)
    return Response(track.to_gpx(d["name"], points), media_type="application/gpx+xml",
                    headers={"Content-Disposition": f'attachment; filename="altgeo_{d["id"]}_{t_from}.gpx"'})


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

class ClientIn(BaseModel):
    login: str = Field(min_length=3, max_length=60, pattern=r"^[A-Za-z0-9_.@-]+$")
    name: str = Field(default="", max_length=100)


class ClientPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=100)
    is_active: Optional[bool] = None


@app.get("/api/admin/clients")
def admin_clients(_admin=Depends(admin_user), conn=Depends(db.get_conn)):
    out = []
    for u in conn.execute("SELECT * FROM users WHERE role = 'client' ORDER BY login"):
        devices = conn.execute("SELECT * FROM devices WHERE owner_id = ? ORDER BY name", (u["id"],)).fetchall()
        out.append({"id": u["id"], "login": u["login"], "name": u["name"], "is_active": bool(u["is_active"]),
                    "created_at": u["created_at"], "devices": [_device_out(conn, d) for d in devices]})
    return out


@app.post("/api/admin/clients", status_code=201)
def admin_create_client(body: ClientIn, _admin=Depends(admin_user), conn=Depends(db.get_conn)):
    password = auth.new_password()
    try:
        cur = conn.execute(
            "INSERT INTO users (login, password_hash, role, name, created_at) VALUES (?, ?, 'client', ?, ?)",
            (body.login, auth.hash_password(password), body.name.strip(), int(time.time())),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Такой логин уже есть")
    conn.commit()
    return {"id": cur.lastrowid, "login": body.login, "password": password}


def _client(conn, client_id):
    u = conn.execute("SELECT * FROM users WHERE id = ? AND role = 'client'", (client_id,)).fetchone()
    if u is None:
        raise HTTPException(404, "Клиент не найден")
    return u


@app.patch("/api/admin/clients/{client_id}")
def admin_update_client(client_id: int, body: ClientPatch, _admin=Depends(admin_user), conn=Depends(db.get_conn)):
    _client(conn, client_id)
    if body.name is not None:
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (body.name.strip(), client_id))
    if body.is_active is not None:
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (int(body.is_active), client_id))
        if not body.is_active:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (client_id,))
    conn.commit()
    return {"ok": True}


@app.post("/api/admin/clients/{client_id}/reset-password")
def admin_reset_password(client_id: int, _admin=Depends(admin_user), conn=Depends(db.get_conn)):
    _client(conn, client_id)
    password = auth.new_password()
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (auth.hash_password(password), client_id))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (client_id,))
    conn.commit()
    return {"password": password}


@app.get("/api/admin/sources")
def admin_sources(_admin=Depends(admin_user), conn=Depends(db.get_conn)):
    """Every data source the server has heard from, with the owner if any."""
    rows = conn.execute(
        """SELECT r.source_key, COUNT(*) AS points, SUM(r.gps_ok) AS gps_points, MAX(r.ts) AS last_seen,
                  d.name AS device_name, u.login AS owner
           FROM raw_points r
           LEFT JOIN devices d ON d.source_key = r.source_key
           LEFT JOIN users u ON u.id = d.owner_id
           GROUP BY r.source_key ORDER BY last_seen DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tracker ingest (protocol fixed by the firmware in wifigps / WIFI_GPS_T-CALL)
# ---------------------------------------------------------------------------

def _shared_password_ok(given: Optional[str]) -> bool:
    shared = os.environ.get("WIFIGPS_PASSWORD", "")
    return bool(shared and given and hmac.compare_digest(given, shared))


def sd_source(request: Request, conn=Depends(db.get_conn)) -> str:
    """SD tracker: its sync_password is either a device's own token or the shared one."""
    given = request.headers.get("x-sync-password")
    if given:
        d = conn.execute("SELECT source_key FROM devices WHERE sync_token_hash = ?", (auth.token_hash(given),)).fetchone()
        if d is not None:
            return d["source_key"]
    if _shared_password_ok(given):
        return SHARED_SOURCE
    raise HTTPException(401, "bad sync password")


class SyncCheckIn(BaseModel):
    filenames: List[str] = Field(default_factory=list, max_length=5000)


@app.post("/api/sync/check")
def sync_check(body: SyncCheckIn, source=Depends(sd_source), conn=Depends(db.get_conn)):
    have = {r["filename"] for r in conn.execute("SELECT filename FROM uploaded_files WHERE source_key = ?", (source,))}
    # The firmware takes the first [...] in the response as the missing list.
    return {"missing": [n for n in body.filenames if ingest.valid_log_filename(n) and n not in have]}


@app.post("/api/upload")
async def upload(request: Request, logfiles: List[UploadFile] = File(...), source=Depends(sd_source),
                 conn=Depends(db.get_conn)):
    stored, raw_files = [], []
    for f in logfiles:
        name = os.path.basename(f.filename or "")
        if not ingest.valid_log_filename(name):
            raise HTTPException(400, f"unexpected file name: {name}")
        data = await f.read()
        raw_files.append((name, data))
        text = data.decode("utf-8", errors="replace")
        points = ingest.parse_log_file(name, text)
        conn.execute("DELETE FROM raw_points WHERE source_key = ? AND file = ?", (source, name))
        ingest.store_points(conn, source, points, file=name)
        conn.execute(
            "INSERT OR REPLACE INTO uploaded_files (source_key, filename, uploaded_at) VALUES (?, ?, ?)",
            (source, name, int(time.time())),
        )
        stored.append({"file": name, "points": len(points)})
    conn.commit()
    if eng_feed and raw_files:
        await run_in_threadpool(eng_feed.log_files, raw_files, request.headers.get("x-device-chip-id"))
    return {"stored": stored}


@app.post("/api/device/points")
async def device_points(request: Request, conn=Depends(db.get_conn)):
    if not _shared_password_ok(request.headers.get("x-sync-password")):
        raise HTTPException(401, "bad sync password")
    imei = (request.headers.get("x-device-imei") or "").strip()
    if not IMEI_RE.match(imei):
        raise HTTPException(400, "bad X-Device-IMEI")
    raw = await request.body()
    try:
        raw_points = ingest.parse_realtime_body(raw)
    except ValueError:
        # Unparseable even after the SSID fix-up: the device would resend this
        # batch forever, so drop it and acknowledge its seqs.
        seqs = [int(s) for s in re.findall(rb'"seq":(\d+)', raw)]
        return {"acked_seqs": seqs}
    now = int(time.time())
    points, unusable = [], []
    for p in raw_points:
        if not isinstance(p, dict):
            continue
        rp = ingest.realtime_point(p, now)
        if rp is None:
            # Ack anything carrying a seq so a malformed point can't block the queue.
            if isinstance(p.get("seq"), int):
                unusable.append(p["seq"])
            continue
        points.append(rp)
    acked = ingest.store_points(conn, f"imei:{imei}", points)
    conn.commit()
    if eng_feed:
        await run_in_threadpool(eng_feed.realtime_points, imei, [p for p in raw_points if isinstance(p, dict)])
    return {"acked_seqs": acked + unusable}


@app.get("/api/admin/track")
def admin_track(source_key: str, t_from: int, t_to: int, _admin=Depends(admin_user), conn=Depends(db.get_conn)):
    _report_range(t_from, t_to)
    points = track.build_track(conn, source_key, t_from, t_to)
    return {"summary": track.summarize(points), "track": points}
