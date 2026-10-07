# Maseru Smart Traffic — IoT Traffic Management & Tracking

An AI camera system for Maseru intersections. It replaces guesswork (fixed-time lights, officers
directing traffic by hand) with live measurements:

1. **Detects vehicles** in a live camera feed (or a test video) with YOLOv8 + ByteTrack tracking.
2. **Measures traffic flow**: counts every vehicle crossing each approach's counting line, by type:
   private car, 4+1 cab, minibus taxi, bus, truck, motorcycle. Totals are stored per **5-minute
   window** and turned into a level: **Low / Moderate / Heavy / Severe**.
3. **Suggests alternative routes** across the city network based on live congestion.
4. **Tells users** in the web app (works on phones), by **SMS** and through a **WhatsApp bot**.
5. **Recommends adaptive signal timings** (Webster's method) from measured demand and shows how
   much waiting time that saves compared with a fixed 90 s cycle.

```
 Camera / video ─► detector.py (YOLOv8 + ByteTrack + counting lines)
                        │ crossings, vehicles-in-view
                        ▼
                  analytics.py ── 5-min windows ──► SQLite (data/traffic.db)
                   │        │                  └──► notifier.py ─► SMS / WhatsApp alerts
                   │        └─► signal_timing.py (adaptive green times)
                   └─► routing.py (congestion-aware Dijkstra over network.py)
                        │
   FastAPI (main.py): REST + WebSocket + MJPEG video + WhatsApp webhooks ─► frontend/ (phone app)
```

## Install the app

> **Publishing on Google Play (and later the App Store) with a cloud server?** Follow
> [GO-LIVE.md](GO-LIVE.md): cloud server, camera station, push notifications, Android build and store listing.

One app runs everywhere. The **Windows app** is the control centre: it runs the cameras and AI.
**Phones (Android, iPhone) and other PCs** install the same app from the browser and connect to it.

### Windows (control centre)
```powershell
powershell -ExecutionPolicy Bypass -File install_windows.ps1
```
This sets up Python packages and puts a **Maseru Smart Traffic** icon on the Desktop and in the Start
menu. The icon opens the app in its own window; closing the window stops the cameras and server.
The PC running it is the **admin**: it sees subscribers and the message log, edits counting lines, etc.

### Phones: Android and iPhone
Phones can only *install* the app from a secure **https://** address. The simplest free way:
1. Download `cloudflared-windows-amd64.exe` from
   https://github.com/cloudflare/cloudflared/releases/latest, rename it to `cloudflared.exe` and put it
   in the `desktop` folder.
2. Start the Windows app. It creates an https link automatically. Open **Alerts → Connect phones** to
   see the link and a QR code.
3. On the phone, scan the QR code, then:
   - **Android (Chrome):** tap **Install app** in the app header (or ⋮ → *Install app*).
   - **iPhone (Safari):** Share button → **Add to Home Screen**.
   - **Other PCs (Edge/Chrome):** the install icon in the address bar.

The quick-tunnel link **changes every time** the Windows app starts. For a permanent address, use a
named Cloudflare tunnel with your own domain, or host the server on a cloud machine; then set
`PUBLIC_URL=https://your-address` in `.env`. Without https, phones on the same Wi-Fi can still *view* the
app at the address shown under *Connect phones*, but can't install it.

**Who can do what.** People using the app over the link can view traffic, find routes, chat with the bot
and subscribe their own number (they confirm it with a 6-digit code sent to that phone, so nobody can
sign up someone else). Admin features need the Windows app or an admin key: set `ADMIN_TOKEN=some-long-secret`
in `.env`, then open `https://your-link/?admin=some-long-secret` once on your own phone.

### App-store apps (optional, later)
`mobile/` wraps the same app as a native Android/iOS app with Capacitor (needs Node.js and a permanent
https server address in `mobile/capacitor.config.json`):
```bash
cd mobile && npm install && npx cap add android && npm run android
```
That opens Android Studio to build the APK / Play Store bundle. **iOS builds need a Mac with Xcode**
(Apple's rule): on a Mac run `npx cap add ios && npm run ios`. Publishing needs a Google Play developer
account (one-off fee) and an Apple Developer account (yearly fee).

### Icons
`python tools/make_icons.py` regenerates all icons (Windows `.ico`, Android, iOS) from one drawing.

## Quick start (Windows, developer mode)

```powershell
.\run.ps1
```
Then open http://localhost:8000 (on your phone: `http://<your-PC-IP>:8000`, same Wi-Fi).

`run.ps1` creates the Python environment at `%USERPROFILE%\.venvs\maseru`, installs
`requirements.txt`, copies `.env.example → .env` and `cameras.example.json → cameras.json`, and starts
the server. The environment is kept at a short path on purpose: Windows can't load OpenCV/PyTorch DLLs
from long folder paths ("DLL load failed … The filename or extension is too long").

The AI packages (PyTorch + OpenCV, ~250 MB download) take a while on a slow connection. If they're
missing, the app still starts and logs `camera disabled, AI packages missing`, and every junction runs
on simulated data. Install them with:
```powershell
& "$env:USERPROFILE\.venvs\maseru\Scripts\python.exe" -m pip install ultralytics lap
```
If PyTorch has no wheel for your Python version yet, use Python 3.12 for the `.venv`.

### Use your test video
1. Copy your video to `videos/test_traffic.mp4` (or change `source` in `cameras.json`).
2. Start the app, open the **Live** tab, click **✏️ Counting lines**, click two points across the lanes
   of one approach, pick N/S/E/W, and **Save**. Add one line per approach. Lines are saved into
   `cameras.json`.
3. For a real camera, set `source` to its RTSP URL, e.g. `rtsp://user:pass@192.168.1.20:554/stream1`,
   or a webcam index like `"0"`.

The first run downloads `yolov8n.pt` (~6 MB) automatically. On a CPU-only PC expect ~5–15 fps;
raise `FRAME_STRIDE` in `.env` if the video lags. Video files are played at real speed so a
"5-minute window" really covers 5 minutes of footage.

### Junctions without cameras
Until every junction has a camera, the others run on **simulated** traffic (marked "simulated"
everywhere) so the map, routing and alerts work city-wide. Set `SIMULATE_OTHER_INTERSECTIONS=false`
to turn it off. The **Alerts** tab has a "Simulate incident" button to demo congestion alerts.

## Road network (real roads)
Junctions and the roads between them come from OpenStreetMap: `tools/build_network.py` downloads
Maseru's main roads, places each junction on its real intersection, and links neighbouring junctions
along the actual road shapes with real lengths. The result is `backend/app/maseru_network.json`.
To add a junction, add a line to `JUNCTIONS` in that script (an anchor point near the intersection)
and run `python tools/build_network.py`.

## Vehicle types: taxis and buses
**Live detection today** uses the standard YOLOv8 model plus Maseru rules (`backend/app/vehicle_types.py`):
4+1 cabs are recognised by the **yellow belt** along their sides, minibus taxis by size.

**Custom model (first round, not yet used live).** `train_custom_model.py` auto-labelled 339 frames
from the Main Circle and Lekhaloaneng videos with those rules and trained a model
(`runs/detect/maseru/weights/best.pt`). Validation mAP50: 4+1 cab 0.89, private car 0.73,
minibus 0.42, bus/truck ≈ 0 (too few examples). It copied the rules' mistakes (e.g. a fuel tanker
as a 4+1 cab), so it isn't better yet. To improve it: correct the labels by hand in
`datasets/maseru` (Label Studio / Roboflow / CVAT), add footage with more buses, trucks and
motorcycles and daytime Lekhaloaneng, then re-run `train`. Switch with `YOLO_MODEL=` in `.env`.

## SMS and WhatsApp

By default messages are printed to the server console (`SMS_PROVIDER=console`), so you can test
everything without an account. Every message is also listed in the app's **Alerts → Message log**.

**Twilio (SMS + WhatsApp)** — set in `.env`:
```
SMS_PROVIDER=twilio
WHATSAPP_PROVIDER=twilio
TWILIO_ACCOUNT_SID=...   TWILIO_AUTH_TOKEN=...
TWILIO_SMS_FROM=+1...    TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
```
For the WhatsApp bot, set the sandbox/number's "When a message comes in" webhook to
`https://<public-url>/webhooks/twilio/whatsapp` (use `ngrok http 8000` during development).

**Meta WhatsApp Cloud API** — set `WHATSAPP_PROVIDER=meta`, `META_WA_TOKEN`,
`META_WA_PHONE_NUMBER_ID`, and register the webhook `https://<public-url>/webhooks/meta/whatsapp` with
verify token `META_WA_VERIFY_TOKEN`. Note: Meta only allows free-form messages within 24 h of the user's
last message; proactive alerts outside that window need an approved message template.

Local SMS gateways (Vodacom Lesotho / Econet bulk SMS) can be added as another provider in `notifier.py`.

**Bot commands:** `hi`, `status`, `status kingsway`, `route pioneer mall to airport`,
`commute ha hoohlo to lekhaloaneng`, `watch sefika`, `places`, `stop`. Try them in the app's **Bot** tab.

Phone numbers: 8-digit Lesotho numbers (e.g. `5xxxxxxx`, `6xxxxxxx`) get `+266` added automatically.

## How congestion is scored

`score = max(vehicles in 5 min ÷ junction capacity, vehicles queued in view ÷ jam level)`
Low < 0.4 ≤ Moderate < 0.7 ≤ Heavy < 0.9 ≤ Severe. Using queue length as well as flow matters: in
a jam very few cars cross the line, so flow alone would look "quiet".
Capacities and jam levels per junction are in `backend/app/network.py`; calibrate them from a few
days of real counts.

## Limitations — read before deploying
* Junction coordinates and road links in `network.py` are **approximate**; replace them with surveyed
  positions or an OpenStreetMap extract before relying on the routes.
* Simulated demand patterns and vehicle mix are assumptions, not survey data.
* The signal plan is a **recommendation** shown in the app; driving real signal controllers needs
  integration with the controller hardware and approval from the traffic authority.
* There is no login on the dashboard yet — add authentication before exposing it to the internet.

## API (selection)
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/intersections` | live state of all junctions (+ signal plan) |
| GET | `/api/intersections/{id}/history` | stored 5-minute windows |
| GET | `/api/route?src=..&dst=..` | route suggestion |
| GET | `/api/cameras/{id}/stream` | annotated MJPEG video |
| PUT | `/api/cameras/{id}/lines` | set counting lines |
| POST | `/api/subscribers` | subscribe a phone number |
| POST | `/api/notify/test` | send a test SMS/WhatsApp |
| WS | `/ws` | live updates every second |
| POST | `/webhooks/twilio/whatsapp`, `/webhooks/meta/whatsapp` | WhatsApp bot |
