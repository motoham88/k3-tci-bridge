#!/usr/bin/env python3
"""Long-run audio soak.

Everything measured so far ran for 60 seconds. The failure mode that bites
long-running audio is not a steady cost -- it is a rare underrun every few
minutes, or a slow drift, neither of which a short test can see.

Streams RX audio for hours and records: frame count against the expected
rate, inter-frame gaps, any interruption long enough to be audible, and the
server's own liveness. Prints a summary line every few minutes so a long log
stays readable, and a full report at the end.

Receive only -- it never keys anything.

    setsid --fork ./venv/bin/python soak.py --hours 8 > soak.log 2>&1 < /dev/null
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import struct
import time

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

HDR = struct.Struct("<16I")
RATE, FRAMES = 48000, 2048
NOMINAL = FRAMES / RATE            # 42.67 ms
TYPE_RX_AUDIO = 1


def now() -> str:
    return time.strftime("%H:%M:%S")


async def main(args) -> None:
    end_at = time.time() + args.hours * 3600
    url = f"ws://{args.host}:{args.port}"
    print(f"{now()}  soak starting: {url}, {args.hours} h, "
          f"expecting {1/NOMINAL:.2f} frames/s")

    total = gaps_big = reconnects = 0
    worst = 0.0
    gaps: list[float] = []
    started = time.time()
    seg_start = time.time()
    seg_frames = 0

    while time.time() < end_at:
        try:
            async with connect(url, max_size=2 ** 20) as ws:
                while True:
                    m = await asyncio.wait_for(ws.recv(), 15)
                    if isinstance(m, str) and m.strip().rstrip(";") == "start":
                        break
                await ws.send("audio_start:0;")
                last = time.perf_counter()
                while time.time() < end_at:
                    m = await asyncio.wait_for(ws.recv(), 15)
                    if not isinstance(m, (bytes, bytearray)):
                        continue
                    if len(m) < 64 or HDR.unpack_from(bytes(m), 0)[6] != TYPE_RX_AUDIO:
                        continue
                    t = time.perf_counter()
                    dt = t - last
                    last = t
                    total += 1
                    seg_frames += 1
                    gaps.append(dt)
                    if dt > worst:
                        worst = dt
                    # Anything over ~2.5 frames is an audible break, not
                    # scheduler jitter.
                    if dt > NOMINAL * 2.5:
                        gaps_big += 1
                        print(f"{now()}  GAP {dt*1000:.0f} ms "
                              f"(frame {total})", flush=True)

                    if t - seg_start >= args.report * 60:
                        el = time.time() - started
                        rate = seg_frames / (t - seg_start)
                        print(f"{now()}  +{el/3600:5.2f} h  "
                              f"{total:>9,} frames  {rate:6.2f}/s  "
                              f"gaps>{NOMINAL*2500:.0f}ms: {gaps_big}  "
                              f"worst {worst*1000:6.1f} ms  "
                              f"reconnects {reconnects}", flush=True)
                        seg_start, seg_frames = t, 0
        except (ConnectionClosed, asyncio.TimeoutError, TimeoutError,
                OSError) as exc:
            reconnects += 1
            print(f"{now()}  CONNECTION LOST ({type(exc).__name__}) — "
                  f"reconnecting in 3 s", flush=True)
            await asyncio.sleep(3)

    el = time.time() - started
    print(f"\n{now()}  === soak complete: {el/3600:.2f} h ===")
    print(f"  frames          {total:,}")
    print(f"  expected        {int(el / NOMINAL):,}")
    print(f"  delivered       {100*total/(el/NOMINAL):.2f}% of expected")
    print(f"  mean rate       {total/el:.3f}/s (nominal {1/NOMINAL:.3f})")
    if gaps:
        g = sorted(gaps)
        p = lambda q: g[min(len(g) - 1, int(len(g) * q))] * 1000
        print(f"  gap p50/p99     {p(0.50):.2f} / {p(0.99):.2f} ms "
              f"(nominal {NOMINAL*1000:.2f})")
        print(f"  gap p999/max    {p(0.999):.2f} / {worst*1000:.2f} ms")
        print(f"  stdev           {statistics.pstdev(gaps)*1000:.3f} ms")
    print(f"  audible gaps    {gaps_big}")
    print(f"  reconnects      {reconnects}")
    verdict = "PASS" if gaps_big == 0 and reconnects == 0 else "SEE ABOVE"
    print(f"  verdict         {verdict}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Long-run audio soak")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=50001)
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--report", type=float, default=10.0,
                    help="summary interval in minutes")
    try:
        asyncio.run(main(ap.parse_args()))
    except KeyboardInterrupt:
        print("\ninterrupted")
