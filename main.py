from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from datetime import datetime

app = FastAPI()

BASE = Path(__file__).parent
STATIC = BASE / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

latest = {
    "lat": None,
    "lng": None,
    "acc": None,
    "speed": None,
    "driver": None,
    "time": None,
    "ts": None,
}

dashboards: set[WebSocket] = set()


@app.get("/")
async def dashboard_page():
    return HTMLResponse((STATIC / "dashboard.html").read_text(encoding="utf-8"))


@app.get("/driver")
async def driver_page():
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


@app.post("/update")
async def update_location(req: Request):
    data = await req.json()
    latest.update({
        "lat": data.get("lat"),
        "lng": data.get("lng"),
        "acc": data.get("acc"),
        "speed": data.get("speed"),
        "driver": data.get("driver") or "پیک",
        "time": datetime.now().strftime("%H:%M:%S"),
        "ts": datetime.now().timestamp(),
    })

    dead = []
    for ws in dashboards:
        try:
            await ws.send_json(latest)
        except Exception:
            dead.append(ws)
    for ws in dead:
        dashboards.discard(ws)

    return {"ok": True}


@app.get("/api/latest")
async def get_latest():
    return JSONResponse(latest)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    dashboards.add(ws)
    try:
        await ws.send_json(latest)
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        dashboards.discard(ws)