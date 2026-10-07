"""Maseru Smart Traffic - Windows app.

Starts the traffic server (cameras + AI + API) and shows the dashboard in its own window.
If cloudflared is available it also opens a secure https link so phones can install the app
(the link and a QR code appear under Alerts -> Connect phones). Closing the window stops everything.

Start it from the desktop shortcut, or:  pythonw desktop/maseru_traffic.py
(Not named app.py: that would hide the server's `app` package.)
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.chdir(ROOT)

LOG = ROOT / "data" / "app.log"
LOG.parent.mkdir(exist_ok=True)
if sys.stdout is None or "pythonw" in Path(sys.executable).name.lower():
    # no console when started from the shortcut: keep the server's messages in a log file
    sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)

from app import config  # noqa: E402  (after sys.path / cwd are set)

URL = f"http://127.0.0.1:{config.PORT}"
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_server():
    import uvicorn
    server = uvicorn.Server(uvicorn.Config("app.main:app", host="0.0.0.0", port=config.PORT, log_level="info"))
    threading.Thread(target=server.run, daemon=True, name="server").start()
    for _ in range(300):              # the AI model can take a while to load the first time
        if server.started:
            break
        time.sleep(0.2)
    return server


def find_cloudflared() -> str | None:
    local = ROOT / "desktop" / "cloudflared.exe"
    return str(local) if local.exists() else shutil.which("cloudflared")


def start_tunnel():
    """Free Cloudflare quick tunnel: an https address that reaches this PC from any phone.
    The address changes every time the app starts; it's shown in the app with a QR code."""
    exe = find_cloudflared()
    config.PUBLIC_URL_FILE.unlink(missing_ok=True)
    if not exe or os.getenv("SHARE_ONLINE", "true").lower() in ("0", "false", "no"):
        return None
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", URL], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def watch():
        for line in proc.stdout:
            m = TUNNEL_URL_RE.search(line)
            if m:
                config.PUBLIC_URL_FILE.write_text(m.group(0), encoding="utf-8")
                print(f"[app] phones can use {m.group(0)}")
    threading.Thread(target=watch, daemon=True, name="tunnel").start()
    return proc


def main():
    import webview

    server = None
    if port_in_use(config.PORT):
        print(f"[app] server already running on port {config.PORT}; opening a window to it")
    else:
        server = start_server()
    tunnel = start_tunnel() if server else None

    webview.create_window("Maseru Smart Traffic", URL, width=1280, height=860, min_size=(380, 600))
    webview.start(icon=str(ROOT / "desktop" / "app.ico"))

    # window closed: shut down
    if tunnel:
        tunnel.terminate()
    config.PUBLIC_URL_FILE.unlink(missing_ok=True)
    if server:
        server.should_exit = True
        time.sleep(1.5)
    os._exit(0)    # camera threads are daemons; exit without waiting for video reads


if __name__ == "__main__":
    main()
