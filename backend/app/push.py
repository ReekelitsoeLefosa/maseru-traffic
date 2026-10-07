"""Push notifications to the Android/iOS app via Firebase Cloud Messaging (FCM, free).

Setup: Firebase console -> Project settings -> Service accounts -> "Generate new private key".
Save the JSON on the server and point FCM_CREDENTIALS at it, or paste its contents into
FCM_CREDENTIALS_JSON (Render). Without either, pushes are only logged.
"""
import json
import threading

import httpx

from . import config

_lock = threading.Lock()
_creds = None
_project_id = None


def enabled() -> bool:
    return bool(config.FCM_CREDENTIALS_JSON) or (bool(config.FCM_CREDENTIALS) and config.FCM_CREDENTIALS.exists())


def _key_info() -> dict:
    if config.FCM_CREDENTIALS_JSON:
        return json.loads(config.FCM_CREDENTIALS_JSON)
    return json.loads(config.FCM_CREDENTIALS.read_text(encoding="utf-8"))


def _access_token() -> str:
    """OAuth token for the FCM API, refreshed automatically before it expires (1 h)."""
    global _creds, _project_id
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    with _lock:
        if _creds is None:
            info = _key_info()
            _project_id = info["project_id"]
            _creds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/firebase.messaging"])
        if not _creds.valid:
            _creds.refresh(Request())
        return _creds.token


class TokenGone(Exception):
    """The app was uninstalled or the token expired - forget this device."""


def send(token: str, title: str, body: str, data: dict | None = None) -> str:
    if not enabled():
        print(f"[PUSH -> ...{token[-8:]}] {title}: {body}")
        return "sent"
    message = {
        "token": token,
        "notification": {"title": title, "body": body},
        "data": {k: str(v) for k, v in (data or {}).items()},
        "android": {"priority": "high", "notification": {"channel_id": "traffic_alerts", "color": "#00209F"}},
        "apns": {"payload": {"aps": {"sound": "default"}}},
    }
    r = httpx.post(f"https://fcm.googleapis.com/v1/projects/{_project_id or _load_project()}/messages:send",
                   headers={"Authorization": f"Bearer {_access_token()}"}, json={"message": message}, timeout=15)
    if r.status_code == 404 or "UNREGISTERED" in r.text:
        raise TokenGone(token)
    r.raise_for_status()
    return "sent"


def _load_project() -> str:
    _access_token()
    return _project_id
