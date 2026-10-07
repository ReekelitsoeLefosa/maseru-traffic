"""SMS + WhatsApp delivery and congestion alerting.

Providers (set in .env):
  console - print the message to the server log (default; no account needed)
  twilio  - SMS and WhatsApp through Twilio's REST API
  meta    - WhatsApp through Meta's WhatsApp Business Cloud API
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import config, db, push, routing
from .analytics import analytics
from .network import INTERSECTIONS

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify")
ALERT_LEVELS = {"HEAVY", "SEVERE"}


def normalize_phone(raw: str) -> str:
    """Return an E.164 number. 8-digit local numbers get Lesotho's +266 prefix."""
    s = re.sub(r"[^\d+]", "", raw or "")
    if s.startswith("00"):
        s = "+" + s[2:]
    if s.startswith("+"):
        digits = s[1:]
    elif len(s) == 8:
        digits = config.DEFAULT_COUNTRY_CODE + s
    else:
        digits = s
    if not digits.isdigit() or not 8 <= len(digits) <= 15:
        raise ValueError(f"Invalid phone number: {raw!r}")
    return "+" + digits


# ---------------------------------------------------------------- providers

def _twilio(to: str, body: str, sender: str) -> str:
    url = f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}/Messages.json"
    r = httpx.post(url, data={"To": to, "From": sender, "Body": body},
                   auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN), timeout=15)
    r.raise_for_status()
    return r.json().get("sid", "")


def _meta_whatsapp(to: str, body: str) -> str:
    url = f"https://graph.facebook.com/v21.0/{config.META_WA_PHONE_NUMBER_ID}/messages"
    payload = {"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "text", "text": {"body": body}}
    r = httpx.post(url, json=payload, headers={"Authorization": f"Bearer {config.META_WA_TOKEN}"}, timeout=15)
    r.raise_for_status()
    return r.json().get("messages", [{}])[0].get("id", "")


def send_sms(phone: str, body: str, intersection_id: str | None = None) -> str:
    try:
        if config.SMS_PROVIDER == "twilio":
            _twilio(phone, body, config.TWILIO_SMS_FROM)
        else:
            print(f"[SMS -> {phone}] {body}")
        status = "sent"
    except Exception as exc:
        status = f"failed: {exc}"
    db.log_alert(phone, f"sms:{config.SMS_PROVIDER}", intersection_id, body, status)
    return status


def send_whatsapp(phone: str, body: str, intersection_id: str | None = None) -> str:
    try:
        if config.WHATSAPP_PROVIDER == "twilio":
            _twilio(f"whatsapp:{phone}", body, config.TWILIO_WHATSAPP_FROM)
        elif config.WHATSAPP_PROVIDER == "meta":
            _meta_whatsapp(phone, body)
        else:
            print(f"[WhatsApp -> {phone}] {body}")
        status = "sent"
    except Exception as exc:
        status = f"failed: {exc}"
    db.log_alert(phone, f"whatsapp:{config.WHATSAPP_PROVIDER}", intersection_id, body, status)
    return status


# ------------------------------------------------------------------ alerts

MIN_SCORE_FOR_LEVEL = {"LOW": 0.0, "MODERATE": 0.4, "HEAVY": 0.7, "SEVERE": 0.9}


def _route_advice(sub: dict, window: dict | None = None) -> dict | None:
    if not (sub.get("origin") and sub.get("destination")) or sub["origin"] == sub["destination"]:
        return None
    scores = analytics.scores()
    if window and window.get("intersection_id") in scores:
        # the junction that triggered the alert must count as at least as congested as the alert says
        iid = window["intersection_id"]
        scores[iid] = max(scores[iid], window.get("score", MIN_SCORE_FOR_LEVEL[window["level"]]))
    try:
        return routing.suggest(sub["origin"], sub["destination"], scores)
    except ValueError:
        return None


def compose(window: dict, sub: dict, whatsapp: bool) -> str:
    name, level = window["name"], window["level"]
    total, pt = window["total"], window["public_transport"]
    advice = _route_advice(sub, window)
    icon = "\U0001F6A6 " if whatsapp else ""
    msg = f"{icon}Maseru Traffic: {level} traffic at {name}. {total} vehicles passed in the last 5 min ({pt} taxis/buses)."
    if advice:
        rec = advice["recommended"]
        via = " > ".join(rec["roads"])
        if advice["reroute_advised"]:
            msg += f" Your trip {advice['from']} to {advice['to']}: use {via} (~{rec['minutes']:.0f} min, saves {advice['minutes_saved']:.0f} min)."
        else:
            msg += (f" Your usual route {via} is still quickest but expect ~{rec['minutes']:.0f} min"
                    f" (normally {rec['free_minutes']:.0f}); detours would take longer.")
    else:
        msg += " Avoid the area if you can."
    return msg + " Reply STOP to opt out."


def _affected(sub: dict, iid: str) -> bool:
    if iid in sub["watch"]:
        return True
    advice = _route_advice(sub)
    return bool(advice and iid in advice["usual"]["nodes"])


def handle_window(window: dict):
    """Listener for closed 5-minute windows: alert subscribers affected by heavy/severe traffic."""
    if window["level"] not in ALERT_LEVELS:
        return
    iid = window["intersection_id"]
    for sub in db.list_subscribers():
        if not _affected(sub, iid):
            continue
        if time.time() - db.last_alert_ts(sub["phone"], iid) < config.ALERT_COOLDOWN_SECONDS:
            continue
        if sub["whatsapp"]:
            _pool.submit(send_whatsapp, sub["phone"], compose(window, sub, True), iid)
        if sub["sms"]:
            _pool.submit(send_sms, sub["phone"], compose(window, sub, False), iid)
    for dev in db.list_devices():
        key = "push:" + dev["token"][-16:]
        if _affected(dev, iid) and time.time() - db.last_alert_ts(key, iid) >= config.ALERT_COOLDOWN_SECONDS:
            _pool.submit(send_push, dev["token"], window, compose(window, dev, False), iid)


def send_push(token: str, window: dict, text: str, intersection_id: str | None) -> str:
    body = text.replace("Maseru Traffic: ", "").replace(" Reply STOP to opt out.", "")
    title = f"{window['level'].title()} traffic: {window['name']}"
    try:
        status = push.send(token, title, body, {"intersection": intersection_id or "", "tab": "map"})
    except push.TokenGone:
        db.delete_device(token)      # app uninstalled: stop trying
        status = "failed: app no longer installed"
    except Exception as exc:
        status = f"failed: {exc}"
    db.log_alert("push:" + token[-16:], f"push:{'fcm' if push.enabled() else 'console'}", intersection_id, f"{title} - {body}", status)
    return status


def send_test(phone: str) -> dict:
    sub = db.get_subscriber(phone) or {"phone": phone, "watch": [], "sms": True, "whatsapp": True}
    snap = analytics.snapshot()
    busiest = max(snap.values(), key=lambda s: s["score"])
    window = {"name": busiest["name"], "level": busiest["level"], "total": busiest["rolling_5min_total"],
              "public_transport": busiest["public_transport"], "intersection_id": busiest["id"]}
    out = {}
    if sub.get("whatsapp", True):
        out["whatsapp"] = send_whatsapp(phone, "[TEST] " + compose(window, sub, True), None)
    if sub.get("sms", True):
        out["sms"] = send_sms(phone, "[TEST] " + compose(window, sub, False), None)
    return out


analytics.on_window_closed(handle_window)


def intersection_names() -> list[str]:
    return [v["name"] for v in INTERSECTIONS.values()]
