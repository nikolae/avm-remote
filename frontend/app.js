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
const maxVolInput = el("set-max-vol");

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
const DEFAULT_SETTINGS = {
  volumeUnit: "db", // "db" | "pct"
  // Volume range the slider maps onto, in dB. maxVolumeDb should match the
  // receiver's configured "maximum volume" so the dB readout/slider make sense.
  // (Querying this from the unit over IP is TODO — see TODO.md.)
  maxVolumeDb: 0,
};
const VOL_FLOOR_DB = -90; // receiver mute floor (PVOL 0%)
const VOL_CEIL_DB = 0; // receiver reference max that PVOL 100% maps to

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
// The receiver's own max-volume limit (GCMMV) is authoritative when known; the
// manual setting is just a fallback for when it hasn't been reported yet.
const maxDb = () =>
  state && state.max_volume_db != null
    ? state.max_volume_db
    : Number(settings.maxVolumeDb) || 0;
// Estimate dB from a PVOL %, across the receiver's full range [floor .. ceil]
// (PVOL maps over the whole hardware range, NOT the configured max limit). Only
// a fallback for when the true volume_db hasn't been reported yet.
const estDb = (pct) =>
  Math.round(VOL_FLOOR_DB + (pct / 100) * (VOL_CEIL_DB - VOL_FLOOR_DB));

// Readout for a given state, honoring the volume-unit setting.
function volumeReadout(s) {
  if (settings.volumeUnit === "pct") return { value: String(s.volume), unit: "%" };
  const db =
    s.volume_db !== null && s.volume_db !== undefined
      ? fmtDb(s.volume_db)
      : String(estDb(s.volume));
  return { value: db, unit: "dB" };
}
// Readout for a raw slider position mid-drag, in the slider's current unit.
function sliderReadout(v) {
  return settings.volumeUnit === "pct"
    ? { value: String(Math.round(v)), unit: "%" }
    : { value: fmtDb(v), unit: "dB" };
}
// Point the slider's scale at the current unit. In dB mode the slider range IS
// [floor .. max], so it visibly rescales whenever the max-volume limit changes;
// in % mode it's the raw 0-100 PVOL.
function configureSlider(s) {
  if (settings.volumeUnit === "pct") {
    volSlider.min = 0;
    volSlider.max = 100;
    volSlider.step = 1;
    volSlider.value = s.volume;
  } else {
    volSlider.min = VOL_FLOOR_DB;
    volSlider.max = maxDb();
    volSlider.step = 0.5;
    volSlider.value = s.volume_db != null ? s.volume_db : estDb(s.volume);
  }
}
// Send a new volume from a value in the current display unit. In dB mode we set
// the true dB directly (Z1VOL) — exact, no %↔dB conversion; in % mode, PVOL.
function sendVolume(v) {
  if (settings.volumeUnit === "pct") {
    const level = Math.max(0, Math.min(100, Math.round(v)));
    optimistic({ volume: level });
    postCmd("/api/volume", { level });
  } else {
    const db = Math.max(VOL_FLOOR_DB, Math.min(maxDb(), Math.round(v * 2) / 2));
    optimistic({ volume_db: db });
    postCmd("/api/volume", { db });
  }
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
  const noSignal =
    !next.audio_format ||
    next.audio_format === "No audio" ||
    next.audio_input_name === "No Signal";
  // The listening mode is only meaningful with a live signal (the receiver locks
  // it to "None" otherwise), so hide it when there's nothing playing.
  npMode.textContent = noSignal ? "" : next.listening_mode || "";
  let parts;
  if (!next.power) {
    parts = [];
  } else if (noSignal) {
    parts = next.input_name ? ["No signal"] : [];
  } else {
    parts = [next.audio_format, next.audio_channels];
    if (next.sample_rate) parts.push(`${next.sample_rate} kHz`);
    if (next.video_resolution && next.video_resolution !== "No video") {
      parts.push(next.video_resolution);
    }
  }
  npFormat.textContent = parts.filter(Boolean).join(" · ");

  // Volume (don't fight an active drag)
  if (!draggingVolume) {
    configureSlider(next);
    setVolDisplay(volumeReadout(next));
  }
  muteBtn.classList.toggle("active", next.mute);
  muteBtn.textContent = next.mute ? "Muted" : "Mute";

  // Reflect the receiver's max-volume limit in the settings field (unless the
  // user is actively editing it).
  if (next.max_volume_db != null && document.activeElement !== maxVolInput) {
    maxVolInput.value = next.max_volume_db;
  }

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

// Nudge the volume: ±1 dB in dB mode, ±2% in % mode.
function nudge(dir) {
  if (settings.volumeUnit === "pct") {
    sendVolume((state ? state.volume : 0) + dir * 2);
  } else {
    const curDb =
      state && state.volume_db != null ? state.volume_db : estDb(state ? state.volume : 0);
    sendVolume(curDb + dir);
  }
}
el("vol-up").addEventListener("click", () => nudge(+1));
el("vol-down").addEventListener("click", () => nudge(-1));

// Live readout while dragging; commit on release.
volSlider.addEventListener("input", () => {
  draggingVolume = true;
  setVolDisplay(sliderReadout(Number(volSlider.value)));
});
function commitVolume() {
  if (!draggingVolume) return;
  draggingVolume = false;
  sendVolume(Number(volSlider.value));
}
volSlider.addEventListener("change", commitVolume);
volSlider.addEventListener("pointerup", commitVolume);

// Manual entry: tap the readout to type a target in the current display unit.
const volReadout = el("vol-readout");
const volInput = el("vol-input");
const volValueSpan = el("vol-value");

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
    if (!Number.isNaN(num)) sendVolume(num);
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

// Maximum volume: writes the limit to the receiver (GCMMV) and keeps a local
// fallback used to scale the slider until the device reports its own value.
maxVolInput.value = settings.maxVolumeDb;
maxVolInput.addEventListener("change", () => {
  const num = parseFloat(maxVolInput.value);
  if (Number.isNaN(num)) {
    maxVolInput.value = settings.maxVolumeDb; // restore on bad input
    return;
  }
  // Snap to the receiver's 0.5 dB granularity and clamp to a sane range.
  const db = Math.max(-90, Math.min(10, Math.round(num * 2) / 2));
  maxVolInput.value = db;
  settings.maxVolumeDb = db;
  saveSettings();
  postCmd("/api/max_volume", { db }); // set it on the receiver
  // Optimistically apply so the slider's dB scale recomputes immediately
  // (otherwise maxDb() keeps returning the old device value until the
  // round-trip arrives).
  if (state) {
    state.max_volume_db = db;
    render(state);
  }
});

// --- PWA service worker ------------------------------------------------------
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () =>
    navigator.serviceWorker.register("sw.js").catch(() => {})
  );
}
