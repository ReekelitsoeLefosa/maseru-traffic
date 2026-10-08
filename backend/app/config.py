"""Central configuration, loaded from environment variables / .env file."""
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Windows consoles default to cp1252, which can't print the emoji used in WhatsApp messages.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "traffic.db"))
# PostgreSQL connection string (e.g. a free Neon database). Empty = use the SQLite file above.
DATABASE_URL = os.getenv("DATABASE_URL", "")
FRONTEND_DIR = ROOT / "frontend"

# --- Detection -------------------------------------------------------------
YOLO_MODEL = os.getenv("YOLO_MODEL", "yolov8n.pt")   # or path to a custom Maseru-trained model
if (ROOT / YOLO_MODEL).exists():   # relative paths are relative to the project, not the launch directory
    YOLO_MODEL = str(ROOT / YOLO_MODEL)
DETECT_CONF = float(os.getenv("DETECT_CONF", "0.2"))
DETECT_IMGSZ = int(os.getenv("DETECT_IMGSZ", "960"))
# Process every Nth frame (CPU-friendly). 1 = every frame.
FRAME_STRIDE = int(os.getenv("FRAME_STRIDE", "2"))
# Heuristic to split "car" detections into minibus taxis when using the stock COCO model.
MINIBUS_HEURISTIC = _bool("MINIBUS_HEURISTIC", True)

# --- Analytics -------------------------------------------------------------
WINDOW_SECONDS = int(os.getenv("WINDOW_SECONDS", "300"))  # 5-minute counting window
# Simulate traffic for intersections that have no camera attached (so map/routing has data).
SIMULATE_OTHER_INTERSECTIONS = _bool("SIMULATE_OTHER_INTERSECTIONS", True)

# --- Notifications ---------------------------------------------------------
# SMS_PROVIDER / WHATSAPP_PROVIDER: "console" (log only), "twilio", or "meta" (WhatsApp only)
SMS_PROVIDER = (os.getenv("SMS_PROVIDER") or "console").strip().lower()   # console | textbee | twilio
# textbee (https://textbee.dev): your own Android phone sends the SMS from its SIM - free plan 50/day, 300/month
TEXTBEE_API_KEY = os.getenv("TEXTBEE_API_KEY", "")
TEXTBEE_DEVICE_ID = os.getenv("TEXTBEE_DEVICE_ID", "")           # optional: empty = your default phone
TEXTBEE_WEBHOOK_SECRET = os.getenv("TEXTBEE_WEBHOOK_SECRET", "")  # signs incoming SMS (STOP replies)
# Never send more SMS than this per day (textbee free plan allows 50). 0 = no limit.
SMS_DAILY_LIMIT = int(os.getenv("SMS_DAILY_LIMIT") or "45")
WHATSAPP_PROVIDER = (os.getenv("WHATSAPP_PROVIDER") or "console").strip().lower()
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_SMS_FROM = os.getenv("TWILIO_SMS_FROM", "")            # e.g. +1XXXXXXXXXX or alphanumeric sender id
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")  # e.g. whatsapp:+14155238886 (sandbox)
META_WA_TOKEN = os.getenv("META_WA_TOKEN", "")
META_WA_PHONE_NUMBER_ID = os.getenv("META_WA_PHONE_NUMBER_ID", "")
META_WA_VERIFY_TOKEN = os.getenv("META_WA_VERIFY_TOKEN", "maseru-traffic-verify")
# App secret (Meta app dashboard -> App settings -> Basic): proves incoming webhooks really come from Meta.
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
# Names of the message templates you created and Meta approved (WhatsApp Manager -> Message templates).
META_WA_ALERT_TEMPLATE = os.getenv("META_WA_ALERT_TEMPLATE", "traffic_alert")
META_WA_CODE_TEMPLATE = os.getenv("META_WA_CODE_TEMPLATE", "verification_code")
META_WA_TEMPLATE_LANG = os.getenv("META_WA_TEMPLATE_LANG", "en")
# Don't alert the same user about the same intersection more often than this.
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "900"))
DEFAULT_COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "266")  # Lesotho
ALERT_LOG_DAYS = int(os.getenv("ALERT_LOG_DAYS", "90"))   # must match the privacy policy


# --- App / access ----------------------------------------------------------
PORT = int(os.getenv("PORT", "8000"))
# Admin = the Windows app on the server PC itself, or anyone sending this key (header X-Admin-Token or
# ?admin=KEY once in the browser). Admins see subscribers and the message log, move counting lines, etc.
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
# https address phones use (e.g. a Cloudflare tunnel). The Windows app fills data/public_url.txt itself.
PUBLIC_URL = os.getenv("PUBLIC_URL", "")
PUBLIC_URL_FILE = DATA_DIR / "public_url.txt"


# --- Push notifications (Android/iOS app) ----------------------------------
_fcm = os.getenv("FCM_CREDENTIALS", "")
FCM_CREDENTIALS = Path(_fcm) if _fcm else None
# Alternative for hosts without a disk (Render free plan): paste the whole key JSON into this variable.
FCM_CREDENTIALS_JSON = os.getenv("FCM_CREDENTIALS_JSON", "")

# --- Cloud server <-> camera station ---------------------------------------
# On the camera station (the PC running the AI): where to send measurements.
CLOUD_URL = os.getenv("CLOUD_URL", "")
# Shared secret between stations and the cloud server (set the same value on both).
STATION_KEY = os.getenv("STATION_KEY", "")
FRAME_UPLOAD_SECONDS = float(os.getenv("FRAME_UPLOAD_SECONDS", "3"))   # how often to send a video snapshot
FRAME_UPLOAD_WIDTH = int(os.getenv("FRAME_UPLOAD_WIDTH", "640"))


def public_url() -> str:
    if PUBLIC_URL:
        return PUBLIC_URL
    try:
        return PUBLIC_URL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def load_cameras() -> list[dict]:
    """cameras.json maps camera feeds (video file, RTSP URL or webcam index) to intersections."""
    path = ROOT / os.getenv("CAMERAS_FILE", "cameras.json")
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)
