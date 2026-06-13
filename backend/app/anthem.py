"""Controller wrapping the `anthemav` asyncio library.

Owns a single persistent connection to the AVM90's IP-control port (14999),
exposes high-level command methods, builds `ReceiverState` snapshots, and
provides a tiny pub/sub so the web layer can stream live state to browsers.

The AVM90 is an "x40" series device in anthemav terms: volume is reported and
set as a 0-100 percentage (PVOL), listening modes come from the x40 table, and
inputs are learned via IS<n>IN queries once the unit is powered on.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Optional

from anthemav.connection import Connection
from anthemav import protocol as _anthem_protocol

from .models import Input, ReceiverState

_LOGGER = logging.getLogger(__name__)

# The AVM 70/90 reports its main-zone maximum volume limit (in dB, 0.5 dB steps)
# via the GCMMV command, which anthemav doesn't know about. Register it in the
# library's lookup table (before any AVR instance is created) so it's queried by
# refresh_all, parsed, stored as protocol._GCMMV, and fires update callbacks like
# any built-in attribute. Setting is just `command("GCMMV<dB>")`.
_MAX_VOLUME_CMD = "GCMMV"
_anthem_protocol.LOOKUP.setdefault(
    _MAX_VOLUME_CMD, {"description": "Maximum main volume (dB)"}
)

# Bound each subscriber queue so a slow/dead client can't grow memory without
# limit; we only ever care about the latest state, so we drop the oldest.
_QUEUE_MAXSIZE = 8

# The maintenance loop ticks every _POLL_INTERVAL seconds; the resync cadence is
# configurable (ANTHEM_RESYNC_SECONDS) and converted to a tick count at runtime.
_POLL_INTERVAL = 2.0  # seconds between loop ticks / connection-state checks
_DEFAULT_RESYNC_SECONDS = 30.0
# Watchdog: if the receiver is powered on but we've heard nothing back for this
# long (despite the resync queries actively probing it), the link has wedged —
# reconnect. Set per-instance to ~2 missed resyncs so it can't false-fire on a
# healthy connection (whose resync refreshes it every interval). Gated on power
# so an idle/off receiver, which is legitimately quiet, is never torn down.
_STALE_FLOOR = 45.0  # never fire sooner than this, regardless of resync interval


class AnthemController:
    """Maintain receiver state and broadcast changes to subscribers."""

    def __init__(
        self,
        host: str,
        port: int = 14999,
        resync_seconds: float = _DEFAULT_RESYNC_SECONDS,
    ) -> None:
        self._host = host
        self._port = port
        # How often to re-query full state, as a number of poll-loop ticks
        # (minimum 1 tick). 0 or negative disables periodic resync.
        if resync_seconds and resync_seconds > 0:
            self._resync_every = max(1, round(resync_seconds / _POLL_INTERVAL))
            # Reconnect after ~2 missed resyncs of silence. The watchdog relies
            # on the resync as its active probe, so it's disabled without one.
            self._stale_after = max(_STALE_FLOOR, resync_seconds * 2)
        else:
            self._resync_every = 0
            self._stale_after = 0.0  # no active probe -> watchdog disabled
        self._conn: Optional[Connection] = None
        self._subscribers: set[asyncio.Queue[ReceiverState]] = set()
        self._task: Optional[asyncio.Task] = None
        self._maintain_task: Optional[asyncio.Task] = None
        self._last_connected: Optional[bool] = None
        self._last_rx: float = 0.0  # monotonic time of the last datagram received

    # --- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Begin connecting in the background and start the maintenance loop.

        We deliberately do not block here: if the receiver is unreachable the
        anthemav library retries with backoff forever, and the web server should
        come up regardless (reporting `connected: false` until it links up).
        """
        self._task = asyncio.create_task(self._connect(), name="anthem-connect")
        self._maintain_task = asyncio.create_task(
            self._maintain(), name="anthem-maintain"
        )

    async def _connect(self) -> None:
        try:
            self._conn = await Connection.create(
                host=self._host,
                port=self._port,
                auto_reconnect=True,
                update_callback=self._on_update,
            )
            # The first connection_made fired inside create() before our hook
            # was installed, so apply keepalive to it explicitly.
            self._install_hooks()
            self._last_rx = time.monotonic()
            self._configure_socket()
            # Query the max-volume limit up front (it's a config value the unit
            # answers regardless of power; refresh_all also re-queries it later).
            try:
                self._conn.protocol.query(_MAX_VOLUME_CMD)
            except Exception:  # pragma: no cover - defensive
                pass
            _LOGGER.info("Connected to Anthem at %s:%s", self._host, self._port)
            self._broadcast(self.snapshot())
        except Exception:  # pragma: no cover - defensive
            _LOGGER.exception("Failed to establish Anthem connection")

    async def stop(self) -> None:
        for task in (self._maintain_task, self._task):
            if task:
                task.cancel()
        if self._conn:
            self._conn.close()

    def force_reconnect(self) -> bool:
        """Drop the current link so anthemav opens a fresh session.

        Recovers a receiver whose control session has hung at the application
        level (TCP still up, but no data flowing). Returns True if a live
        transport was closed.
        """
        p = self._protocol
        if p is None:
            return False
        self._last_rx = time.monotonic()  # reset so the watchdog doesn't re-fire
        if p.transport is not None:
            try:
                p.transport.close()  # -> connection_lost -> auto-reconnect
                return True
            except Exception:  # pragma: no cover - defensive
                pass
        return False

    def _install_hooks(self) -> None:
        """Patch the protocol to track liveness, keep keepalive, and fix a read bug.

        anthemav's `Connection` reuses the same protocol instance across
        reconnects, so patching once here covers every future reconnect too.
        """
        p = self._conn.protocol
        if getattr(p, "_avmremote_hooked", False):
            return

        orig_connection_made = p.connection_made
        orig_data_received = p.data_received

        def connection_made(transport):
            result = orig_connection_made(transport)
            self._last_rx = time.monotonic()
            self._configure_socket()
            self._broadcast(self.snapshot())  # surface reconnects promptly
            return result

        def data_received(data):
            self._last_rx = time.monotonic()
            return orig_data_received(data)

        async def assemble_buffer():
            # Reimplements anthemav's _assemble_buffer so a parse error can't
            # wedge the read side. The stock version calls pause_reading(),
            # parses, then resume_reading() with no guard — if any datagram
            # throws mid-parse, reading stays paused forever (socket up, writes
            # work, but no data is ever read again). Here we always resume, and
            # isolate failures to the single offending datagram.
            if p.transport is None:
                return
            p.transport.pause_reading()
            data, p.buffer = p.buffer, ""
            try:
                for message in data.split(";"):
                    try:
                        if message != "":
                            await p._parse_message(message)
                        elif p._last_command != "":
                            last_command = p._last_command
                            p._last_command = ""
                            await p._parse_message(last_command)
                    except Exception:  # pragma: no cover - defensive
                        _LOGGER.warning(
                            "Error parsing datagram %r; skipping", message,
                            exc_info=True,
                        )
            finally:
                if p.transport is not None:
                    try:
                        p.transport.resume_reading()
                    except RuntimeError:
                        pass  # already reading

        p.connection_made = connection_made
        p.data_received = data_received
        p._assemble_buffer = assemble_buffer
        p._avmremote_hooked = True

    def _configure_socket(self) -> None:
        """Enable TCP keepalive so a genuinely dead link is detected by the OS.

        This is the *safe* way to notice a dropped connection: the kernel probes
        an idle socket and, only if the peer is truly gone, closes it — which
        triggers anthemav's auto-reconnect. Unlike an application-level "no data
        in N seconds" timer, it never tears down a connection to a receiver that
        is simply idle or powered off (which would break commands like power-on).
        """
        p = self._protocol
        if p is None or p.transport is None:
            return
        sock = p.transport.get_extra_info("socket")
        if sock is None:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # Linux-specific tuning (present in the container); skipped elsewhere.
            for name, value in (
                ("TCP_KEEPIDLE", 15),
                ("TCP_KEEPINTVL", 5),
                ("TCP_KEEPCNT", 3),
            ):
                opt = getattr(socket, name, None)
                if opt is not None:
                    sock.setsockopt(socket.IPPROTO_TCP, opt, value)
        except OSError:  # pragma: no cover - platform dependent
            pass

    async def _maintain(self) -> None:
        """Keep the UI's connection state current and state fresh.

        Every tick, push a snapshot if the connected flag flipped. While
        connected, periodically re-query state as a safety net in case a pushed
        update was missed. We deliberately do NOT run an application-level
        liveness timer that force-closes "silent" connections: an idle or
        powered-off receiver is legitimately quiet, and tearing the link down
        would drop commands like power-on. Dead sockets are handled by TCP
        keepalive (see _configure_socket) plus anthemav's own auto-reconnect.
        """
        tick = 0
        while True:
            await asyncio.sleep(_POLL_INTERVAL)

            connected = self.connected
            if connected != self._last_connected:
                self._last_connected = connected
                self._broadcast(self.snapshot())

            if not connected:
                continue

            tick += 1
            p = self._conn.protocol

            if self._resync_every and tick % self._resync_every == 0:
                try:
                    await p.refresh_power()
                    await p.refresh_zone(1)
                    await p.refresh_all()
                except Exception:  # pragma: no cover - defensive
                    pass

            # Read-stall watchdog: if the unit is powered on but the resync
            # queries above have gone unanswered for too long, the read side has
            # wedged — force a reconnect to recover. (Powered-off receivers are
            # legitimately silent, so we leave those alone.)
            if (
                self._stale_after
                and bool(p.power)
                and self._last_rx
                and (time.monotonic() - self._last_rx) > self._stale_after
            ):
                _LOGGER.warning(
                    "No data from receiver for >%.0fs while powered on; reconnecting",
                    self._stale_after,
                )
                self.force_reconnect()

    # --- pub/sub --------------------------------------------------------------

    def subscribe(self) -> asyncio.Queue[ReceiverState]:
        queue: asyncio.Queue[ReceiverState] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[ReceiverState]) -> None:
        self._subscribers.discard(queue)

    def _broadcast(self, state: ReceiverState) -> None:
        for queue in self._subscribers:
            if queue.full():
                # Drop the stale snapshot so the newest one always gets through.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(state)
            except asyncio.QueueFull:  # pragma: no cover - race, harmless
                pass

    def _on_update(self, _message: str) -> None:
        """anthemav update_callback: scheduled on the loop, so it's safe here."""
        self._broadcast(self.snapshot())

    # --- state ----------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return bool(self._conn and self._conn.protocol.transport is not None)

    @property
    def _protocol(self):
        return self._conn.protocol if self._conn else None

    def snapshot(self) -> ReceiverState:
        """Build a ReceiverState from the current protocol attributes.

        Tolerant of a missing/uninitialised protocol so it always returns a
        renderable state.
        """
        p = self._protocol
        if p is None:
            return ReceiverState(connected=False)

        zone = p.zones[1]

        # Inputs are stored as {number: name}; expose sorted by number.
        inputs = [
            Input(number=num, name=name)
            for num, name in sorted(p._input_names.items())
            if name
        ]

        listening_mode = self._listening_mode_text(p)

        # The receiver reports true volume in dB via Z1VOL. We parse the raw zone
        # value ourselves because anthemav's `attenuation` getter uses int() and
        # would choke on half-dB steps (e.g. "-40.5").
        volume_db: Optional[float] = None
        raw_vol = zone.values.get("VOL")
        if raw_vol:
            try:
                volume_db = float(raw_vol)
            except ValueError:
                volume_db = None

        # Max volume limit (GCMMV), registered in the library's lookup above so
        # its value lands on protocol._GCMMV. Half-dB steps -> parse as float.
        max_volume_db: Optional[float] = None
        raw_max = getattr(p, f"_{_MAX_VOLUME_CMD}", "")
        if raw_max:
            try:
                max_volume_db = float(raw_max)
            except ValueError:
                max_volume_db = None

        return ReceiverState(
            connected=self.connected,
            model=p.model,
            power=bool(p.power),
            volume=int(p.volume),
            volume_db=volume_db,
            max_volume_db=max_volume_db,
            mute=bool(zone.mute),
            input_number=int(zone.input_number),
            input_name=zone.input_name if inputs else "",
            inputs=inputs,
            listening_mode=listening_mode,
            listening_modes=p.audio_listening_mode_list or [],
            audio_format=p.audio_input_format_text or "",
            audio_channels=p.audio_input_channels_text or "",
            audio_input_name=p.audio_input_name or "",
            sample_rate=p.audio_input_samplerate,
            video_resolution=p.video_input_resolution_text or "",
        )

    @staticmethod
    def _listening_mode_text(p) -> str:
        """Resolve the current listening mode name using the model's ALM table.

        anthemav has a quirk on x40 units (AVM 70/90): the *list* of modes comes
        from the x40 table while `audio_listening_mode_text` is decoded with the
        older x20 table, so the current mode often doesn't match any list entry
        (e.g. it shows "PLII Music" for what is really "DTS neural:X"). We instead
        reverse-map the raw numeric value through `_alm_number`, which the library
        sets to the correct table per model, guaranteeing the result is one of
        `audio_listening_mode_list`.
        """
        raw = p.audio_listening_mode  # e.g. "04"
        alm = getattr(p, "_alm_number", None) or {}
        try:
            num = int(raw)
        except (TypeError, ValueError):
            return p.audio_listening_mode_text or ""
        for name, number in alm.items():
            if number == num:
                return name
        return p.audio_listening_mode_text or ""

    # --- commands -------------------------------------------------------------

    def _require_protocol(self):
        p = self._protocol
        if p is None or not self.connected:
            raise ConnectionError("Receiver is not connected")
        return p

    def set_power(self, on: bool) -> None:
        self._require_protocol().power = on

    def set_volume(self, level: int) -> None:
        self._require_protocol().volume = max(0, min(100, level))

    def set_volume_db(self, db: float) -> None:
        # Set the true volume in dB directly (Z1VOL). The unit accepts this and
        # uses 0.5 dB steps, so we snap and send one decimal (e.g. "Z1VOL-50.0").
        # This avoids the lossy %↔dB conversion the percentage path needs.
        p = self._require_protocol()
        db = max(-90.0, min(10.0, round(db * 2) / 2))
        p.command(f"Z1VOL{db:.1f}")
        p.query("Z1VOL")  # read back to confirm

    def step_volume(self, step: int) -> None:
        p = self._require_protocol()
        p.volume = max(0, min(100, int(p.volume) + step))

    def set_mute(self, on: bool) -> None:
        self._require_protocol().zones[1].mute = on

    def toggle_mute(self) -> None:
        p = self._require_protocol()
        p.zones[1].mute = not bool(p.zones[1].mute)

    def set_input(self, number: int) -> None:
        self._require_protocol().zones[1].input_number = number

    def set_listening_mode(self, mode: str) -> None:
        # NOTE: we can't use anthemav's audio_listening_mode_text setter — it
        # zero-pads the value (e.g. "Z1ALM06"), which the AVM 70/90 rejects with
        # !E. The x40 firmware wants the un-padded form ("Z1ALM6"), verified on
        # the unit. Send it directly using the model's ALM number table.
        p = self._require_protocol()
        alm = getattr(p, "_alm_number", None) or {}
        if mode not in alm:
            return
        p.command(f"Z1ALM{alm[mode]}")
        p.query("Z1ALM")  # read back to confirm

    def set_max_volume_db(self, db: float) -> None:
        # The receiver expects one decimal place (0.5 dB steps), e.g. "GCMMV-20.0".
        p = self._require_protocol()
        p.command(f"{_MAX_VOLUME_CMD}{db:.1f}")
        p.query(_MAX_VOLUME_CMD)  # read back to confirm / refresh state
