const $ = (s) => document.querySelector(s);
const LEVEL_COLORS = { LOW: "#16a34a", MODERATE: "#d4a106", HEAVY: "#ea6c0c", SEVERE: "#dc2626" };
const PT = new Set(["sedan_taxi", "minibus_taxi", "bus"]);
const CAT_COLORS = { private_car: "#3b82f6", sedan_taxi: "#f59e0b", minibus_taxi: "#f97316", bus: "#10b981", truck: "#8b5cf6", motorcycle: "#64748b" };

let META = null, SNAP = {}, selected = null, lastWindowStart = {}, chart = null;
let map, markers = {}, roadLines = [], routeLayers = [];
let editing = false, pendingPoint = null, draftLines = [];

// Admin key: open the app once with ?admin=KEY on a phone/laptop to unlock admin features there.
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode: key lasts for this visit */ } },
};
const urlAdmin = new URLSearchParams(location.search).get("admin");
if (urlAdmin) { store.set("adminToken", urlAdmin); history.replaceState(null, "", location.pathname); }
const ADMIN_TOKEN = urlAdmin || store.get("adminToken") || "";

const api = async (url, opts = {}) => {
  const headers = { "Content-Type": "application/json", ...(ADMIN_TOKEN ? { "X-Admin-Token": ADMIN_TOKEN } : {}) };
  const r = await fetch(url, { ...opts, headers });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const placeName = (id) => META.places.find((p) => p.id === id)?.name ?? id;

// ------------------------------------------------------------------ tabs
document.querySelectorAll("nav.tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("nav.tabs button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
    if (b.dataset.tab === "map") setTimeout(() => map?.invalidateSize(), 300);
    if (b.dataset.tab === "alerts" && META?.admin) loadAlerts();
  })
);

// ------------------------------------------------------------------ init
async function init() {
  META = await api("/api/meta");
  document.body.classList.toggle("is-admin", META.admin);
  if (META.admin) renderShare();
  const opts = META.places.map((p) => `<option value="${p.id}">${esc(p.name)}${META.cameras.includes(p.id) ? " 📹" : ""}</option>`).join("");
  for (const id of ["#intersection", "#routeFrom", "#routeTo", "#incidentAt"]) $(id).innerHTML = opts;
  const none = `<option value="">—</option>`;
  document.querySelector("[name=origin]").innerHTML = none + opts;
  document.querySelector("[name=destination]").innerHTML = none + opts;
  $("#watchList").innerHTML = META.places.map((p) => `<label><input type="checkbox" value="${p.id}"> ${esc(p.name)}</label>`).join("");
  $("#providers").textContent = `SMS: ${META.providers.sms} · WhatsApp: ${META.providers.whatsapp}`;
  $("#routeTo").value = "airport";
  selected = META.cameras[0] || META.places[0].id;
  $("#intersection").value = selected;
  $("#intersection").addEventListener("change", (e) => selectIntersection(e.target.value));
  initMap();
  initChart();
  selectIntersection(selected);
  connect();
  botSay("Dumela! 👋 Send *hi* to see what I can do.");
}

function selectIntersection(id) {
  selected = id;
  $("#intersection").value = id;
  const hasCam = META.cameras.includes(id);
  $("#video").src = hasCam ? `/api/cameras/${id}/stream` : "";
  $("#video").classList.toggle("hidden", !hasCam);
  $("#noVideo").classList.toggle("hidden", hasCam);
  $("#editLines").classList.toggle("hidden", !META.local_cameras.includes(id) || !META.admin);
  stopEditing();
  loadHistory();
  if (SNAP[id]) render();
}

// ------------------------------------------------------------- live feed
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => ($("#conn").textContent = "live");
  ws.onmessage = (e) => { SNAP = JSON.parse(e.data); render(); };
  ws.onclose = () => { $("#conn").textContent = "reconnecting…"; setTimeout(connect, 2000); };
}

function render() {
  const s = SNAP[selected];
  if (!s) return;
  const color = LEVEL_COLORS[s.level];
  $("#levelText").textContent = s.level_text;
  $("#levelText").style.color = color;
  $("#levelCard").style.borderLeftColor = color;
  $("#gaugeFill").style.width = Math.min(100, s.score * 100) + "%";
  $("#gaugeFill").style.background = color;
  $("#sourceTag").textContent = `Load ${Math.round(s.score * 100)}% of capacity · ${s.source === "camera" ? "📹 live camera" : "simulated data"} · ${s.total_today} vehicles today`;
  $("#kTotal").textContent = s.rolling_5min_total;
  $("#kPT").textContent = s.public_transport;
  $("#kHour").textContent = s.vehicles_per_hour;
  $("#kOcc").textContent = s.occupancy;

  const w = s.current_window, now = Date.now() / 1000;
  $("#winLabel").textContent = `${fmtTime(w.start)}–${fmtTime(w.end)}`;
  $("#winProgress").style.width = Math.min(100, ((now - w.start) / (w.end - w.start)) * 100) + "%";
  $("#winCount").textContent = w.total;
  const left = Math.max(0, Math.round(w.end - now));
  $("#winLeft").textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
  const max = Math.max(1, ...Object.values(w.by_category));
  $("#catBars").innerHTML = Object.entries(w.by_category).map(([c, n]) =>
    `<div class="cat-row"><span>${esc(META.categories[c])}</span><div><div class="bar ${PT.has(c) ? "pt" : ""}" style="width:${(n / max) * 100}%"></div></div><b>${n}</b></div>`).join("");

  if (s.camera) {
    const c = s.camera;
    $("#camStatus").textContent = c.state === "error" ? `⚠️ ${c.error}` : `Camera ${c.state} · ${c.fps} fps · ${c.model}`;
  } else $("#camStatus").textContent = "";

  if (lastWindowStart[selected] && lastWindowStart[selected] !== w.start) loadHistory();
  lastWindowStart[selected] = w.start;

  renderMap();
  renderSignals();
}

// ------------------------------------------------------------- history
function initChart() {
  chart = new Chart($("#histChart"), {
    type: "bar",
    data: { labels: [], datasets: Object.keys(CAT_COLORS).map((c) => ({ label: META.categories[c], data: [], backgroundColor: CAT_COLORS[c], stack: "v" })) },
    options: { responsive: true, maintainAspectRatio: false, animation: false,
      scales: { x: { stacked: true }, y: { stacked: true, beginAtZero: true, title: { display: true, text: "vehicles / 5 min" } } },
      plugins: { legend: { position: "bottom", labels: { boxWidth: 10, font: { size: 11 } } } } },
  });
}

async function loadHistory() {
  const rows = await api(`/api/intersections/${selected}/history?limit=12`);
  chart.data.labels = rows.map((r) => fmtTime(r.window_start));
  chart.data.datasets.forEach((ds, i) => { const c = Object.keys(CAT_COLORS)[i]; ds.data = rows.map((r) => r.by_category[c] || 0); });
  chart.update();
}

// ----------------------------------------------------------------- map
function initMap() {
  map = L.map("map").setView([-29.315, 27.49], 13);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" }).addTo(map);
  roadLines = META.roads.map((r) => ({ ...r, line: L.polyline(r.geometry, { weight: 6, opacity: 0.75, color: "#94a3b8" })
    .bindTooltip(`${esc(r.road)} · ${r.km.toFixed(1)} km`).addTo(map) }));
  map.fitBounds(L.featureGroup(roadLines.map((r) => r.line)).getBounds(), { padding: [20, 20] });
  META.places.forEach((p) => {
    markers[p.id] = L.circleMarker([p.lat, p.lon], { radius: 11, weight: 3, color: "#fff", fillOpacity: 1 })
      .addTo(map).on("click", () => selectIntersection(p.id));
  });
}

function renderMap() {
  for (const [id, m] of Object.entries(markers)) {
    const s = SNAP[id];
    if (!s) continue;
    m.setStyle({ fillColor: LEVEL_COLORS[s.level] });
    m.bindTooltip(`<b>${esc(s.name)}</b>${s.source === "camera" ? " 📹" : ""}<br>${s.level_text}<br>${s.rolling_5min_total} veh / 5 min · ${s.public_transport} taxis & buses`);
  }
  for (const r of roadLines) {
    const sc = ((SNAP[r.from]?.score || 0) + (SNAP[r.to]?.score || 0)) / 2;
    r.line.setStyle({ color: sc >= 0.9 ? LEVEL_COLORS.SEVERE : sc >= 0.7 ? LEVEL_COLORS.HEAVY : sc >= 0.4 ? LEVEL_COLORS.MODERATE : LEVEL_COLORS.LOW });
  }
}

$("#routeBtn").addEventListener("click", async () => {
  try {
    const r = await api(`/api/route?src=${$("#routeFrom").value}&dst=${$("#routeTo").value}`);
    routeLayers.forEach((l) => map.removeLayer(l));
    routeLayers = [];
    if (r.reroute_advised) routeLayers.push(L.polyline(r.usual.coords, { color: "#111", weight: 4, dashArray: "6 8", opacity: 0.7 }).addTo(map));
    const best = L.polyline(r.recommended.coords, { color: "#00209F", weight: 9, opacity: 0.85 }).addTo(map);
    routeLayers.push(best);
    map.fitBounds(best.getBounds(), { padding: [40, 40] });
    const rec = r.recommended;
    $("#routeResult").innerHTML = `
      <div class="route-box ${r.reroute_advised ? "warn" : ""}">
        <p><b>${r.reroute_advised ? "⚠️ Take the alternative route" : "✅ Your usual route is clear"}</b></p>
        <p>Via <b>${esc(rec.roads.join(" → "))}</b></p>
        <p>~${Math.round(rec.minutes)} min · ${rec.km} km (${Math.round(rec.free_minutes)} min without traffic)</p>
        ${r.reroute_advised ? `<p>Usual route (${esc(r.usual.roads.join(" → "))}) would take ~${Math.round(r.usual.minutes)} min — you save ~${Math.round(r.minutes_saved)} min.</p>` : ""}
        ${r.alternative ? `<p class="muted">Backup option: ${esc(r.alternative.roads.join(" → "))} (~${Math.round(r.alternative.minutes)} min)</p>` : ""}
        <p class="muted small">Stops: ${esc(rec.names.join(" · "))}</p>
      </div>`;
  } catch (e) { $("#routeResult").innerHTML = `<p class="fail">${esc(e.message)}</p>`; }
});

// -------------------------------------------------------------- signals
function renderSignals() {
  const s = SNAP[selected], sig = s.signal;
  $("#sigName").textContent = "· " + s.name;
  if (!sig.available) {
    $("#signalBody").innerHTML = `<p class="muted">${esc(sig.reason)}</p>`;
  } else {
    const bars = (plan, fixed) => sig.phases.map((p) => {
      const g = fixed ? sig.fixed_plan.green_each_s : p.green_s, c = fixed ? sig.fixed_plan.cycle_s : sig.cycle_s;
      return `<div class="phase"><small>${esc(p.phase)} (${p.approaches.join("+")}) · ${p.flow_pcu_h} PCU/h</small>
        <div class="bar ${fixed ? "fixed" : ""}" style="width:${(g / c) * 100}%">${g}s green</div></div>`;
    }).join("");
    $("#signalBody").innerHTML = `
      <p><b>Recommended cycle: ${sig.cycle_s}s</b> ${sig.oversaturated ? '<span class="badge SEVERE">over capacity</span>' : ""}
        ${sig.queue_adjusted ? '<span class="badge HEAVY">queue detected</span>' : ""}</p>
      ${bars(sig, false)}
      <p class="muted small">Fixed-time plan today: ${sig.fixed_plan.cycle_s}s cycle, equal ${sig.fixed_plan.green_each_s}s greens</p>
      ${bars(sig, true)}
      ${sig.oversaturated
        ? `<p class="route-box warn">Demand exceeds what this junction can serve: the plan runs the longest safe cycle with green split by demand, and drivers are being sent detours. Retiming alone can't clear it.</p>`
        : `<div class="compare">
        <div><small>Avg wait · fixed</small><b>${sig.avg_delay_fixed_s}s</b></div>
        <div><small>Avg wait · adaptive</small><b style="color:var(--LOW)">${sig.avg_delay_adaptive_s}s</b></div>
      </div>
      <p><b>${sig.delay_reduction_pct}%</b> less waiting per vehicle at the current demand.</p>`}`;
  }
  $("#sigTable").innerHTML = Object.values(SNAP).sort((a, b) => b.score - a.score).map((x) =>
    `<tr data-id="${x.id}"><td>${esc(x.name)}</td><td><span class="badge ${x.level}">${x.level}</span></td><td>${x.rolling_5min_total}</td>
     <td>${x.signal.available ? x.signal.cycle_s + "s" : "–"}</td><td>${x.signal.available && !x.signal.oversaturated ? x.signal.delay_reduction_pct + "%" : "–"}</td></tr>`).join("");
}
$("#sigTable").addEventListener("click", (e) => { const tr = e.target.closest("tr"); if (tr) selectIntersection(tr.dataset.id); });

// --------------------------------------------------------------- alerts
async function loadAlerts() {
  const [subs, log] = await Promise.all([api("/api/subscribers"), api("/api/alerts")]);
  $("#subList").innerHTML = subs.map((s) => `<li><span><b>${esc(s.name || "—")}</b> ${esc(s.phone)} ${s.active ? "" : "(stopped)"}<br>
      <small class="muted">${[s.whatsapp && "WhatsApp", s.sms && "SMS"].filter(Boolean).join(" + ")}
      ${s.origin ? ` · trip ${esc(placeName(s.origin))} → ${esc(placeName(s.destination))}` : ""}
      ${s.watch.length ? ` · watching ${s.watch.map((w) => esc(placeName(w))).join(", ")}` : ""}</small></span>
      <button class="ghost small" data-del="${s.id}">Remove</button></li>`).join("") || "<li class='muted'>No subscribers yet</li>";
  $("#alertLog").innerHTML = log.map((a) => `<li><div class="meta">${new Date(a.ts * 1000).toLocaleString()} · ${esc(a.channel)} · ${esc(a.phone)}
      · <span class="${a.status.startsWith("failed") ? "fail" : ""}">${esc(a.status)}</span></div>${esc(a.message)}</li>`).join("") || "<li class='muted'>No messages yet</li>";
}
$("#subList").addEventListener("click", async (e) => {
  const id = e.target.dataset.del;
  if (id && confirm("Remove this subscriber?")) { await api(`/api/subscribers/${id}`, { method: "DELETE" }); loadAlerts(); }
});
$("#subForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const body = {
    name: f.get("name") || null, phone: f.get("phone"), sms: !!f.get("sms"), whatsapp: !!f.get("whatsapp"),
    origin: f.get("origin") || null, destination: f.get("destination") || null,
    watch: [...document.querySelectorAll("#watchList input:checked")].map((i) => i.value),
  };
  try {
    const r = await api("/api/subscribers", { method: "POST", body: JSON.stringify(body) });
    if (r.status === "code_sent") {
      pendingPhone = r.phone;
      $("#subMsg").textContent = "";
      $("#verifyMsg").textContent = `We sent a 6-digit code by ${r.channel} to ${r.phone}. Enter it to confirm.`;
      $("#verifyForm").classList.remove("hidden");
      $("#verifyCode").focus();
    } else {
      $("#subMsg").textContent = `✅ Subscribed ${r.subscriber.phone}`;
      if (META.admin) loadAlerts();
    }
  } catch (err) { $("#subMsg").textContent = "❌ " + err.message; }
});
let pendingPhone = null;
$("#verifyForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await api("/api/subscribers/verify", { method: "POST", body: JSON.stringify({ phone: pendingPhone, code: $("#verifyCode").value }) });
    $("#verifyForm").classList.add("hidden");
    $("#verifyCode").value = "";
    $("#subMsg").textContent = `✅ Subscribed ${r.subscriber.phone}. You'll get alerts when your junctions get busy.`;
    if (META.admin) loadAlerts();
  } catch (err) { $("#verifyMsg").textContent = "❌ " + err.message; }
});

// ---------------------------------------------------- connect phones (admin)
function renderShare() {
  const { public_url, lan_urls } = META.share || {};
  const url = public_url || "";
  $("#shareBody").innerHTML = url
    ? `<p>Phones open this link (or scan the code), then tap <b>Install app</b>:</p>
       <p class="share-url"><a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a></p><div class="qr" id="qr"></div>`
    : `<p>No public <b>https</b> link yet, so phones can view the app on the same Wi-Fi but can't install it.
       See <i>Install on phones</i> in the README to switch on a secure link.</p>`;
  if (lan_urls?.length) {
    $("#shareBody").insertAdjacentHTML("beforeend", `<p class="muted small">Same Wi-Fi (view only): ${lan_urls.map(esc).join(", ")}</p>`);
  }
  if (url && window.QRCode) new QRCode($("#qr"), { text: url, width: 180, height: 180 });
}
$("#testBtn").addEventListener("click", async () => {
  const phone = document.querySelector("[name=phone]").value;
  try { const r = await api("/api/notify/test", { method: "POST", body: JSON.stringify({ phone }) }); $("#subMsg").textContent = "Test: " + JSON.stringify(r); loadAlerts(); }
  catch (err) { $("#subMsg").textContent = "❌ " + err.message; }
});
$("#incidentBtn").addEventListener("click", async () => {
  try { await api("/api/simulate/incident", { method: "POST", body: JSON.stringify({ intersection_id: $("#incidentAt").value, minutes: 15 }) }); alert("Incident started — watch the map turn red."); }
  catch (err) { alert(err.message); }
});

// ------------------------------------------------------------------ bot
function botSay(text, who = "bot") {
  const d = document.createElement("div");
  d.className = "msg " + who;
  d.innerHTML = esc(text).replace(/\*(.+?)\*/g, "<b>$1</b>");
  $("#chat").appendChild(d);
  $("#chat").scrollTop = $("#chat").scrollHeight;
}
$("#chatForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#chatInput").value.trim();
  if (!text) return;
  botSay(text, "me");
  $("#chatInput").value = "";
  const phone = document.querySelector("[name=phone]").value || "+26650000000";
  try { botSay((await api("/api/bot/chat", { method: "POST", body: JSON.stringify({ phone, text }) })).reply); }
  catch (err) { botSay("⚠️ " + err.message); }
});

// ------------------------------------------------------ counting lines
$("#editLines").addEventListener("click", async () => {
  if (editing) return stopEditing();
  editing = true;
  draftLines = await api(`/api/cameras/${selected}/lines`);
  $("#lineEditor").classList.remove("hidden");
  $("#videoWrap").classList.add("editing");
  drawDraft();
});
function stopEditing() {
  editing = false; pendingPoint = null;
  $("#lineEditor").classList.add("hidden");
  $("#videoWrap").classList.remove("editing");
  $("#lineOverlay").innerHTML = "";
}
function drawDraft() {
  $("#lineOverlay").innerHTML = draftLines.map((l) =>
    `<line x1="${l.points[0]}" y1="${l.points[1]}" x2="${l.points[2]}" y2="${l.points[3]}" stroke="#f59e0b" stroke-width="0.008"/>`).join("") +
    (pendingPoint ? `<circle cx="${pendingPoint[0]}" cy="${pendingPoint[1]}" r="0.01" fill="#f59e0b"/>` : "");
  $("#lineList").innerHTML = draftLines.map((l, i) => `<li>${esc(l.approach)} · ${esc(l.count_direction)} <a href="#" data-rm="${i}">remove</a></li>`).join("");
}
$("#lineList").addEventListener("click", (e) => { if (e.target.dataset.rm) { e.preventDefault(); draftLines.splice(+e.target.dataset.rm, 1); drawDraft(); } });
$("#video").addEventListener("click", (e) => {
  if (!editing) return;
  const img = e.target, r = img.getBoundingClientRect();
  // account for object-fit: contain letterboxing
  const ratio = img.naturalWidth / img.naturalHeight || 16 / 9;
  let w = r.width, h = r.width / ratio;
  if (h > r.height) { h = r.height; w = h * ratio; }
  const ox = (r.width - w) / 2, oy = (r.height - h) / 2;
  const x = (e.clientX - r.left - ox) / w, y = (e.clientY - r.top - oy) / h;
  if (x < 0 || x > 1 || y < 0 || y > 1) return;
  // overlay uses the full box, convert back for display
  const pt = [+(x).toFixed(4), +(y).toFixed(4)];
  if (!pendingPoint) { pendingPoint = pt; }
  else {
    draftLines.push({ approach: $("#lineApproach").value, count_direction: $("#lineDir").value, points: [...pendingPoint, ...pt] });
    pendingPoint = null;
  }
  drawDraft();
});
$("#saveLines").addEventListener("click", async () => {
  await api(`/api/cameras/${selected}/lines`, { method: "PUT", body: JSON.stringify(draftLines) });
  stopEditing();
});
$("#clearLines").addEventListener("click", () => { draftLines = []; pendingPoint = null; drawDraft(); });

// ------------------------------------------------------- install as app
// Windows (Edge/Chrome) and Android (Chrome) offer a real install prompt; iOS installs from Safari's
// Share menu. Installing requires HTTPS, except on localhost.
const isStandalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
let installEvent = null;

if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("sw.js").catch((e) => console.warn("Service worker not registered:", e));
}
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  installEvent = e;
  $("#installBtn").classList.remove("hidden");
});
window.addEventListener("appinstalled", () => $("#installBtn").classList.add("hidden"));
if (!isStandalone && (isIOS || !window.isSecureContext)) $("#installBtn").classList.remove("hidden");

$("#installBtn").addEventListener("click", async () => {
  if (installEvent) {
    installEvent.prompt();
    const { outcome } = await installEvent.userChoice;
    if (outcome === "accepted") $("#installBtn").classList.add("hidden");
    installEvent = null;
    return;
  }
  const steps = !window.isSecureContext
    ? `<p>Installing needs a secure (https://) address. This page was opened over plain http, so the
       phone won't offer to install it.</p><p>Ask the person running the server for the <b>https://</b>
       link (see “Install on phones” in the README), open that, then tap Install again.</p>`
    : isIOS
      ? `<ol><li>Open this page in <b>Safari</b>.</li><li>Tap the <b>Share</b> button
         (square with an arrow).</li><li>Choose <b>Add to Home Screen</b>, then <b>Add</b>.</li></ol>`
      : `<ol><li>Open the browser menu (<b>⋮</b> or <b>…</b>).</li><li>Choose <b>Install app</b> or
         <b>Add to Home screen</b>.</li></ol>`;
  $("#installSteps").innerHTML = steps;
  $("#installHelp").showModal();
});

// ------------------------------------------------ native app (Android / iOS)
// Inside the store app, Capacitor injects window.Capacitor with the phone's native features.
const native = !!window.Capacitor?.isNativePlatform?.();
const Plugins = window.Capacitor?.Plugins || {};
document.body.classList.toggle("is-native", native);

function myTripAndJunctions() {
  const f = new FormData($("#subForm"));
  return {
    origin: f.get("origin") || null,
    destination: f.get("destination") || null,
    watch: [...document.querySelectorAll("#watchList input:checked")].map((i) => i.value),
  };
}

async function registerPush() {
  const P = Plugins.PushNotifications;
  const choice = myTripAndJunctions();
  if (!choice.watch.length && !(choice.origin && choice.destination)) {
    $("#pushMsg").textContent = "Choose your daily trip or some junctions in the form below first.";
    return;
  }
  let perm = await P.checkPermissions();
  if (perm.receive !== "granted") perm = await P.requestPermissions();
  if (perm.receive !== "granted") {
    $("#pushMsg").textContent = "Notifications are blocked. Allow them for Maseru Traffic in the phone's Settings.";
    return;
  }
  await P.createChannel?.({ id: "traffic_alerts", name: "Traffic alerts", importance: 4, description: "Heavy traffic on your trip" }).catch(() => {});
  pushChoice = choice;
  await P.register();     // the "registration" listener below sends the token + choices to the server
}

let pushChoice = null;
if (native && Plugins.PushNotifications) {
  const P = Plugins.PushNotifications;
  P.addListener("registration", async ({ value: token }) => {
    if (!pushChoice) return;     // token refresh at startup: nothing new to save
    try {
      await api("/api/devices", { method: "POST", body: JSON.stringify({ token, platform: window.Capacitor.getPlatform(), ...pushChoice }) });
      store.set("pushToken", token);
      $("#pushMsg").textContent = "✅ Alerts are on for this phone.";
      $("#pushOn").textContent = "Update my alerts";
      $("#pushOff").classList.remove("hidden");
    } catch (err) { $("#pushMsg").textContent = "❌ " + err.message; }
  });
  P.addListener("registrationError", (e) => ($("#pushMsg").textContent = "❌ Could not register: " + (e.error || e)));
  // tapping an alert opens the app on that junction's route view
  P.addListener("pushNotificationActionPerformed", ({ notification }) => {
    const iid = notification.data?.intersection;
    const open = () => { selectIntersection(iid); document.querySelector('nav.tabs [data-tab="map"]').click(); };
    if (iid) META ? open() : window.addEventListener("load", () => setTimeout(open, 1500), { once: true });
  });
  if (store.get("pushToken")) { $("#pushOn").textContent = "Update my alerts"; $("#pushOff").classList.remove("hidden"); }
  $("#pushOn").addEventListener("click", () => registerPush().catch((e) => ($("#pushMsg").textContent = "❌ " + e.message)));
  $("#pushOff").addEventListener("click", async () => {
    const token = store.get("pushToken");
    if (token) await api("/api/devices/remove", { method: "POST", body: JSON.stringify({ token }) }).catch(() => {});
    store.set("pushToken", "");
    $("#pushOff").classList.add("hidden");
    $("#pushOn").textContent = "🔔 Turn on alerts";
    $("#pushMsg").textContent = "Alerts are off for this phone.";
  });
}

// "Near me": the location stays on the phone; only the nearest junction is used.
async function myPosition() {
  if (native && Plugins.Geolocation) {
    const perm = await Plugins.Geolocation.requestPermissions().catch(() => null);
    if (perm && perm.location === "denied") throw new Error("Location is blocked. Allow it for Maseru Traffic in Settings.");
    const p = await Plugins.Geolocation.getCurrentPosition({ enableHighAccuracy: false, timeout: 15000 });
    return p.coords;
  }
  if (!navigator.geolocation) throw new Error("This device can't share its location.");
  return new Promise((ok, fail) => navigator.geolocation.getCurrentPosition((p) => ok(p.coords),
    (e) => fail(new Error(e.code === 1 ? "Location permission was refused." : "Couldn't get your location.")), { timeout: 15000 }));
}
function km(a, b) {
  const R = 6371, rad = Math.PI / 180, dLat = (b.lat - a.lat) * rad, dLon = (b.lon - a.lon) * rad;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}
$("#nearMe").addEventListener("click", async () => {
  $("#nearMeMsg").textContent = "Finding you…";
  try {
    const c = await myPosition();
    const me = { lat: c.latitude, lon: c.longitude };
    const best = META.places.map((p) => ({ p, d: km(me, p) })).sort((a, b) => a.d - b.d)[0];
    if (best.d > 25) { $("#nearMeMsg").textContent = "You seem to be outside Maseru, so pick a start point from the list."; return; }
    $("#routeFrom").value = best.p.id;
    $("#nearMeMsg").textContent = `Nearest junction: ${best.p.name} (${best.d.toFixed(1)} km away)`;
  } catch (err) { $("#nearMeMsg").textContent = "❌ " + err.message; }
});

// open a tab directly, e.g. from the app-icon shortcuts (?tab=map)
const startTab = new URLSearchParams(location.search).get("tab");
if (startTab) document.querySelector(`nav.tabs [data-tab="${CSS.escape(startTab)}"]`)?.click();

init();
