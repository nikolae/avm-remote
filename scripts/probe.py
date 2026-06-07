#!/usr/bin/env python3
"""Milestone-1 connectivity probe for the Anthem AVM90.

Connects to the receiver's IP-control port via the `anthemav` library, waits for
the device to identify itself, and dumps the live state we plan to surface in the
web app. Use this to confirm the library actually talks to *your* unit before
running the full server.

Usage:
    pip install anthemav
    python scripts/probe.py 10.125.200.128
    python scripts/probe.py 10.125.200.128 --port 14999 --watch

With --watch it stays connected and prints every state change (try nudging the
volume on the physical remote to see updates stream in).
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from anthemav.connection import Connection


def describe(protocol) -> str:
    zone = protocol.zones[1]
    inputs = ", ".join(
        f"{n}:{name}" for n, name in sorted(protocol._input_names.items()) if name
    )
    lines = [
        f"model              : {protocol.model}",
        f"connected          : {protocol.transport is not None}",
        f"power              : {protocol.power}",
        f"volume (0-100)     : {protocol.volume}",
        f"mute               : {zone.mute}",
        f"input number       : {zone.input_number}",
        f"input name         : {zone.input_name}",
        f"inputs             : {inputs or '(none yet)'}",
        f"listening mode     : {protocol.audio_listening_mode_text}",
        f"listening modes    : {protocol.audio_listening_mode_list}",
        f"audio format       : {protocol.audio_input_format_text}",
        f"audio channels     : {protocol.audio_input_channels_text}",
        f"audio input name   : {protocol.audio_input_name}",
        f"sample rate (kHz)  : {protocol.audio_input_samplerate}",
        f"video resolution   : {protocol.video_input_resolution_text}",
    ]
    return "\n".join(lines)


# Now-playing / status commands we query and show in the app, by display name.
DIAG_COMMANDS = [
    ("Z1AIF", "audio input format"),
    ("Z1AIC", "audio input channels"),
    ("Z1AIN", "audio input name"),
    ("Z1AIR", "audio input rate name"),
    ("Z1SRT", "audio sample rate (kHz)"),
    ("Z1BRT", "audio bitrate (kbps)"),
    ("Z1VIR", "video input resolution"),
    ("Z1IRH", "video horizontal res"),
    ("Z1IRV", "video vertical res"),
    ("Z1ALM", "audio listening mode"),
]


async def run_listen(protocol) -> None:
    """Print every datagram the receiver pushes, after the initial sync settles.

    The cleanest way to discover an unknown command: run this, then change the
    setting on the receiver (web UI or front panel) and watch what it emits.
    """
    print("\n=== Listen mode ===")
    print("Waiting for the initial sync to settle...")
    await asyncio.sleep(3)
    print(
        "Ready. Now change the setting on the receiver (e.g. the maximum volume "
        "in the web UI) and watch what appears below. Ctrl-C to stop.\n"
    )

    original_data_received = protocol.data_received

    def tap(data):
        try:
            for msg in data.decode(errors="ignore").split(";"):
                if msg.strip():
                    print(f"  {msg}")
        except Exception:
            pass
        return original_data_received(data)

    protocol.data_received = tap
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        protocol.data_received = original_data_received


async def run_raw(protocol, commands: list[str]) -> None:
    """Send arbitrary raw commands and print exactly what the receiver replies.

    Use this to discover the receiver's real behavior — e.g. to learn how it
    numbers listening modes, change the mode on the front panel and query it:

        --raw 'Z1ALM?'          # see the current mode number
        --raw 'Z1ALM03'         # try selecting a mode, watch the reply
    """
    captured: list[str] = []
    original_data_received = protocol.data_received

    def tap(data):
        try:
            captured.append(data.decode(errors="ignore"))
        except Exception:
            pass
        return original_data_received(data)

    protocol.data_received = tap

    print("\n=== Raw command test ===")
    for command in commands:
        captured.clear()
        protocol.command(command.rstrip(";"))
        await asyncio.sleep(0.5)
        raw = "".join(captured).strip()
        reply = repr(raw) if raw else "(none)"
        print(f"  sent {command!r:18} -> reply {reply}")

    protocol.data_received = original_data_received


async def run_diag(protocol) -> None:
    """Query each status command and report the raw reply from the receiver.

    This is the definitive test for the "now-playing is blank" problem: it shows,
    per command, whether the unit answered with a value, rejected it as an invalid
    command, or stayed silent. Run with audio actually playing.
    """
    # Capture the raw datagram stream so we can see exactly what comes back,
    # including "!I" (invalid command) replies the library would otherwise hide.
    captured: list[str] = []
    original_data_received = protocol.data_received

    def tap(data):
        try:
            captured.append(data.decode(errors="ignore"))
        except Exception:
            pass
        return original_data_received(data)

    protocol.data_received = tap

    print("\n=== Status command diagnostic ===")
    print("Querying each command (play audio for meaningful values)...\n")

    for command, label in DIAG_COMMANDS:
        captured.clear()
        setattr(protocol, f"_{command}", "")  # clear any cached value
        protocol.query(command)
        await asyncio.sleep(0.4)
        raw = "".join(captured).strip()
        parsed = getattr(protocol, f"_{command}", "")
        # Classify the result for quick scanning.
        if f"!I{command}" in raw or "Invalid" in raw:
            verdict = "INVALID COMMAND (not supported on this model)"
        elif raw == "":
            verdict = "NO REPLY"
        else:
            verdict = f"raw={raw!r} parsed={parsed!r}"
        print(f"  {command:7} {label:24} -> {verdict}")

    protocol.data_received = original_data_received
    print(
        "\nIf commands show INVALID/NO REPLY, this firmware uses different status\n"
        "commands than anthemav sends — share this output and we'll map them."
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="Receiver IP address or hostname")
    parser.add_argument("--port", type=int, default=14999)
    parser.add_argument(
        "--watch", action="store_true", help="Stay connected and stream updates"
    )
    parser.add_argument("--debug", action="store_true", help="Verbose protocol logs")
    parser.add_argument(
        "--diag",
        action="store_true",
        help="Probe each now-playing status command and dump the raw replies "
        "(run with audio actually playing on the receiver)",
    )
    parser.add_argument(
        "--raw",
        nargs="+",
        metavar="CMD",
        help="Send raw protocol command(s) and print the reply, e.g. "
        "--raw 'Z1ALM?' 'Z1ALM03' 'Z1ALM?' (no trailing ';'). Great for "
        "discovering how your unit numbers listening modes.",
    )
    parser.add_argument(
        "--listen",
        action="store_true",
        help="After the initial sync, print every datagram the receiver pushes. "
        "Change a setting in the web UI / front panel to discover its command.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING)

    def on_update(message: str) -> None:
        if args.watch:
            print(f"\n--- update ({message}) ---\n{describe(conn.protocol)}")

    print(f"Connecting to {args.host}:{args.port} ...")
    conn = await Connection.create(
        host=args.host,
        port=args.port,
        auto_reconnect=False,
        update_callback=on_update,
    )

    # With auto_reconnect=False the factory doesn't open the socket itself, so
    # connect explicitly here (this raises immediately if the host is unreachable).
    try:
        await conn.reconnect()
    except OSError as exc:
        print(f"\n!! Could not connect to {args.host}:{args.port}: {exc}")
        return

    try:
        await conn.protocol.wait_for_device_initialised(timeout=10)
    except Exception:
        print(
            "\n!! Device did not identify itself within 10s.\n"
            "   Check the IP, that port 14999 IP control is enabled on the unit,\n"
            "   and that no other app is holding the single allowed connection."
        )
        conn.close()
        return

    # Give the unit a moment to report inputs / now-playing after power state syncs.
    await asyncio.sleep(2)
    print("\n=== Receiver state ===")
    print(describe(conn.protocol))

    if args.diag:
        await run_diag(conn.protocol)

    if args.raw:
        await run_raw(conn.protocol, args.raw)

    if args.listen:
        await run_listen(conn.protocol)

    if args.watch:
        print("\nWatching for changes (Ctrl-C to quit) ...")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

    conn.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
