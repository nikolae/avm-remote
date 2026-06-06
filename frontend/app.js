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

// --- Volume display (dB) -----------------------------------------------------
const fmtDb = (db) => (Number.isInteger(db) ? String(db) : db.toFixed(1));
// Estimate dB for a slider position during a drag (90 dB span over 0-100%); the
// receiver's true dB snaps in over the WebSocket on release.
const estDb = (pct) => Math.round((pct - 100) * 0.9);
function dbReadout(s) {
  if (s.volume_db !== null && s.volume_db !== undefined) return fmtDb(s.volume_db);
  return String(estDb(s.volume));
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
  const fmt = [next.audio_format, next.audio_channels]
    .filter(Boolean)
    .join(" · ");
  const rate = next.sample_rate ? `${next.sample_rate} kHz` : "";
  npFormat.textContent = [fmt, rate].filter(Boolean).join(" · ");

  // Volume (don't fight an active drag)
  if (!draggingVolume) {
    volSlider.value = next.volume;
    volValue.textContent = dbReadout(next);
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
  volValue.textContent = String(estDb(Number(volSlider.value)));
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

// --- WebSocket live state ----------------------------------------------------
let ws = null;
let reconnectTimer = null;

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
  ws.onclose = scheduleReconnect;
  ws.onerror = () => ws && ws.close();
}

function scheduleReconnect() {
  conn.classList.remove("online");
  conn.classList.add("offline");
  connText.textContent = "reconnecting…";
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectWS();
  }, 1500);
}

connectWS();

// Re-sync promptly when the app returns to the foreground (e.g. iPhone unlock).
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && (!ws || ws.readyState > 1)) {
    connectWS();
  }
});

// --- PWA service worker ------------------------------------------------------
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () =>
    navigator.serviceWorker.register("sw.js").catch(() => {})
  );
}
