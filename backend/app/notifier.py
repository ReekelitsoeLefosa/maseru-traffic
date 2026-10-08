"""SMS + WhatsApp + push delivery and congestion alerting.

Providers (set in .env / Render environment):
  console - print the message to the server log (default; no account needed)
  textbee - SMS from your own Android phone's SIM (free textbee plan: 50/day) - cheapest SMS option
  twilio  - SMS (and WhatsApp sandbox for testing) through Twilio's REST API
  meta    - WhatsApp through Meta's WhatsApp Business Cloud API (recommended: cheapest)

Costs shape the design:
* SMS to Lesotho is billed per 160-character part, so SMS alerts are always ONE part, plain characters.
* WhatsApp lets a business message someone first only with a pre-approved template (a few cents);
  normal text is free within 24 h after the person last messaged the bot. We use free text when we can.
"""
import re
import time
import unicodedata
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


def _textbee(to: str, body: str) -> str:
    """Send through your own Android phone (textbee app) - normal local SMS prices / your SMS bundle."""
    payload = {"recipients": [to], "message": body}
    if config.TEXTBEE_DEVICE_ID:
        payload["deviceId"] = config.TEXTBEE_DEVICE_ID
    r = httpx.post("https://api.textbee.dev/api/v1/gateway/send-sms", json=payload, timeout=20,
                   headers={"x-api-key": config.TEXTBEE_API_KEY})
    if r.status_code >= 400:
        raise RuntimeError(f"textbee {r.status_code}: {r.text[:300]}")
    return r.json().get("data", {}).get("smsBatchId", "")


def _meta_post(payload: dict) -> str:
    url = f"https://graph.facebook.com/v21.0/{config.META_WA_PHONE_NUMBER_ID}/messages"
    r = httpx.post(url, json={"messaging_product": "whatsapp", **payload},
                   headers={"Authorization": f"Bearer {config.META_WA_TOKEN}"}, timeout=15)
    if r.status_code >= 400:   # Meta explains what's wrong (template not approved, number not allowed, ...)
        raise RuntimeError(f"Meta {r.status_code}: {r.text[:300]}")
    return r.json().get("messages", [{}])[0].get("id", "")


def _meta_whatsapp(to: str, body: str) -> str:
    return _meta_post({"to": to.lstrip("+"), "type": "text", "text": {"body": body}})


def _meta_template(to: str, template: dict) -> str:
    """template = {"name": ..., "params": [...], "code": optional one-time code for the copy button}"""
    components = [{"type": "body", "parameters": [{"type": "text", "text": _template_param(p)}
                                                  for p in template["params"]]}]
    if template.get("code"):
        components.append({"type": "button", "sub_type": "url", "index": "0",
                           "parameters": [{"type": "text", "text": template["code"]}]})
    return _meta_post({"to": to.lstrip("+"), "type": "template",
                       "template": {"name": template["name"], "language": {"code": config.META_WA_TEMPLATE_LANG},
                                    "components": components}})


def _template_param(text: str) -> str:
    # WhatsApp rejects template values with line breaks, tabs or more than 4 spaces in a row
    return re.sub(r"\s+", " ", str(text)).strip()[:1000]


def in_whatsapp_window(phone: str) -> bool:
    """True if the person messaged our bot in the last 24 h, so free normal messages are allowed."""
    return time.time() - db.last_inbound_ts(phone) < 24 * 3600 - 120


def gsm_text(text: str) -> str:
    """Plain characters only: one emoji or accent switches an SMS to a format that holds 70 characters, not 160."""
    text = text.replace("→", ">").replace("–", "-").replace("—", "-").replace("~", "about ")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[\[\]{}\\^|`]", "", text)


def _sms_quota_left() -> bool:
    if config.SMS_PROVIDER == "console" or config.SMS_DAILY_LIMIT <= 0:
        return True
    midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    return db.sms_sent_since(midnight) < config.SMS_DAILY_LIMIT


def send_sms(phone: str, body: str, intersection_id: str | None = None) -> str:
    body = gsm_text(body)
    try:
        if not _sms_quota_left():
            status = f"skipped: daily SMS limit ({config.SMS_DAILY_LIMIT}) reached"
        else:
            if config.SMS_PROVIDER == "textbee":
                _textbee(phone, body)
            elif config.SMS_PROVIDER == "twilio":
                _twilio(phone, body, config.TWILIO_SMS_FROM)
            else:
                print(f"[SMS -> {phone}] {body}")
            status = "sent"
    except Exception as exc:
        status = f"failed: {exc}"
    db.log_alert(phone, f"sms:{config.SMS_PROVIDER}", intersection_id, body, status)
    return status


def send_whatsapp(phone: str, body: str, intersection_id: str | None = None, template: dict | None = None) -> str:
    """Free normal message inside the 24-hour window; otherwise the approved template (Meta)."""
    channel = f"whatsapp:{config.WHATSAPP_PROVIDER}"
    try:
        if config.WHATSAPP_PROVIDER == "twilio":
            _twilio(f"whatsapp:{phone}", body, config.TWILIO_WHATSAPP_FROM)
        elif config.WHATSAPP_PROVIDER == "meta":
            if template and template.get("name") and not in_whatsapp_window(phone):
                _meta_template(phone, template)
                channel += ":template"
            else:
                _meta_whatsapp(phone, body)
        else:
            print(f"[WhatsApp -> {phone}] {body}")
        status = "sent"
    except Exception as exc:
        status = f"failed: {exc}"
    db.log_alert(phone, channel, intersection_id, body, status)
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


def advice_text(window: dict, sub: dict) -> str:
    """The personal part of an alert: what to do about it."""
    advice = _route_advice(sub, window)
    if not advice:
        return "Avoid the area if you can."
    rec = advice["recommended"]
    via = " > ".join(rec["roads"])
    if advice["reroute_advised"]:
        return (f"Your trip {advice['from']} to {advice['to']}: use {via} "
                f"(about {rec['minutes']:.0f} min, saves {advice['minutes_saved']:.0f} min).")
    return (f"Your usual route {via} is still quickest but expect about {rec['minutes']:.0f} min "
            f"(normally {rec['free_minutes']:.0f}); detours would take longer.")


def compose(window: dict, sub: dict, whatsapp: bool) -> str:
    """Full alert text (WhatsApp, push, message log)."""
    icon = "\U0001F6A6 " if whatsapp else ""
    return (f"{icon}Maseru Traffic: {window['level']} traffic at {window['name']}. {window['total']} vehicles passed "
            f"in the last 5 min ({window['public_transport']} taxis/buses). {advice_text(window, sub)} "
            f"Reply STOP to opt out.")


def alert_template(window: dict, sub: dict) -> dict:
    """Values for the approved WhatsApp template:
    'Maseru Traffic alert: {{1}} traffic at {{2}}. {{3}} Reply STOP to stop alerts.'"""
    return {"name": config.META_WA_ALERT_TEMPLATE,
            "params": [window["level"].title(), window["name"], advice_text(window, sub)]}


SMS_LIMIT = 160


def sms_text(window: dict, sub: dict) -> str:
    """One SMS part (160 plain characters): Lesotho SMS is billed per part."""
    level, name = window["level"], gsm_text(window["name"])
    advice = _route_advice(sub, window)
    end = " Reply STOP to end"
    head = f"Maseru Traffic: {level} at {name}."
    options = []
    if advice and advice["reroute_advised"]:
        rec = advice["recommended"]
        roads = [gsm_text(r) for r in rec["roads"]]
        for n in range(len(roads), 0, -1):    # drop road names from the end until it fits
            via = ">".join(roads[:n]) + ("..." if n < len(roads) else "")
            options.append(f"{head} Use {via} ({rec['minutes']:.0f}min, saves {advice['minutes_saved']:.0f}).{end}")
    elif advice:
        options.append(f"{head} Usual route still best, about {advice['recommended']['minutes']:.0f}min.{end}")
    options.append(f"{head} Avoid if you can.{end}")
    for text in options:
        if len(text) <= SMS_LIMIT:
            return text
    return (head + end)[:SMS_LIMIT]


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
            _pool.submit(send_whatsapp, sub["phone"], compose(window, sub, True), iid, alert_template(window, sub))
        if sub["sms"]:
            _pool.submit(send_sms, sub["phone"], sms_text(window, sub), iid)
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
        out["whatsapp"] = send_whatsapp(phone, "[TEST] " + compose(window, sub, True), None, alert_template(window, sub))
    if sub.get("sms", True):
        out["sms"] = send_sms(phone, ("TEST " + sms_text(window, sub))[:SMS_LIMIT], None)
    return out


analytics.on_window_closed(handle_window)


def intersection_names() -> list[str]:
    return [v["name"] for v in INTERSECTIONS.values()]
