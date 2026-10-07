"""Maseru Smart Traffic - API server.

Run:  uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000
"""
import asyncio
import hmac
import json
import secrets
import socket
import time
from contextlib import asynccontextmanager
from xml.sax.saxutils import escape

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import bot, config, db, notifier, routing, signal_timing
from .analytics import CATEGORY_LABELS, analytics
from .network import INTERSECTIONS, ROADS

cameras: dict = {}      # intersection_id -> CameraWorker (cameras attached to this machine)
remote_cams: dict = {}  # intersection_id -> {"status", "jpeg", "last"} sent by camera stations (cloud server)
REMOTE_TIMEOUT = 30     # seconds without news before a station's camera shows as offline
simulator = None
uplink = None


def _save_cameras():
    data = [dict(w.cfg, lines=w.lines) for w in cameras.values()]
    path = config.ROOT / "cameras.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global simulator
    for cfg in config.load_cameras():
        if cfg["intersection_id"] not in INTERSECTIONS:
            print(f"[startup] unknown intersection in cameras.json: {cfg['intersection_id']}")
            continue
        try:
            from .detector import CameraWorker
        except ImportError as exc:
            print(f"[startup] camera disabled, AI packages missing ({exc}). "
                  "Run: pip install opencv-python-headless ultralytics lap")
            break
        w = CameraWorker(cfg)
        cameras[cfg["intersection_id"]] = w
        w.start()
    if config.SIMULATE_OTHER_INTERSECTIONS:
        from .simulator import Simulator
        simulator = Simulator([i for i in INTERSECTIONS if i not in cameras])
        simulator.start()
    if config.CLOUD_URL:       # this machine is a camera station: send measurements to the cloud server
        global uplink
        from .uplink import Uplink
        uplink = Uplink(cameras)
        uplink.start()
        print(f"[startup] sending measurements to {config.CLOUD_URL}")

    async def ticker():
        last_purge = 0.0
        while True:
            await asyncio.to_thread(analytics.tick)
            if time.time() - last_purge > 3600:
                last_purge = time.time()
                await asyncio.to_thread(db.purge_old_alerts, config.ALERT_LOG_DAYS)
            await asyncio.sleep(1)

    task = asyncio.create_task(ticker())
    yield
    task.cancel()
    for w in cameras.values():
        w.stop()
    if simulator:
        simulator.running = False


app = FastAPI(title="Maseru Smart Traffic", lifespan=lifespan)


@app.middleware("http")
async def no_stale_frontend(request: Request, call_next):
    """Make browsers re-check the app's files, so users get updates without a hard refresh."""
    response = await call_next(request)
    if not request.url.path.startswith(("/api/", "/webhooks/", "/ws", "/healthz")):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ------------------------------------------------------------------ access

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
PROXY_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "x-real-ip", "forwarded")


def is_admin(request: Request) -> bool:
    """The Windows app on the server PC is admin; so is anyone holding ADMIN_TOKEN.
    Requests relayed by a tunnel/proxy also arrive from 127.0.0.1, so those count as remote."""
    token = request.headers.get("x-admin-token", "")
    if config.ADMIN_TOKEN and token and hmac.compare_digest(token, config.ADMIN_TOKEN):
        return True
    via_proxy = any(h in request.headers for h in PROXY_HEADERS)
    return bool(request.client) and request.client.host in LOCAL_HOSTS and not via_proxy


def require_admin(request: Request):
    if not is_admin(request):
        raise HTTPException(403, "Admin only")


def lan_urls() -> list[str]:
    """Addresses other devices on the same Wi-Fi can open."""
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))     # no packet is sent; this just picks the outgoing interface
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        ips |= {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        pass
    return [f"http://{ip}:{config.PORT}" for ip in sorted(ips) if not ip.startswith("127.")]


def _full_state() -> dict:
    snap = analytics.snapshot()
    for iid, s in snap.items():
        queue_ratio = s["occupancy"] / INTERSECTIONS[iid]["jam_occ"]
        s["signal"] = signal_timing.compute(s["by_approach_category"], config.WINDOW_SECONDS, queue_ratio)
        if iid in cameras:
            s["camera"] = cameras[iid].status
        elif iid in remote_cams:
            r = remote_cams[iid]
            online = time.time() - r["last"] < REMOTE_TIMEOUT
            s["camera"] = {**r["status"], "remote": True} if online else \
                {"state": "offline", "fps": 0, "error": "Camera station not connected", "model": "", "remote": True}
    return snap


def camera_ids() -> list[str]:
    return list(cameras) + [i for i in remote_cams if i not in cameras]


def current_jpeg(iid: str) -> bytes | None:
    if iid in cameras:
        return cameras[iid].jpeg
    r = remote_cams.get(iid)
    return r["jpeg"] if r else None


# ----------------------------------------------------------------- traffic

@app.get("/healthz")
def healthz():
    """Tiny check for Render and uptime monitors (UptimeRobot pings keep the free plan awake)."""
    return {"ok": True, "cameras_online": sum(1 for r in remote_cams.values() if time.time() - r["last"] < REMOTE_TIMEOUT)}


@app.get("/api/meta")
def meta(request: Request):
    admin = is_admin(request)
    return {
        "admin": admin,
        "share": {"public_url": config.public_url(), "lan_urls": lan_urls()} if admin else None,
        "categories": CATEGORY_LABELS,
        "window_seconds": config.WINDOW_SECONDS,
        "places": [{"id": k, "name": v["name"], "lat": v["lat"], "lon": v["lon"]} for k, v in INTERSECTIONS.items()],
        "roads": [{k: e[k] for k in ("from", "to", "road", "km", "geometry")} for e in ROADS],
        "cameras": camera_ids(),
        "local_cameras": list(cameras),     # counting lines can only be edited where the camera is attached
        "providers": {"sms": config.SMS_PROVIDER, "whatsapp": config.WHATSAPP_PROVIDER},
        "uplink": uplink.status if (admin and uplink) else None,
    }


@app.get("/api/intersections")
def intersections():
    return _full_state()


@app.get("/api/intersections/{iid}/history")
def history(iid: str, limit: int = 24):
    if iid not in INTERSECTIONS:
        raise HTTPException(404, "Unknown intersection")
    return db.recent_windows(iid, limit)


@app.get("/api/route")
def route(src: str, dst: str):
    try:
        return routing.suggest(src, dst, analytics.scores())
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class Incident(BaseModel):
    intersection_id: str
    minutes: float = 15


@app.post("/api/simulate/incident", dependencies=[Depends(require_admin)])
def incident(body: Incident):
    """Demo helper: force congestion at a simulated intersection (e.g. to test alerts)."""
    if not simulator or body.intersection_id not in simulator.ids:
        raise HTTPException(400, "Only simulated intersections can have a demo incident")
    simulator.add_incident(body.intersection_id, body.minutes)
    return {"ok": True}


# ----------------------------------------------------------------- cameras

@app.get("/api/cameras/{iid}/stream")
async def stream(iid: str):
    if iid not in camera_ids():
        raise HTTPException(404, "No camera at this intersection")

    async def frames():
        last = None
        while True:
            jpeg = current_jpeg(iid)
            if jpeg is not None and jpeg is not last:
                last = jpeg
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + last + b"\r\n"
            await asyncio.sleep(0.05)

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/cameras/{iid}/snapshot.jpg")
def snapshot(iid: str):
    jpeg = current_jpeg(iid)
    if jpeg is None:
        raise HTTPException(404, "No frame yet")
    return Response(jpeg, media_type="image/jpeg")


# ------------------------------------------- camera stations -> cloud server

def require_station(request: Request):
    key = request.headers.get("x-station-key", "")
    if not (config.STATION_KEY and key and hmac.compare_digest(key, config.STATION_KEY)):
        raise HTTPException(403, "Unknown camera station")


class Ingest(BaseModel):
    events: list[dict] = Field(default_factory=list, max_length=5000)
    cameras: dict[str, dict] = Field(default_factory=dict)


def _station_camera(iid: str) -> dict:
    if iid not in remote_cams:
        remote_cams[iid] = {"status": {}, "jpeg": None, "last": 0.0}
        analytics.set_source(iid, "camera")
        if simulator:
            simulator.stop_simulating(iid)
    return remote_cams[iid]


@app.post("/api/ingest", dependencies=[Depends(require_station)])
def ingest(body: Ingest):
    """Measurements from a camera station (the PC running the AI at a junction)."""
    from .analytics import CATEGORIES
    now = time.time()
    for iid, status in body.cameras.items():
        if iid in INTERSECTIONS:
            cam = _station_camera(iid)
            cam["status"] = {k: status.get(k) for k in ("state", "fps", "error", "model")}
            cam["last"] = now
    accepted = 0
    for e in body.events:
        iid = e.get("iid")
        if iid not in INTERSECTIONS:
            continue
        ts = min(float(e.get("ts") or now), now)
        _station_camera(iid)["last"] = now
        if e.get("type") == "crossing" and e.get("category") in CATEGORIES:
            analytics.record_crossing(iid, e["category"], str(e.get("approach", "main"))[:20],
                                      "out" if e.get("direction") == "out" else "in", ts=ts)
            accepted += 1
        elif e.get("type") == "occupancy":
            analytics.record_occupancy(iid, max(0.0, float(e.get("value", 0))), ts=ts)
            accepted += 1
    return {"accepted": accepted}


@app.post("/api/ingest/frame/{iid}", dependencies=[Depends(require_station)])
async def ingest_frame(iid: str, request: Request):
    if iid not in INTERSECTIONS:
        raise HTTPException(404, "Unknown intersection")
    data = await request.body()
    if not data.startswith(b"\xff\xd8") or len(data) > 1_000_000:
        raise HTTPException(400, "Expected a JPEG under 1 MB")
    cam = _station_camera(iid)
    cam["jpeg"], cam["last"] = data, time.time()
    return {"ok": True}


class Line(BaseModel):
    approach: str = Field(..., max_length=20)
    points: list[float] = Field(..., min_length=4, max_length=4)   # normalised x1,y1,x2,y2
    count_direction: str = "both"


@app.get("/api/cameras/{iid}/lines")
def get_lines(iid: str):
    if iid not in cameras:
        raise HTTPException(404, "No camera at this intersection")
    return cameras[iid].lines


@app.put("/api/cameras/{iid}/lines", dependencies=[Depends(require_admin)])
def set_lines(iid: str, lines: list[Line]):
    if iid not in cameras:
        raise HTTPException(404, "No camera at this intersection")
    cameras[iid].set_lines([l.model_dump() for l in lines])
    _save_cameras()
    return cameras[iid].lines


# ------------------------------------------------------------ subscribers

class Subscriber(BaseModel):
    name: str | None = None
    phone: str
    sms: bool = True
    whatsapp: bool = True
    watch: list[str] = []
    origin: str | None = None
    destination: str | None = None


@app.post("/api/subscribers")
def subscribe(body: Subscriber, request: Request):
    """Admins subscribe numbers directly. Anyone else gets a 6-digit code on that phone first,
    so nobody can sign up someone else's number for alerts."""
    try:
        phone = notifier.normalize_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    bad = [w for w in body.watch + [body.origin, body.destination] if w and w not in INTERSECTIONS]
    if bad:
        raise HTTPException(400, f"Unknown places: {bad}")
    if not (body.sms or body.whatsapp):
        raise HTTPException(400, "Choose SMS, WhatsApp or both")
    data = body.model_dump() | {"phone": phone}
    if is_admin(request):
        return {"status": "subscribed", "subscriber": db.upsert_subscriber(**data)}
    pending = db.get_pending(phone)
    if pending and time.time() - pending["sent"] < 60:
        raise HTTPException(429, "A code was just sent. Wait a minute before asking for a new one.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.save_pending(phone, data, code)
    text = f"Maseru Traffic code: {code}. Enter it in the app to confirm traffic alerts. Valid 10 minutes."
    status = notifier.send_whatsapp(phone, text) if body.whatsapp else notifier.send_sms(phone, text)
    if status != "sent":
        db.delete_pending(phone)
        raise HTTPException(502, "Could not send the code. Check the number and try again.")
    return {"status": "code_sent", "phone": phone, "channel": "WhatsApp" if body.whatsapp else "SMS"}


class Verify(BaseModel):
    phone: str
    code: str = Field(..., min_length=6, max_length=6)


@app.post("/api/subscribers/verify")
def verify(body: Verify):
    try:
        phone = notifier.normalize_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    p = db.get_pending(phone)
    if not p or p["expires"] < time.time() or p["attempts"] >= 5:
        db.delete_pending(phone)
        raise HTTPException(400, "This code has expired. Subscribe again to get a new one.")
    if not hmac.compare_digest(p["code"], body.code.strip()):
        db.count_failed_attempt(phone)
        raise HTTPException(400, "Wrong code. Check the message and try again.")
    db.delete_pending(phone)
    return {"status": "subscribed", "subscriber": db.upsert_subscriber(**p["data"])}


class Device(BaseModel):
    token: str = Field(..., min_length=20, max_length=4096)
    platform: str = Field("android", pattern="^(android|ios|web)$")
    watch: list[str] = []
    origin: str | None = None
    destination: str | None = None


@app.post("/api/devices")
def register_device(body: Device):
    """The phone app registers its push token with the junctions/trip it wants alerts for.
    The token itself proves the request comes from that app install, so no code is needed."""
    bad = [w for w in body.watch + [body.origin, body.destination] if w and w not in INTERSECTIONS]
    if bad:
        raise HTTPException(400, f"Unknown places: {bad}")
    if not (body.watch or (body.origin and body.destination)):
        raise HTTPException(400, "Pick a daily trip or at least one junction to watch")
    d = db.upsert_device(body.token, body.platform, body.watch, body.origin, body.destination)
    return {k: d[k] for k in ("platform", "watch", "origin", "destination")}


class DeviceToken(BaseModel):
    token: str = Field(..., min_length=20, max_length=4096)


@app.post("/api/devices/remove")
def remove_device(body: DeviceToken):
    db.delete_device(body.token)
    return {"ok": True}


@app.get("/api/subscribers", dependencies=[Depends(require_admin)])
def subscribers():
    return db.list_subscribers(active_only=False)


@app.delete("/api/subscribers/{sub_id}", dependencies=[Depends(require_admin)])
def remove_subscriber(sub_id: int):
    db.delete_subscriber(sub_id)
    return {"ok": True}


class TestMsg(BaseModel):
    phone: str


@app.post("/api/notify/test", dependencies=[Depends(require_admin)])
def notify_test(body: TestMsg):
    try:
        phone = notifier.normalize_phone(body.phone)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return notifier.send_test(phone)


@app.get("/api/alerts", dependencies=[Depends(require_admin)])
def alerts(limit: int = 50):
    return db.recent_alerts(limit)


# --------------------------------------------------------------- WhatsApp

class ChatIn(BaseModel):
    phone: str = "+26650000000"
    text: str


@app.post("/api/bot/chat")
def bot_chat(body: ChatIn, request: Request):
    """Try the WhatsApp bot from the dashboard without a WhatsApp account."""
    return {"reply": bot.handle(notifier.normalize_phone(body.phone), body.text, can_subscribe=is_admin(request))}


@app.post("/webhooks/twilio/whatsapp")
async def twilio_whatsapp(request: Request):
    form = await request.form()
    phone = str(form.get("From", "")).replace("whatsapp:", "")
    reply = bot.handle(phone, str(form.get("Body", "")))
    db.log_alert(phone, "whatsapp:bot-in", None, str(form.get("Body", "")), "received")
    twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escape(reply)}</Message></Response>'
    return Response(twiml, media_type="application/xml")


@app.get("/webhooks/meta/whatsapp")
def meta_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == config.META_WA_VERIFY_TOKEN:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "Verification failed")


@app.post("/webhooks/meta/whatsapp")
async def meta_incoming(request: Request):
    data = await request.json()
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if msg.get("type") != "text":
                    continue
                phone = "+" + msg["from"]
                text = msg["text"]["body"]
                db.log_alert(phone, "whatsapp:bot-in", None, text, "received")
                reply = bot.handle(phone, text)
                await asyncio.to_thread(notifier.send_whatsapp, phone, reply)
    return {"ok": True}


# -------------------------------------------------------------- live feed

@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(await asyncio.to_thread(_full_state))
            await asyncio.sleep(1)
    except (WebSocketDisconnect, RuntimeError):
        pass


app.mount("/", StaticFiles(directory=config.FRONTEND_DIR, html=True), name="frontend")
