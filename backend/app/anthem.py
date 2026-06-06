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

from .models import Input, ReceiverState

_LOGGER = logging.getLogger(__name__)

# Bound each subscriber queue so a slow/dead client can't grow memory without
# limit; we only ever care about the latest state, so we drop the oldest.
_QUEUE_MAXSIZE = 8

# Connection maintenance cadence (the maintain loop ticks every _POLL_INTERVAL).
_POLL_INTERVAL = 2.0  # seconds between loop ticks / connection-state checks
_HEARTBEAT_EVERY = 5  # ticks -> ~10s: send a liveness query
_RESYNC_EVERY = 30  # ticks -> ~60s: full state refresh to recover missed pushes
_STALE_AFTER = 30.0  # seconds without any RX -> force a reconnect


class AnthemController:
    """Maintain receiver state and broadcast changes to subscribers."""

    def __init__(self, host: str, port: int = 14999) -> None:
        self._host = host
        self._port = port
        self._conn: Optional[Connection] = None
        self._subscribers: set[asyncio.Queue[ReceiverState]] = set()
        self._task: Optional[asyncio.Task] = None
        self._maintain_task: Optional[asyncio.Task] = None
        self._last_connected: Optional[bool] = None
        self._last_rx: float = 0.0  # monotonic time of last datagram from device

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
            # The first connection_made fired inside create() before our hooks
            # were installed, so prime liveness/keepalive for it explicitly.
            self._install_hooks()
            self._last_rx = time.monotonic()
            self._configure_socket()
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

    def _install_hooks(self) -> None:
        """Wrap the protocol's asyncio callbacks to track liveness and keepalive.

        anthemav's `Connection` reuses the same protocol instance across
        reconnects, so patching once here covers every future reconnect too.
        """
        p = self._conn.protocol
        if getattr(p, "_avmremote_hooked", False):
            return

        orig_data_received = p.data_received
        orig_connection_made = p.connection_made

        def data_received(data):
            self._last_rx = time.monotonic()
            return orig_data_received(data)

        def connection_made(transport):
            result = orig_connection_made(transport)
            self._last_rx = time.monotonic()
            self._configure_socket()
            self._broadcast(self.snapshot())  # surface reconnects promptly
            return result

        p.data_received = data_received
        p.connection_made = connection_made
        p._avmremote_hooked = True

    def _configure_socket(self) -> None:
        """Enable TCP keepalive so dead/half-open links are detected quickly.

        Without this a silently-dropped connection lingers as "connected" until
        a write finally fails minutes later, freezing status updates. Keepalive
        lets the OS notice the dead peer and close the socket, which triggers
        anthemav's auto-reconnect.
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
        """Keep the link healthy and the UI's connection state current.

        Every tick: push a snapshot if the connected flag flipped. While
        connected: periodically probe liveness (a core query that the receiver
        answers even in standby), force a reconnect if the link has gone silent,
        and resync full state occasionally to recover any missed pushes.
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

            if tick % _HEARTBEAT_EVERY == 0:
                # IDM (model) is a core attribute the unit answers regardless of
                # power state, so it's a safe liveness probe.
                try:
                    p.query("IDM")
                except Exception:  # pragma: no cover - defensive
                    pass
                if self._last_rx and (time.monotonic() - self._last_rx) > _STALE_AFTER:
                    _LOGGER.warning(
                        "No data from receiver for >%.0fs; forcing reconnect",
                        _STALE_AFTER,
                    )
                    try:
                        if p.transport is not None:
                            p.transport.close()  # -> connection_lost -> reconnect
                    except Exception:  # pragma: no cover - defensive
                        pass
                    continue

            if tick % _RESYNC_EVERY == 0:
                try:
                    await p.refresh_power()
                    await p.refresh_zone(1)
                    await p.refresh_all()
                except Exception:  # pragma: no cover - defensive
                    pass

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

        return ReceiverState(
            connected=self.connected,
            model=p.model,
            power=bool(p.power),
            volume=int(p.volume),
            volume_db=volume_db,
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
        # Setting by display name maps to the model-correct ALM number internally.
        self._require_protocol().audio_listening_mode_text = mode
