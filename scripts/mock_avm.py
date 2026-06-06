#!/usr/bin/env python3
"""A tiny fake Anthem AVM90 for local development and testing.

Speaks just enough of the x40 IP-control protocol (semicolon-terminated text on
TCP 14999) for the `anthemav` library to identify the device as an "AVM 90",
learn its inputs, and populate live state. Set commands update the in-memory
state and are echoed back, so the real UI is fully interactive against it.

    python scripts/mock_avm.py            # listen on 127.0.0.1:14999
    python scripts/mock_avm.py --port 15000

This is NOT a faithful emulation — it's a dev convenience so you can run and demo
the web app without the physical receiver on the network.
"""
from __future__ import annotations

import argparse
import asyncio

# Canned read values keyed by protocol command prefix.
STATE: dict[str, str] = {
    # Device identity
    "IDM": "AVM 90",
    "IDR": "US",
    "IDS": "1.2.3",
    "IDB": "2024-01-01",
    "IDH": "1.0",
    "EMAC": "00:11:22:33:44:55",
    "WMAC": "00:11:22:33:44:66",
    "GCTXS": "1",
    "GCFPB": "3",
    "ICN": "5",
    # Zone 1 live state
    "Z1POW": "1",
    "Z1PVOL": "45",
    "Z1VOL": "-40",
    "Z1MUT": "0",
    "Z1INP": "2",
    "Z1ALM": "14",  # -> "Dolby Surround"
    "Z2POW": "0",
    # Now-playing / status
    "Z1VIR": "14",  # 4K
    "Z1IRH": "3840",
    "Z1IRV": "2160",
    "Z1AIC": "6",  # 7.1 channel
    "Z1AIF": "3",  # Dolby
    "Z1BRT": "0",
    "Z1SRT": "48",
    "Z1AIN": "Dolby TrueHD",
    "Z1AIR": "48kHz",
    "Z1DYN": "0",
    "Z1DIA": "0",
    # Inputs (avoid the substring "IN" inside names; the parser keys on it)
    "IS1IN": "CBL/SAT",
    "IS2IN": "Apple TV",
    "IS3IN": "Blu-ray",
    "IS4IN": "Game",
    "IS5IN": "Media",
    "IS1ARC": "1",
    "IS2ARC": "1",
    "IS3ARC": "1",
    "IS4ARC": "1",
    "IS5ARC": "1",
}

# Settable prefixes, longest first so matching is unambiguous.
SET_PREFIXES = ["Z1PVOL", "Z1POW", "Z1MUT", "Z1INP", "Z1ALM", "Z1VOL", "GCTXS"]


def handle(msg: str) -> str | None:
    """Return the device's reply datagram (without trailing ';') for one message."""
    if msg.endswith("?"):
        key = msg[:-1]
        if key in STATE:
            return f"{key}{STATE[key]}"
        return None
    for prefix in SET_PREFIXES:
        if msg.startswith(prefix):
            value = msg[len(prefix):]
            STATE[prefix] = value
            return f"{prefix}{value}"
    return None


async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    print(f"client connected: {peer}")
    buffer = ""
    try:
        while True:
            data = await reader.read(1024)
            if not data:
                break
            buffer += data.decode(errors="ignore")
            while ";" in buffer:
                msg, buffer = buffer.split(";", 1)
                if not msg:
                    continue
                reply = handle(msg)
                if reply is not None:
                    writer.write(f"{reply};".encode())
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        print(f"client disconnected: {peer}")
        writer.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=14999)
    args = parser.parse_args()

    server = await asyncio.start_server(on_client, args.host, args.port)
    print(f"Mock AVM90 listening on {args.host}:{args.port}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
