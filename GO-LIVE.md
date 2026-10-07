# Going live: cloud server + Play Store app

```
 Junction camera ─► Camera station PC (AI)  ──results + snapshots──►  Cloud server (https)  ◄── Phones (Play Store app)
                    Windows app, this PC          CLOUD_URL/STATION_KEY     docker compose            push alerts, routes, bot
```
Phones never need your Wi-Fi: they talk to the cloud server over the internet. Your PC only *sends*
results to the cloud, so it can sit behind any home/office/mobile connection.

Steps marked 👤 need you (accounts, payments, logins). Everything else is ready in this project.

---

## 1. Cloud server on Render (free)

Three free accounts: **GitHub** (holds the code), **Neon** (keeps the data), **Render** (runs the server).

**Why Neon?** Render's free plan wipes its disk on every restart/update, which would erase subscribers
and phones registered for alerts. Neon is a free PostgreSQL database that keeps them.
(Render's own free database is deleted after 30 days, so don't use that.)

### 1a. Put the code on GitHub
1. 👤 Create an account at https://github.com and a new **private** repository called `maseru-traffic`
   (leave "Add README" unticked).
2. In PowerShell, in the `maseru-traffic` folder (the project is already a Git repository with everything
   committed - secrets, videos and AI models are excluded by `.gitignore`):
   ```powershell
   git remote add origin https://github.com/YOUR-GITHUB-NAME/maseru-traffic.git
   git push -u origin main
   ```
   A GitHub sign-in window opens the first time.

### 1b. Create the database on Neon
1. 👤 Sign up at https://neon.tech → **New project**, region **AWS Europe Central (Frankfurt)**.
2. Copy the **connection string** (starts with `postgresql://`, ends with `?sslmode=require`).

### 1c. Create the server on Render
1. 👤 Sign up at https://render.com with your GitHub account.
2. **New → Blueprint** → choose the `maseru-traffic` repository. Render reads `render.yaml`.
3. It asks for the secret settings:
   - `DATABASE_URL` → the Neon connection string
   - `PUBLIC_URL` → `https://maseru-traffic.onrender.com` (if Render gives the service a different name,
     use that address; you can change it later under **Environment**)
   - `FCM_CREDENTIALS_JSON` → leave empty for now (step 3)
4. **Apply**. The first build takes a few minutes. Open the address: the app loads with a padlock.
5. In Render → the service → **Environment**, copy **`STATION_KEY`** and **`ADMIN_TOKEN`** (Render generated
   them). Open `https://YOUR-APP.onrender.com/?admin=ADMIN_TOKEN` once on your own phone/PC for admin features.

Updating later: `git add -A`, `git commit -m "..."`, `git push` - Render redeploys automatically.

### 1d. Keep it awake 24/7 (free)
Render's free server sleeps after 15 minutes without visitors. A free uptime monitor visits it every
5 minutes so it never sleeps (and emails you if it goes down):
1. 👤 Sign up at https://uptimerobot.com (free plan).
2. **+ New monitor** → type **HTTP(s)** → URL `https://maseru-traffic.onrender.com/healthz` →
   interval **5 minutes** → **Create monitor**.
3. Add your email as the alert contact, so you hear about outages.

**Free-plan limits to know:**
- With the uptime monitor (or a running camera station, which sends data every 2 s) the server stays awake.
- 750 free hours/month covers one always-on service (a month is ~744 h) - don't run a second free service.
- 100 GB/month of traffic: plenty for alerts and routes; live camera snapshots use roughly 1 GB/month per
  camera plus viewing.
- When the town relies on it, move to Render's Starter plan (US$7/month: no sleeping) or a VPS (below).

### Alternative: your own server (≈ US$5–7/month)

1. 👤 Rent a small Linux server: **Ubuntu 24.04, 1 vCPU, 1–2 GB RAM** (Hetzner, DigitalOcean, Contabo, …).
   Note its **IP address**.
2. 👤 Address: buy a domain and point an `A` record at the IP, **or** use the free
   `IP-with-dashes.sslip.io` (IP `203.0.113.7` → `203-0-113-7.sslip.io`).
3. Log in to the server (`ssh root@IP`) and install Docker:
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```
4. Copy this project to the server (from this PC, in PowerShell):
   ```powershell
   scp -r "$HOME\OneDrive\Desktop\Maseru Smart Traffic\maseru-traffic" root@IP:/opt/
   ```
5. On the server:
   ```bash
   cd /opt/maseru-traffic
   cp deploy/server.env.example .env
   nano .env        # set DOMAIN, PUBLIC_URL, STATION_KEY, ADMIN_TOKEN (long random strings)
   docker compose up -d --build
   ```
   Make random keys with: `openssl rand -hex 24`
6. Open `https://YOUR-DOMAIN` - the app should load with a padlock. Open
   `https://YOUR-DOMAIN/?admin=YOUR_ADMIN_TOKEN` once to unlock admin features on that device.

Updates later: copy the changed files again and run `docker compose up -d --build`.
Data (database, logs) is in `/opt/maseru-traffic/data` - back it up.

## 2. Camera station (this PC)

In `maseru-traffic/.env` on this PC add (Render: `https://YOUR-APP.onrender.com`):
```
CLOUD_URL=https://YOUR-DOMAIN
STATION_KEY=the-same-key-as-on-the-server
SHARE_ONLINE=false
```
Start the Windows app. In the cloud app, Lekhaloaneng now shows **live camera** instead of simulated.
If the internet drops, the station keeps the measurements and sends them when it's back.

## 2b. SMS and WhatsApp alerts

Costs to Lesotho (check current prices): **SMS via Twilio ≈ US$0.41 per message** (the app keeps every
alert to one 160-character SMS); **WhatsApp ≈ a few US cents per alert template**, and **free** for 24 h
after the person last messaged your bot. So WhatsApp is the main channel and SMS the backup.
In Render → **Environment**, a provider set to `console` (or empty) only writes messages to the log.

### WhatsApp (Meta WhatsApp Cloud API) 👤
1. https://business.facebook.com → create a **Meta Business** account (needs your business/organisation
   name and an email).
2. https://developers.facebook.com → **My Apps → Create app** → type **Business** → add the
   **WhatsApp** product. Meta gives you a free **test number** to try with up to 5 phones of your own.
3. To use a real number: **WhatsApp → API Setup → Add phone number** - a number that is *not* already on
   WhatsApp, which you verify by SMS/call. Then **verify your business** in Business Settings.
4. **Permanent token:** Business Settings → Users → **System users** → add one (Admin) → **Generate token**
   for your app with `whatsapp_business_messaging` and `whatsapp_business_management`.
5. **Message templates** (WhatsApp Manager → Message templates → Create). Create exactly these two:
   - Name **`traffic_alert`**, category **Utility**, language **English**, body:
     `Maseru Traffic alert: {{1}} traffic at {{2}}. {{3}} Reply STOP to stop alerts.`
     (sample values: `Heavy` / `Main Circle (Cathedral)` / `Use Kofi Annan Road (about 11 min).`)
   - Name **`verification_code`**, category **Authentication**, **Copy code** button.
   Wait for both to show **Approved**.
6. **Webhook** (so the bot can answer): App → WhatsApp → **Configuration** → Callback URL
   `https://maseru-traffic.onrender.com/webhooks/meta/whatsapp`, Verify token = Render's
   `META_WA_VERIFY_TOKEN`. Then subscribe to the **messages** field.
7. In Render → **Environment** set: `WHATSAPP_PROVIDER=meta`, `META_WA_TOKEN`, `META_WA_PHONE_NUMBER_ID`
   (API Setup page) and `META_APP_SECRET` (App settings → Basic). Save.
8. Test: send `hi` to your WhatsApp number - the bot answers with the menu.

### SMS (Twilio) 👤
1. https://www.twilio.com → sign up. The free trial can only text numbers you verify first; **upgrade**
   (add credit) to text anyone.
2. Buy a number with SMS (**Phone Numbers → Buy a number**). Check Twilio's *Lesotho* guidelines page for
   whether an alphanumeric sender name like `MaseruTrfc` is allowed - replies (STOP) only work with a number.
3. Number → **Messaging configuration** → "A message comes in": Webhook
   `https://maseru-traffic.onrender.com/webhooks/twilio/sms` (HTTP POST).
4. In Render → **Environment** set: `SMS_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`
   (Twilio console home page), `TWILIO_SMS_FROM` (the number, e.g. `+1...`). Save.
5. Test in the app as admin: **Alerts → Send test message** to your number.

`PUBLIC_URL` must be exactly `https://maseru-traffic.onrender.com` - Twilio messages are checked against it.

## 3. Push notifications (free) - Firebase

1. 👤 https://console.firebase.google.com → **Add project** (Google Analytics not needed).
2. 👤 **Add app → Android**, package name **`ls.maseru.traffic`**. Download **`google-services.json`**
   and put it in `mobile/android/app/`.
3. 👤 **Project settings → Service accounts → Generate new private key** (keep this file secret - never put
   it in the app or on GitHub).
   - **Render:** open the file in Notepad, copy everything, paste it as the value of
     `FCM_CREDENTIALS_JSON` under the service's **Environment**, then **Save** (it redeploys).
   - **Own server:** copy it to `/opt/maseru-traffic/data/firebase-service-account.json` and run
     `docker compose restart app`.

## 4. Android app → Google Play

**Tools (one-time, large download ~3–4 GB):** 👤 install **Android Studio** from
https://developer.android.com/studio (includes the Java and Android SDK).

1. Point the app at your server: in `mobile/capacitor.config.json` set
   `"url": "https://YOUR-DOMAIN"` (Render: `https://YOUR-APP.onrender.com`).
2. Build the Android project (the `android` folder is already created):
   ```bash
   cd mobile
   npx cap sync android
   npx cap open android
   ```
3. In Android Studio: wait for "Gradle sync" to finish, plug in an Android phone (USB debugging on) and
   press ▶ **Run** to test. Check: live traffic, routes, **Alerts → Turn on alerts**, 📍 near me.
4. Release build: **Build → Generate Signed App Bundle → Android App Bundle** → create a new key store.
   👤 **Keep the key store file and passwords safe forever** - without them you can never update the app.
   The result is `app-release.aab`.
5. 👤 Google Play Console (https://play.google.com/console, US$25 once) → **Create app**:
   - Upload `app-release.aab` (start with **Internal testing**, then Production).
   - **Store listing:** icon `store/play-icon-512.png`, feature graphic
     `store/feature-graphic-1024x500.png`, 2–8 phone screenshots (take them on your phone).
   - **Privacy policy URL:** `https://YOUR-DOMAIN/privacy.html` - first fill in the `[...]` parts in
     `frontend/privacy.html` (your name/organisation, email, date) and redeploy.
   - **Data safety:** collects phone number (optional, for alerts), approximate location is **not**
     collected (it stays on the phone), device ID (push token) for notifications; data is encrypted in
     transit (https); users can request deletion.
   - New personal developer accounts must run a **closed test with at least 12 testers for 14 days**
     before going to Production - recruit friends/colleagues early.

## 5. iPhone (later)

Same project: `npx cap add ios`, then build on a Mac with Xcode, or with a cloud build service
(e.g. Codemagic) if you don't have a Mac. 👤 Apple Developer Program (US$99/year). For push on iOS,
add an APNs key to the Firebase project.
