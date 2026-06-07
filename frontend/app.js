"use strict";

// --- DOM refs ----------------------------------------------------------------
const el = (id) => document.getElementById(id);
const conn = el("conn");
const connText = el("conn-text");
const powerBtn = el("power");
const powerLabel = el("power-label");
const npInput = el("np-input");
const npMode = el("np-mode");
const npFormat = el("np-format");
const volValue = el("vol-value");
const volSlider = el("vol-slider");
const muteBtn = el("mute");
const inputsEl = el("inputs");
const modesEl = el("modes");

// Latest known state from the server.
let state = null;
// True while the user is dragging the volume slider, so incoming state updates
// don't yank the thumb out from under their finger.
let draggingVolume = false;

// --- Commands ----------------------------------------------------------------
// Fire-and-forget: the receiver applies the change asynchronously and the real
// post-change state arrives over the WebSocket a moment later. We deliberately
// ignore the (intentionally stale) REST response body to avoid UI flicker.
async function postCmd(path, body) {
  try {
    await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  } catch (err) {
    // Network blip — the WebSocket will resync us shortly.
    console.warn("command failed", path, err);
  }
}

// Apply an immediate local change for snappy feedback; the WebSocket reconciles.
function optimistic(patch) {
  if (state) render({ ...state, ...patch });
}

// --- Settings (persisted in localStorage) ------------------------------------
const SETTINGS_KEY = "avm-remote-settings";
const DEFAULT_SETTINGS = { volumeUnit: "db" }; // "db" | "pct"

function loadSettings() {
  try {
    return { ...DEFAULT_SETTINGS, ...JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") };
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}
function saveSettings() {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    /* private mode / quota — settings just won't persist */
  }
}
const settings = loadSettings();

// --- Volume display ----------------------------------------------------------
const fmtDb = (db) => (Number.isInteger(db) ? String(db) : db.toFixed(1));
// Estimate dB for a slider position during a drag (90 dB span over 0-100%); the
// receiver's true dB snaps in over the WebSocket on release.
const estDb = (pct) => Math.round((pct - 100) * 0.9);

// Readout for a given state, honoring the volume-unit setting.
function volumeReadout(s) {
  if (settings.volumeUnit === "pct") return { value: String(s.volume), unit: "%" };
  const db =
    s.volume_db !== null && s.volume_db !== undefined
      ? fmtDb(s.volume_db)
      : String(estDb(s.volume));
  return { value: db, unit: "dB" };
}
// Readout for a slider position mid-drag (no receiver value yet).
function dragReadout(pct) {
  if (settings.volumeUnit === "pct") return { value: String(pct), unit: "%" };
  return { value: String(estDb(pct)), unit: "dB" };
}
const volUnit = document.querySelector(".vol-unit");
function setVolDisplay({ value, unit }) {
  volValue.textContent = value;
  volUnit.textContent = unit;
}

// --- Rendering ---------------------------------------------------------------
function render(next) {
  state = next;

  // Connection indicator
  conn.classList.toggle("online", next.connected);
  conn.classList.toggle("offline", !next.connected);
  connText.textContent = next.connected
    ? next.model || "connected"
    : "disconnected";

  // Power
  powerBtn.classList.toggle("on", next.power);
  powerLabel.textContent = next.power ? "On" : "Off";

  // Now playing
  npInput.textContent = next.input_name || (next.power ? "—" : "Standby");
  npMode.textContent = next.listening_mode || "";
  const parts = [next.audio_format, next.audio_channels];
  if (next.sample_rate) parts.push(`${next.sample_rate} kHz`);
  if (next.video_resolution && next.video_resolution !== "No video") {
    parts.push(next.video_resolution);
  }
  npFormat.textContent = parts.filter(Boolean).join(" · ");

  // Volume (don't fight an active drag)
  if (!draggingVolume) {
    volSlider.value = next.volume;
    setVolDisplay(volumeReadout(next));
  }
  muteBtn.classList.toggle("active", next.mute);
  muteBtn.textContent = next.mute ? "Muted" : "Mute";

  renderPills(
    inputsEl,
    next.inputs,
    (i) => i.name,
    (i) => i.number === next.input_number,
    (i) => {
      optimistic({ input_number: i.number, input_name: i.name });
      postCmd("/api/input", { number: i.number });
    }
  );
  renderPills(
    modesEl,
    next.listening_modes,
    (m) => m,
    (m) => m === next.listening_mode,
    (m) => {
      optimistic({ listening_mode: m });
      postCmd("/api/mode", { mode: m });
    }
  );

  // Dim the lower controls when powered off.
  document.querySelectorAll(".volume, .now-playing").forEach((c) =>
    c.classList.toggle("dimmed", !next.power)
  );
}

function renderPills(container, items, labelOf, isActiveOf, onClick) {
  container.innerHTML = "";
  if (!items || items.length === 0) {
    const span = document.createElement("div");
    span.className = "empty";
    span.textContent = state && state.power ? "—" : "Power on to load";
    container.appendChild(span);
    return;
  }
  for (const item of items) {
    const btn = document.createElement("button");
    btn.className = "pill" + (isActiveOf(item) ? " active" : "");
    btn.textContent = labelOf(item);
    btn.addEventListener("click", () => onClick(item));
    container.appendChild(btn);
  }
}

// --- Event handlers ----------------------------------------------------------
powerBtn.addEventListener("click", () => {
  const on = !(state && state.power);
  optimistic({ power: on });
  postCmd("/api/power", { on });
});

muteBtn.addEventListener("click", () => {
  optimistic({ mute: !(state && state.mute) });
  postCmd("/api/mute", {});
});

function nudge(step) {
  const cur = state ? state.volume : 0;
  optimistic({ volume: Math.max(0, Math.min(100, cur + step)) });
  postCmd("/api/volume", { step });
}
el("vol-up").addEventListener("click", () => nudge(2));
el("vol-down").addEventListener("click", () => nudge(-2));

// Live readout while dragging; commit on release.
volSlider.addEventListener("input", () => {
  draggingVolume = true;
  setVolDisplay(dragReadout(Number(volSlider.value)));
});
function commitVolume() {
  if (!draggingVolume) return;
  draggingVolume = false;
  const level = Number(volSlider.value);
  optimistic({ volume: level });
  postCmd("/api/volume", { level });
}
volSlider.addEventListener("change", commitVolume);
volSlider.addEventListener("pointerup", commitVolume);

// Manual entry: tap the readout to type a target in the current display unit.
const volReadout = el("vol-readout");
const volInput = el("vol-input");
const volValueSpan = el("vol-value");

function pctFromTyped(num) {
  // Interpret the typed value in whatever unit is shown, return a 0-100 level.
  if (settings.volumeUnit === "pct") return Math.round(num);
  // dB -> % using the same linear map the readout estimate uses (inverse of estDb).
  return Math.round(num / 0.9 + 100);
}

function openVolumeEntry() {
  if (!state) return;
  volInput.value =
    settings.volumeUnit === "pct"
      ? String(state.volume)
      : volValueSpan.textContent.replace(/[^\d.-]/g, "");
  volValueSpan.hidden = true;
  volUnit.hidden = true;
  volInput.hidden = false;
  volInput.focus();
  volInput.select();
}

function closeVolumeEntry(commit) {
  if (volInput.hidden) return;
  if (commit) {
    const num = parseFloat(volInput.value);
    if (!Number.isNaN(num)) {
      const level = Math.max(0, Math.min(100, pctFromTyped(num)));
      optimistic({ volume: level });
      postCmd("/api/volume", { level });
    }
  }
  volInput.hidden = true;
  volValueSpan.hidden = false;
  volUnit.hidden = false;
  if (state) render(state); // restore the readout to the (optimistic) value
}

volReadout.addEventListener("click", (e) => {
  if (e.target === volInput) return; // don't re-trigger while editing
  openVolumeEntry();
});
volInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") closeVolumeEntry(true);
  else if (e.key === "Escape") closeVolumeEntry(false);
});
volInput.addEventListener("blur", () => closeVolumeEntry(true));

// --- Live state: WebSocket (low latency) + REST poll (resilient fallback) -----
// The WebSocket gives instant updates, but mobile browsers drop it freely. So we
// ALSO poll /api/state on an interval: this keeps the readout/power state fresh
// and the connection indicator accurate even when the socket is down. The
// indicator reflects the *backend's* link to the receiver (state.connected) and
// whether the backend itself is reachable — never the WS's transient state.
const POLL_MS = 4000;
let ws = null;
let reconnectTimer = null;

function showServerUnreachable() {
  conn.classList.remove("online");
  conn.classList.add("offline");
  connText.textContent = "no server";
}

async function pollState() {
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    if (res.ok) render(await res.json());
    else showServerUnreachable();
  } catch {
    showServerUnreachable(); // backend not reachable at all
  }
}

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    try {
      render(JSON.parse(ev.data));
    } catch (e) {
      console.warn("bad ws message", e);
    }
  };
  // A dropped socket is normal and non-fatal — the poll keeps us live while we
  // quietly re-establish it. We deliberately do NOT flip the indicator here.
  ws.onclose = scheduleReconnect;
  ws.onerror = () => ws && ws.close();
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectWS();
  }, 1500);
}

connectWS();
pollState();
setInterval(pollState, POLL_MS);

// Reconnect/refresh promptly when the app returns to the foreground (iPhone unlock).
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    pollState();
    if (!ws || ws.readyState > 1) connectWS();
  }
});

// --- Settings panel ----------------------------------------------------------
const settingsOverlay = el("settings");
el("settings-btn").addEventListener("click", () =>
  settingsOverlay.classList.add("open")
);
el("settings-close").addEventListener("click", () =>
  settingsOverlay.classList.remove("open")
);
// Tap the dimmed backdrop (but not the sheet itself) to dismiss.
settingsOverlay.addEventListener("click", (e) => {
  if (e.target === settingsOverlay) settingsOverlay.classList.remove("open");
});

// Generic segmented control bound to a settings key; re-renders on change.
function initSegmented(id, key) {
  const seg = el(id);
  const sync = () =>
    seg.querySelectorAll("button").forEach((b) =>
      b.classList.toggle("active", b.dataset.value === settings[key])
    );
  seg.querySelectorAll("button").forEach((btn) =>
    btn.addEventListener("click", () => {
      settings[key] = btn.dataset.value;
      saveSettings();
      sync();
      if (state) render(state); // reflect the new unit immediately
    })
  );
  sync();
}
initSegmented("seg-volume-unit", "volumeUnit");

// --- PWA service worker ------------------------------------------------------
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () =>
    navigator.serviceWorker.register("sw.js").catch(() => {})
  );
}
