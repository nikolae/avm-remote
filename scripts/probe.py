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


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="Receiver IP address or hostname")
    parser.add_argument("--port", type=int, default=14999)
    parser.add_argument(
        "--watch", action="store_true", help="Stay connected and stream updates"
    )
    parser.add_argument("--debug", action="store_true", help="Verbose protocol logs")
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
