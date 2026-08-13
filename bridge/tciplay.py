#!/usr/bin/env python3
"""Listen to the K3 TCI bridge from any machine.

A minimal TCI audio client: connects, subscribes, and pipes the audio to
aplay. No conversion is needed anywhere -- TCI sends float32 stereo and
ALSA accepts FLOAT_LE directly, so the payload goes straight from the
socket to the sound card after stripping the 64-byte header.

    python3 tciplay.py --host 192.168.1.198

Also prints the state messages the bridge broadcasts, so you can watch the
radio being tuned while you listen.
"""
from __future__ import annotations

import argparse
import array
import asyncio
import math
import struct
import subprocess
import sys

try:
    from websockets.asyncio.client import connect
except ImportError:
    sys.exit("needs the websockets package:\n"
             "  python3 -m venv .venv && .venv/bin/pip install websockets")

HDR = struct.Struct("<16I")
HEADER_BYTES = 64
TYPE_RX_AUDIO = 1


def bar(rms: float, width: int = 30) -> str:
    """Crude VU meter, -60..0 dBFS."""
    d = 20 * math.log10(rms) if rms > 1e-9 else -99.0
    n = max(0, min(width, int((d + 60) / 60 * width)))
    return f"[{'#' * n}{'.' * (width - n)}] {d:6.1f} dBFS"


async def main(args) -> None:
    url = f"ws://{args.host}:{args.port}"
    print(f"connecting to {url} ...")
    async with connect(url, max_size=2 ** 20) as ws:
        device = ""
        while True:
            m = await asyncio.wait_for(ws.recv(), 10.0)
            if not isinstance(m, str):
                continue
            if m.startswith("device:"):
                device = m.split(":", 1)[1].rstrip(";")
            if m.startswith(("vfo:", "modulation:")):
                print(f"  {m}")
            if m.strip().rstrip(";") == "start":
                break
        print(f"connected to {device or 'TCI server'}; starting audio\n")

        player = subprocess.Popen(
            ["aplay", "-D", args.device, "-f", "FLOAT_LE", "-r", "48000",
             "-c", "2", "-t", "raw", "--period-size", "2048", "-q"],
            stdin=subprocess.PIPE)
        await ws.send("audio_start:0;")
        if args.volume is not None:
            await ws.send(f"volume:{args.volume};")

        n = 0
        try:
            async for m in ws:
                if isinstance(m, (bytes, bytearray)):
                    raw = bytes(m)
                    if len(raw) <= HEADER_BYTES:
                        continue
                    if HDR.unpack_from(raw, 0)[6] != TYPE_RX_AUDIO:
                        continue
                    payload = raw[HEADER_BYTES:]
                    player.stdin.write(payload)
                    n += 1
                    if n % 12 == 0:                # ~2 Hz meter
                        a = array.array("f")
                        a.frombytes(payload)
                        rms = math.sqrt(sum(v * v for v in a) / len(a))
                        print(f"\r{bar(rms)}  {n} frames", end="", flush=True)
                else:
                    txt = m.strip().rstrip(";")
                    if txt.startswith(("vfo:", "modulation:", "trx:",
                                       "split_enable:")):
                        print(f"\r  {txt}{' ' * 30}")
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            print("\nstopping")
            try:
                await ws.send("audio_stop;")
            except Exception:
                pass
            if player.stdin:
                player.stdin.close()
            player.terminate()
            player.wait(timeout=3)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Listen to the K3 TCI bridge")
    p.add_argument("--host", default="192.168.1.198")
    p.add_argument("--port", type=int, default=50001)
    p.add_argument("--device", default="default", help="ALSA output device")
    p.add_argument("--volume", type=int, default=None,
                   help="TCI master volume in dB, -60..0")
    try:
        asyncio.run(main(p.parse_args()))
    except KeyboardInterrupt:
        pass
