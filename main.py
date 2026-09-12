from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from datetime import datetime, timezone
import sqlite3
import secrets
import string
import asyncio
import urllib.request
import urllib.parse
import json

app = FastAPI()

BASE = Path(__file__).parent
STATIC = BASE / "static"
DB_PATH = BASE / "peyk.db"

app.mount("/static", StaticFiles(directory=STATIC), name="static")

dashboards: set[WebSocket] = set()
_address_cache = {}


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    conn = db_connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS drivers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            acc INTEGER,
            speed INTEGER,
            ts TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


db_init()


def gen_token(length=8):
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def now_utc():
    return datetime.now(timezone.utc).isoformat()


# ============ Reverse Geocoding ============
def _fetch_address_sync(lat, lng):
    try:
        params = urllib.parse.urlencode({
            "format": "json",
            "lat": lat,
            "lon": lng,
            "accept-language": "fa",
            "zoom": 18,
        })
        url = "https://nominatim.openstreetmap.org/reverse?" + params
        req = urllib.request.Request(url, headers={
            "User-Agent": "DeliveryTracker/1.0"
        })
        with urllib.request.urlopen(req, timeout=6) as res:
            data = json.loads(res.read().decode("utf-8"))
            return data.get("display_name", "") or ""
    except Exception:
        return ""


async def get_address(lat, lng):
    lat_r = round(lat, 4)
    lng_r = round(lng, 4)
    key = (lat_r, lng_r)
    now_ts = datetime.now().timestamp()

    if key in _address_cache:
        addr, cached_at = _address_cache[key]
        if now_ts - cached_at < 86400:
            return addr

    addr = await asyncio.to_thread(_fetch_address_sync, lat, lng)
    _address_cache[key] = (addr, now_ts)
    return addr


@app.get("/api/address")
async def address_endpoint(lat: float, lng: float):
    addr = await get_address(lat, lng)
    return {"address": addr}


# ============ Pages ============
@app.get("/")
async def dashboard_page():
    return HTMLResponse((STATIC / "dashboard.html").read_text(encoding="utf-8"))


@app.get("/driver/{token}")
async def driver_page(token: str):
    conn = db_connect()
    row = conn.execute("SELECT * FROM drivers WHERE token = ?", (token,)).fetchone()
    conn.close()
    if not row:
        return HTMLResponse(
            "<h1 style='font-family:Tahoma;text-align:center;padding:50px;'>"
            "❌ لینک نامعتبر<br><br>"
            "<span style='color:#888;font-size:14px;'>این لینک اشتباه است یا حذف شده.</span>"
            "</h1>",
            status_code=404,
        )
    return HTMLResponse((STATIC / "driver.html").read_text(encoding="utf-8"))


@app.get("/manifest.json")
async def manifest():
    return FileResponse(STATIC / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        STATIC / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


# ============ Drivers API ============
@app.post("/api/drivers")
async def add_driver(req: Request):
    data = await req.json()
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()

    if not name or not phone:
        raise HTTPException(400, "name and phone required")

    token = gen_token()
    conn = db_connect()
    while conn.execute("SELECT 1 FROM drivers WHERE token = ?", (token,)).fetchone():
        token = gen_token()

    conn.execute(
        "INSERT INTO drivers (name, phone, token, created_at) VALUES (?, ?, ?, ?)",
        (name, phone, token, now_utc()),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "token": token, "name": name, "phone": phone}


@app.get("/api/drivers")
async def list_drivers():
    conn = db_connect()
    rows = conn.execute("SELECT id, name, phone, token, created_at FROM drivers ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.delete("/api/drivers/{token}")
async def delete_driver(token: str):
    conn = db_connect()
    conn.execute("DELETE FROM drivers WHERE token = ?", (token,))
    conn.execute("DELETE FROM locations WHERE token = ?", (token,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ============ Location ============
@app.post("/api/location/{token}")
async def update_location(token: str, req: Request):
    conn = db_connect()
    driver = conn.execute("SELECT name FROM drivers WHERE token = ?", (token,)).fetchone()
    if not driver:
        conn.close()
        raise HTTPException(404, "invalid token")

    data = await req.json()
    lat = data.get("lat")
    lng = data.get("lng")
    acc = data.get("acc")
    speed = data.get("speed")

    if lat is None or lng is None:
        conn.close()
        raise HTTPException(400, "lat/lng required")

    ts = now_utc()
    conn.execute(
        "INSERT INTO locations (token, lat, lng, acc, speed, ts) VALUES (?, ?, ?, ?, ?, ?)",
        (token, lat, lng, acc, speed, ts),
    )
    conn.commit()

    last = conn.execute(
        "SELECT lat, lng, acc, speed, ts FROM locations WHERE token = ? ORDER BY id DESC LIMIT 1",
        (token,),
    ).fetchone()
    conn.close()

    payload = {
        "token": token,
        "name": driver["name"],
        "lat": last["lat"],
        "lng": last["lng"],
        "acc": last["acc"],
        "speed": last["speed"],
        "ts": last["ts"],
    }

    dead = []
    for ws in dashboards:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        dashboards.discard(ws)

    return {"ok": True}


@app.get("/api/positions")
async def get_all_positions():
    conn = db_connect()
    rows = conn.execute("""
        SELECT d.token, d.name, d.phone,
               l.lat, l.lng, l.acc, l.speed, l.ts
        FROM drivers d
        LEFT JOIN locations l ON l.id = (
            SELECT id FROM locations WHERE token = d.token ORDER BY id DESC LIMIT 1
        )
        ORDER BY d.id DESC
    """).fetchall()
    conn.close()

    result = []
    for r in rows:
        result.append({
            "token": r["token"],
            "name": r["name"],
            "phone": r["phone"],
            "lat": r["lat"],
            "lng": r["lng"],
            "acc": r["acc"],
            "speed": r["speed"],
            "ts": r["ts"],
        })
    return result


# ============ WebSocket ============
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    dashboards.add(ws)
    try:
        positions = await get_all_positions()
        for p in positions:
            if p["lat"] is not None:
                await ws.send_json(p)
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        dashboards.discard(ws)
