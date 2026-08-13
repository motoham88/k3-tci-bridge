#!/usr/bin/env python3
"""Verify TX_CHRONO pacing.

TX_CHRONO is how clients like WSJT-X are told to send TX audio: header-only
type-3 frames, one per 1024 stereo frames of audio (21.33 ms at 48 kHz).
Without them such a client keys up and transmits silence.

Checks that the clock starts on key-up, runs at the right rate, and stops on
key-down. Run with the radio in TX TEST so nothing radiates.
"""
import asyncio
import statistics
import struct
import sys
import time

from websockets.asyncio.client import connect

URL = "ws://127.0.0.1:50001"
HDR = struct.Struct("<16I")
TYPE_RX_AUDIO, TYPE_TX_CHRONO = 1, 3
EXPECT_MS = 1024 / 48000 * 1000      # 21.33


async def init(ws):
    while True:
        m = await asyncio.wait_for(ws.recv(), 8.0)
        if isinstance(m, str) and m.strip().rstrip(";") == "start":
            return


async def drain(ws, seconds=1.0):
    """Discard whatever is already queued.

    Frames buffered before the measurement window would otherwise be
    counted inside it and inflate the apparent rate.
    """
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        try:
            await asyncio.wait_for(ws.recv(), max(0.05, end - loop.time()))
        except (asyncio.TimeoutError, TimeoutError):
            return


async def gather(ws, seconds):
    """Collect chrono frame arrival times over a window.

    NOTE: inter-arrival gaps here are NOT the server's send intervals.
    TCP coalesces WebSocket frames, so they arrive in bursts (gaps near
    zero followed by longer ones). Only the COUNT over a drained window is
    meaningful from the client side; the server logs its own true rate.
    """
    times, others = [], 0
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        try:
            m = await asyncio.wait_for(ws.recv(), max(0.05, end - loop.time()))
        except (asyncio.TimeoutError, TimeoutError):
            break
        if isinstance(m, (bytes, bytearray)) and len(m) >= 64:
            t = HDR.unpack_from(bytes(m), 0)[6]
            if t == TYPE_TX_CHRONO:
                times.append(time.perf_counter())
            elif t == TYPE_RX_AUDIO:
                others += 1
    return times, others


async def main():
    fails = []
    async with connect(URL, max_size=2 ** 20) as ws:
        await init(ws)
        await ws.send("audio_start:0;")
        await asyncio.sleep(1.0)

        # Digital mode: keying a DATA mode implies the client intends to
        # modulate, which is what starts the clock without an explicit
        # source argument.
        await ws.send("modulation:0,digu;")
        await asyncio.sleep(1.5)

        print("=== before key-up: there must be NO chrono ===")
        t, rx = await gather(ws, 2.0)
        print(f"  chrono frames {len(t)}, rx audio frames {rx}")
        if t:
            fails.append(f"{len(t)} chrono frames arrived while receiving")

        print("\n=== key up ===")
        await ws.send("trx:0,true,tci;")
        await drain(ws, 1.0)              # let it settle, discard the backlog
        window = 6.0
        t, rx = await gather(ws, window)
        expect = window * 1000 / EXPECT_MS
        rate = len(t) / window
        print(f"  chrono frames {len(t)} in {window:.0f} s "
              f"= {rate:.2f}/s (expect {expect/window:.2f}/s), rx audio {rx}")
        if abs(rate - expect / window) > 4.0:
            fails.append(f"rate {rate:.2f}/s is off "
                         f"{expect/window:.2f}/s")
        if len(t) > 2:
            gaps = [(b - a) * 1000 for a, b in zip(t, t[1:])]
            print(f"  arrival gaps (batched by TCP, informational only): "
                  f"median {statistics.median(gaps):.2f} ms, "
                  f"min {min(gaps):.2f}, max {max(gaps):.2f}")
            print(f"  the server logs the authoritative send rate; "
                  f"target is {EXPECT_MS:.2f} ms")

        print("\n=== key down: chrono must stop ===")
        await ws.send("trx:0,false;")
        await drain(ws, 1.0)
        t, rx = await gather(ws, 2.0)
        print(f"  chrono frames {len(t)} after unkey, rx audio {rx}")
        if t:
            fails.append(f"{len(t)} chrono frames still arriving after unkey")

        await ws.send("modulation:0,cwl;")
        await ws.send("audio_stop;")
        await asyncio.sleep(0.4)

    print()
    if fails:
        for f in fails:
            print(f"  FAIL: {f}")
        sys.exit(1)
    print("  TX_CHRONO starts on key-up, paces correctly, stops on key-down")


if __name__ == "__main__":
    asyncio.run(main())
