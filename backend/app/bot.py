"""WhatsApp bot conversation logic (provider-independent).

Examples a user can send:
  hi / help / dumela          -> menu
  status                      -> busiest intersections now
  status kingsway             -> one intersection
  route pioneer mall to airport
  commute ha hoohlo to lekhaloaneng   -> saves the trip; alerts include a personal detour
  watch sefika                -> alerts for that intersection
  places                      -> list of known places
  stop                        -> unsubscribe
"""
import re

from . import db, routing
from .analytics import CATEGORY_LABELS, analytics
from .network import INTERSECTIONS

MENU = (
    "\U0001F6A6 *Maseru Smart Traffic*\n"
    "Send one of:\n"
    "• *status* – busiest junctions now\n"
    "• *status <place>* – e.g. status kingsway\n"
    "• *route <from> to <to>* – best route right now\n"
    "• *commute <from> to <to>* – save your daily trip for alerts\n"
    "• *watch <place>* – alert me when it's congested\n"
    "• *places* – list of places\n"
    "• *stop* – stop alerts"
)
EMOJI = {"LOW": "\U0001F7E2", "MODERATE": "\U0001F7E1", "HEAVY": "\U0001F7E0", "SEVERE": "\U0001F534"}


def _status_line(s: dict) -> str:
    return f"{EMOJI[s['level']]} *{s['name']}*: {s['level_text']} – {s['rolling_5min_total']} vehicles/5 min"


def _detail(s: dict) -> str:
    cats = ", ".join(f"{CATEGORY_LABELS[c]} {n}" for c, n in s["by_category"].items() if n)
    return (f"{_status_line(s)}\nPublic transport: {s['public_transport']}\n{cats or 'No vehicles counted yet'}\n"
            f"Source: {s['source']}")


def _parse_trip(text: str):
    m = re.match(r"(.+?)\s+to\s+(.+)", text, re.I)
    if not m:
        return None, None
    return routing.find_place(m.group(1)), routing.find_place(m.group(2))


def handle(phone: str, text: str, can_subscribe: bool = True) -> str:
    """can_subscribe=False for the in-app preview, where the phone number isn't proven to be the user's."""
    t = (text or "").strip()
    low = t.lower()
    word, _, rest = low.partition(" ")
    rest = rest.strip()

    if word in ("hi", "hello", "help", "menu", "dumela", "lumela", "start", ""):
        return MENU

    if word == "places":
        return "Known places:\n" + "\n".join(f"• {v['name']}" for v in INTERSECTIONS.values())

    if word == "status":
        snap = analytics.snapshot()
        if rest:
            iid = routing.find_place(rest)
            return _detail(snap[iid]) if iid else f"I don't know '{rest}'. Send *places* for the list."
        top = sorted(snap.values(), key=lambda s: s["score"], reverse=True)[:5]
        return "Busiest junctions right now:\n" + "\n".join(_status_line(s) for s in top)

    if word in ("route", "commute"):
        src, dst = _parse_trip(rest)
        if not src or not dst:
            return f"Please send like: *{word} pioneer mall to airport*. Send *places* for the list."
        try:
            r = routing.suggest(src, dst, analytics.scores())
        except ValueError as exc:
            return str(exc)
        rec = r["recommended"]
        reply = (f"\U0001F697 *{r['from']} → {r['to']}*\n"
                 f"Best now: {' → '.join(rec['roads'])} (~{rec['minutes']:.0f} min, {rec['km']} km)\n"
                 f"Via: {', '.join(rec['names'])}")
        if r["reroute_advised"]:
            reply += f"\n⚠️ Usual route is congested – this saves ~{r['minutes_saved']:.0f} min."
        if word == "commute" and not can_subscribe:
            reply += "\n\nTo save this trip for alerts, message this bot on WhatsApp or use the Alerts tab."
        elif word == "commute":
            sub = db.get_subscriber(phone)
            db.upsert_subscriber(phone, sms=sub["sms"] if sub else False, whatsapp=True, origin=src, destination=dst)
            reply += "\n✅ Trip saved. I'll warn you when it gets congested."
        return reply

    if word in ("watch", "subscribe", "stop", "unsubscribe") and not can_subscribe:
        return "Alerts are managed on WhatsApp or in the app's Alerts tab (it checks your number with a code)."

    if word in ("watch", "subscribe"):
        iid = routing.find_place(rest)
        if not iid:
            return "Which place? e.g. *watch kingsway*. Send *places* for the list."
        sub = db.get_subscriber(phone)
        watch = sorted(set((sub["watch"] if sub else []) + [iid]))
        db.upsert_subscriber(phone, sms=sub["sms"] if sub else False, whatsapp=True, watch=watch)
        return f"✅ You'll get alerts when *{INTERSECTIONS[iid]['name']}* has heavy traffic."

    if word in ("stop", "unsubscribe"):
        db.deactivate_subscriber(phone)
        return "You won't receive traffic alerts anymore. Send *hi* anytime to come back."

    # Bare "X to Y" is treated as a route request
    src, dst = _parse_trip(t)
    if src and dst:
        return handle(phone, f"route {t}", can_subscribe)
    return "Sorry, I didn't get that.\n\n" + MENU
