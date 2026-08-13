#!/usr/bin/env python3
"""Exercise the TCI audio path as a client would.

RX: subscribes with audio_start, then checks frame rate, header fields,
payload size, timing jitter and that the samples are real audio rather than
silence or a stuck buffer.

TX: sends a tone as TX_AUDIO frames and confirms the server accepts them.
Does NOT key the transmitter -- audio written while in receive goes nowhere,
which is exactly what makes this safe to run unattended.
"""
import asyncio
import struct
import sys
import time

import numpy as np
from websockets.asyncio.client import connect

URL = "ws://127.0.0.1:50001"
HDR = struct.Struct("<16I")
RATE, FRAMES = 48000, 2048


def parse(data):
    f = HDR.unpack_from(data, 0)
    return {"receiver": f[0], "rate": f[1], "format": f[2], "length": f[5],
            "type": f[6], "channels": f[7]}


async def init(ws):
    while True:
        m = await asyncio.wait_for(ws.recv(), 8.0)
        if isinstance(m, str) and m.strip().rstrip(";") == "start":
            return


async def main():
    fails = []
    async with connect(URL, max_size=2 ** 20) as ws:
        await init(ws)
        print("connected, handshake complete\n")

        print(">>> audio_start:0;")
        await ws.send("audio_start:0;")

        frames, texts = [], []
        t_end = time.perf_counter() + 8.0
        first = None
        while time.perf_counter() < t_end:
            try:
                m = await asyncio.wait_for(ws.recv(), 2.0)
            except (asyncio.TimeoutError, TimeoutError):
                break
            if isinstance(m, (bytes, bytearray)):
                now = time.perf_counter()
                if first is None:
                    first = now
                frames.append((now, bytes(m)))
            else:
                texts.append(m)

        print(f"    {len(frames)} binary frames in ~8 s, "
              f"{len(texts)} text messages")
        if not frames:
            print("    NO AUDIO RECEIVED")
            fails.append("no audio frames")
        else:
            hdr = parse(frames[0][1])
            payload = len(frames[0][1]) - 64
            print(f"\n    header: {hdr}")
            print(f"    frame size: {len(frames[0][1])} bytes "
                  f"({payload} payload)")

            if hdr["type"] != 1:
                fails.append(f"type is {hdr['type']}, expected 1 (RX_AUDIO)")
            if hdr["format"] != 3:
                fails.append(f"format is {hdr['format']}, expected 3 (float32)")
            if hdr["rate"] != RATE:
                fails.append(f"rate is {hdr['rate']}")
            if hdr["channels"] != 2:
                fails.append(f"channels is {hdr['channels']}")
            if hdr["length"] != FRAMES * 2:
                fails.append(f"length is {hdr['length']}, expected "
                             f"{FRAMES*2} (stereo samples, not frames)")
            if payload != hdr["length"] * 4:
                fails.append(f"payload {payload} != length*4 "
                             f"{hdr['length']*4}")

            elapsed = frames[-1][0] - frames[0][0]
            rate_hz = (len(frames) - 1) / elapsed if elapsed else 0
            expect = RATE / FRAMES
            print(f"    frame rate: {rate_hz:.2f}/s (expect {expect:.2f})")
            if abs(rate_hz - expect) > 1.0:
                fails.append(f"frame rate {rate_hz:.2f} off expected {expect:.2f}")

            gaps = np.diff([f[0] for f in frames]) * 1000
            print(f"    inter-frame ms: p50 {np.percentile(gaps,50):.2f}  "
                  f"p95 {np.percentile(gaps,95):.2f}  max {gaps.max():.2f}")

            # is it actually audio?
            samples = np.frombuffer(frames[len(frames)//2][1][64:],
                                    dtype=np.float32)
            left, right = samples[0::2], samples[1::2]
            rms = float(np.sqrt((left ** 2).mean()))
            print(f"    left RMS {rms:.6f} "
                  f"({20*np.log10(max(rms,1e-9)):.1f} dBFS)")
            print(f"    L==R (mono duplicated): "
                  f"{np.array_equal(left, right)}")
            if not np.array_equal(left, right):
                fails.append("channels differ -- mono duplication broken")
            if rms == 0.0:
                fails.append("payload is digital silence")
            uniq = len(np.unique(left))
            print(f"    distinct sample values: {uniq}")
            if uniq < 50:
                fails.append(f"only {uniq} distinct values -- stuck buffer?")

        # ---- TX audio ----
        print("\n>>> sending TX_AUDIO frames (radio stays in receive)")
        t = np.arange(FRAMES) / RATE
        tone = (0.2 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)
        stereo = np.repeat(tone, 2)
        head = HDR.pack(0, RATE, 3, 0, 0, stereo.size, 2, 2,
                        0, 0, 0, 0, 0, 0, 0, 0)
        for _ in range(20):
            await ws.send(head + stereo.tobytes())
            await asyncio.sleep(0.04)
        print("    20 TX frames sent")

        await ws.send("trx:0;")
        try:
            r = await asyncio.wait_for(ws.recv(), 2.0)
            while isinstance(r, (bytes, bytearray)):
                r = await asyncio.wait_for(ws.recv(), 2.0)
            print(f"    server still responsive: {r}")
        except (asyncio.TimeoutError, TimeoutError):
            fails.append("server stopped responding after TX audio")

        print("\n>>> audio_stop;")
        await ws.send("audio_stop;")
        await asyncio.sleep(0.5)

    print("\n=== RESULT ===")
    if fails:
        for f in fails:
            print(f"  FAIL: {f}")
        sys.exit(1)
    print("  RX audio stream and TX audio ingest both OK")


if __name__ == "__main__":
    asyncio.run(main())
