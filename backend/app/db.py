"""Persistence: vehicle crossings, 5-minute window summaries, subscribers, app devices and alert log.

SQLite file by default (camera station / Windows app). With DATABASE_URL set it uses PostgreSQL
instead - needed on hosts whose disk is wiped on every restart, such as Render's free plan
(use a free Neon database: https://neon.tech).
"""
import json
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()
PG = config.DATABASE_URL.startswith(("postgres://", "postgresql://"))


def _connect():
    if PG:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(config.DATABASE_URL, autocommit=True, row_factory=dict_row, connect_timeout=15)
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _sql(sql: str) -> str:
    """Our SQL is written for SQLite; PostgreSQL uses %s placeholders."""
    return sql.replace("?", "%s") if PG else sql


_conn = _connect()

SCHEMA = """
CREATE TABLE IF NOT EXISTS crossings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    intersection_id TEXT NOT NULL,
    approach TEXT NOT NULL,
    category TEXT NOT NULL,
    direction TEXT NOT NULL,
    simulated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_crossings_ts ON crossings(intersection_id, ts);

CREATE TABLE IF NOT EXISTS windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intersection_id TEXT NOT NULL,
    window_start REAL NOT NULL,
    window_end REAL NOT NULL,
    total INTEGER NOT NULL,
    by_category TEXT NOT NULL,
    by_approach TEXT NOT NULL,
    avg_occupancy REAL NOT NULL,
    score REAL NOT NULL,
    level TEXT NOT NULL,
    UNIQUE(intersection_id, window_start)
);

CREATE TABLE IF NOT EXISTS subscribers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    phone TEXT NOT NULL UNIQUE,
    sms INTEGER NOT NULL DEFAULT 1,
    whatsapp INTEGER NOT NULL DEFAULT 1,
    watch TEXT NOT NULL DEFAULT '[]',      -- JSON list of intersection ids
    origin TEXT,                           -- commute start node
    destination TEXT,                      -- commute end node
    active INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL
);

-- phones with the Android/iOS app: alerts go to them as free push notifications
CREATE TABLE IF NOT EXISTS devices (
    token TEXT PRIMARY KEY,                -- Firebase Cloud Messaging registration token
    platform TEXT NOT NULL,
    watch TEXT NOT NULL DEFAULT '[]',
    origin TEXT,
    destination TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL
);

-- sign-ups from the public app wait here until the phone owner enters the code sent to them
CREATE TABLE IF NOT EXISTS pending_subscriptions (
    phone TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    code TEXT NOT NULL,
    expires REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    sent REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    phone TEXT NOT NULL,
    channel TEXT NOT NULL,
    intersection_id TEXT,
    message TEXT NOT NULL,
    status TEXT NOT NULL
);
"""

def _create_schema():
    if PG:
        ddl = (SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
                     .replace(" REAL", " DOUBLE PRECISION"))   # PostgreSQL REAL is too coarse for timestamps
        for stmt in ddl.split(";"):
            if stmt.strip():
                _conn.execute(stmt)
    else:
        _conn.executescript(SCHEMA)
        _conn.commit()


with _lock:
    _create_schema()


def _run(fn):
    """Run a database call; reconnect once if the server dropped the connection (Neon idles them)."""
    global _conn
    with _lock:
        try:
            return fn(_conn)
        except Exception as exc:
            lost = "closed" in str(exc).lower() or "connection" in str(exc).lower()
            if not (PG and lost):
                raise
            _conn = _connect()
            return fn(_conn)


def execute(sql: str, params: tuple = ()):
    def go(conn):
        cur = conn.execute(_sql(sql), params)
        if not PG:
            conn.commit()
        return cur.rowcount
    return _run(go)


def query(sql: str, params: tuple = ()) -> list[dict]:
    return _run(lambda conn: [dict(r) for r in conn.execute(_sql(sql), params).fetchall()])


# --- Crossings & windows ---------------------------------------------------

def add_crossing(ts, intersection_id, approach, category, direction, simulated=False):
    execute(
        "INSERT INTO crossings(ts, intersection_id, approach, category, direction, simulated) VALUES (?,?,?,?,?,?)",
        (ts, intersection_id, approach, category, direction, int(simulated)),
    )


def save_window(w: dict):
    execute(
        """INSERT INTO windows(intersection_id, window_start, window_end, total, by_category,
               by_approach, avg_occupancy, score, level) VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(intersection_id, window_start) DO UPDATE SET window_end=excluded.window_end,
               total=excluded.total, by_category=excluded.by_category, by_approach=excluded.by_approach,
               avg_occupancy=excluded.avg_occupancy, score=excluded.score, level=excluded.level""",
        (w["intersection_id"], w["window_start"], w["window_end"], w["total"], json.dumps(w["by_category"]),
         json.dumps(w["by_approach"]), w["avg_occupancy"], w["score"], w["level"]),
    )


def recent_windows(intersection_id: str, limit: int = 24) -> list[dict]:
    rows = query(
        "SELECT * FROM windows WHERE intersection_id=? ORDER BY window_start DESC LIMIT ?",
        (intersection_id, limit),
    )
    for r in rows:
        r["by_category"] = json.loads(r["by_category"])
        r["by_approach"] = json.loads(r["by_approach"])
    return list(reversed(rows))


# --- Subscribers -----------------------------------------------------------

def _sub_row(r: dict) -> dict:
    r["watch"] = json.loads(r["watch"])
    r["sms"], r["whatsapp"], r["active"] = bool(r["sms"]), bool(r["whatsapp"]), bool(r["active"])
    return r


def upsert_subscriber(phone, name=None, sms=True, whatsapp=True, watch=None, origin=None, destination=None) -> dict:
    existing = get_subscriber(phone)
    if existing:
        execute(
            """UPDATE subscribers SET name=COALESCE(?, name), sms=?, whatsapp=?, watch=?,
                   origin=COALESCE(?, origin), destination=COALESCE(?, destination), active=1 WHERE phone=?""",
            (name, int(sms), int(whatsapp), json.dumps(watch if watch is not None else existing["watch"]),
             origin, destination, phone),
        )
    else:
        execute(
            """INSERT INTO subscribers(name, phone, sms, whatsapp, watch, origin, destination, created)
               VALUES (?,?,?,?,?,?,?,?)""",
            (name, phone, int(sms), int(whatsapp), json.dumps(watch or []), origin, destination, time.time()),
        )
    return get_subscriber(phone)


def get_subscriber(phone) -> dict | None:
    rows = query("SELECT * FROM subscribers WHERE phone=?", (phone,))
    return _sub_row(rows[0]) if rows else None


def list_subscribers(active_only=True) -> list[dict]:
    sql = "SELECT * FROM subscribers" + (" WHERE active=1" if active_only else "") + " ORDER BY created DESC"
    return [_sub_row(r) for r in query(sql)]


def set_sms(phone, enabled: bool):
    execute("UPDATE subscribers SET sms=? WHERE phone=?", (int(enabled), phone))


def last_inbound_ts(phone) -> float:
    """When this person last messaged our WhatsApp bot (opens WhatsApp's free 24-hour window)."""
    rows = query("SELECT MAX(ts) AS t FROM alerts WHERE phone=? AND channel='whatsapp:bot-in'", (phone,))
    return rows[0]["t"] or 0.0


def deactivate_subscriber(phone):
    execute("UPDATE subscribers SET active=0 WHERE phone=?", (phone,))


def delete_subscriber(sub_id: int):
    execute("DELETE FROM subscribers WHERE id=?", (sub_id,))


# --- App devices (push notifications) --------------------------------------

def upsert_device(token, platform, watch, origin, destination) -> dict:
    now = time.time()
    execute(
        """INSERT INTO devices(token, platform, watch, origin, destination, created, updated) VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(token) DO UPDATE SET platform=excluded.platform, watch=excluded.watch,
               origin=excluded.origin, destination=excluded.destination, updated=excluded.updated""",
        (token, platform, json.dumps(watch), origin, destination, now, now),
    )
    return get_device(token)


def get_device(token) -> dict | None:
    rows = query("SELECT * FROM devices WHERE token=?", (token,))
    if not rows:
        return None
    rows[0]["watch"] = json.loads(rows[0]["watch"])
    return rows[0]


def list_devices() -> list[dict]:
    rows = query("SELECT * FROM devices")
    for r in rows:
        r["watch"] = json.loads(r["watch"])
    return rows


def delete_device(token):
    execute("DELETE FROM devices WHERE token=?", (token,))


# --- Phone verification ----------------------------------------------------

def save_pending(phone: str, data: dict, code: str, ttl: float = 600):
    now = time.time()
    execute(
        """INSERT INTO pending_subscriptions(phone, data, code, expires, attempts, sent) VALUES (?,?,?,?,0,?)
           ON CONFLICT(phone) DO UPDATE SET data=excluded.data, code=excluded.code, expires=excluded.expires,
               attempts=0, sent=excluded.sent""",
        (phone, json.dumps(data), code, now + ttl, now),
    )


def get_pending(phone: str) -> dict | None:
    rows = query("SELECT * FROM pending_subscriptions WHERE phone=?", (phone,))
    if not rows:
        return None
    rows[0]["data"] = json.loads(rows[0]["data"])
    return rows[0]


def count_failed_attempt(phone: str):
    execute("UPDATE pending_subscriptions SET attempts = attempts + 1 WHERE phone=?", (phone,))


def delete_pending(phone: str):
    execute("DELETE FROM pending_subscriptions WHERE phone=?", (phone,))


# --- Alert log -------------------------------------------------------------

def log_alert(phone, channel, intersection_id, message, status):
    execute(
        "INSERT INTO alerts(ts, phone, channel, intersection_id, message, status) VALUES (?,?,?,?,?,?)",
        (time.time(), phone, channel, intersection_id, message, status),
    )


def purge_old_alerts(days: int):
    """Message logs hold phone numbers and texts: keep them only as long as the privacy policy says."""
    execute("DELETE FROM alerts WHERE ts < ?", (time.time() - days * 86400,))
    execute("DELETE FROM pending_subscriptions WHERE expires < ?", (time.time(),))


def recent_alerts(limit=50) -> list[dict]:
    return query("SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (limit,))


def last_alert_ts(phone, intersection_id) -> float:
    rows = query(
        "SELECT MAX(ts) AS t FROM alerts WHERE phone=? AND intersection_id=? AND status='sent'",
        (phone, intersection_id),
    )
    return rows[0]["t"] or 0.0
