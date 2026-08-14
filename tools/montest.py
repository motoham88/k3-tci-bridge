#!/usr/bin/env python3
"""Does the K3's transmit MONITOR reach LINE OUT?

If it does, the existing RX audio stream carries your own transmitted audio
during TX, and "hearing your TX" needs no new plumbing at all -- just the
monitor level raised. If it does not, the RX stream goes silent during
transmit and a monitor would have to be synthesised from the TX audio the
bridge already receives.

Feeds a tone as TX audio while keyed and measures the RX stream level, with
the monitor off and then on. Run with the radio in TX TEST: no RF.
"""
import asyncio
import struct
import sys

import numpy as np
from websockets.asyncio.client import connect

URL = "ws://127.0.0.1:50001"
HDR = struct.Struct("<16I")
RATE, FR = 48000, 2048
TYPE_RX = 1


async def init(ws):
    while True:
        m = await asyncio.wait_for(ws.recv(), 8.0)
        if isinstance(m, str) and m.strip().rstrip(";") == "start":
            return


async def rx_level(ws, seconds):
    """Mean RMS of RX audio frames over a window."""
    vals, loop = [], asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        try:
            m = await asyncio.wait_for(ws.recv(), max(0.05, end - loop.time()))
        except (asyncio.TimeoutError, TimeoutError):
            break
        if isinstance(m, (bytes, bytearray)) and len(m) > 64:
            if HDR.unpack_from(bytes(m), 0)[6] != TYPE_RX:
                continue
            a = np.frombuffer(bytes(m)[64:], dtype=np.float32)
            if a.size:
                vals.append(float(np.sqrt((a.astype(np.float64) ** 2).mean())))
    return float(np.mean(vals)) if vals else 0.0


def db(x):
    return 20 * np.log10(x) if x > 1e-9 else -99.0


async def keyed_level(ws, seconds=3.0):
    """Key, feed a tone, measure the RX stream, unkey."""
    t = np.arange(FR) / RATE
    tone = (0.25 * np.sin(2 * np.pi * 1500 * t)).astype(np.float32)
    st = np.repeat(tone, 2)
    head = HDR.pack(0, RATE, 3, 0, 0, st.size, 2, 2, 0, 0, 0, 0, 0, 0, 0, 0)
    stop = False

    async def feed():
        while not stop:
            await ws.send(head + st.tobytes())
            await asyncio.sleep(0.021)

    await ws.send("trx:0,true,tci;")
    await asyncio.sleep(1.2)
    task = asyncio.create_task(feed())
    lvl = await rx_level(ws, seconds)
    stop = True
    await asyncio.sleep(0.1)
    task.cancel()
    await ws.send("trx:0,false;")
    await asyncio.sleep(1.5)
    return lvl


async def main():
    async with connect(URL, max_size=2 ** 20) as ws:
        await init(ws)
        await ws.send("audio_start:0;")
        await ws.send("modulation:0,digu;")
        await asyncio.sleep(2.0)

        base = await rx_level(ws, 2.5)
        print(f"  receiving, monitor off : {db(base):7.1f} dBFS  (band noise)")

        await ws.send("mon_volume:0;")
        await asyncio.sleep(1.0)
        off = await keyed_level(ws)
        print(f"  transmitting, MON = 0  : {db(off):7.1f} dBFS")

        await ws.send("mon_volume:50;")
        await asyncio.sleep(1.0)
        mid = await keyed_level(ws)
        print(f"  transmitting, MON = 50 : {db(mid):7.1f} dBFS")

        await ws.send("mon_volume:100;")
        await asyncio.sleep(1.0)
        on = await keyed_level(ws)
        print(f"  transmitting, MON = 100: {db(on):7.1f} dBFS")

        await ws.send("mon_volume:0;")
        await ws.send("modulation:0,cwl;")
        await ws.send("audio_stop;")
        await asyncio.sleep(0.5)

    print()
    print(f"  MON 0 -> 100 changes the RX stream by "
          f"{db(on) - db(off):+.1f} dB")
    if db(on) > db(off) + 4.0:
        print("  RESULT: the transmit monitor DOES reach LINE OUT.")
        print("  Raising MON makes your own TX audible in the RX stream —")
        print("  no extra plumbing needed.")
        return 0
    print("  RESULT: the monitor does NOT reach LINE OUT — the RX stream is")
    print("  unchanged by MON during transmit. Hearing your own TX would")
    print("  have to be synthesised from the TX audio the bridge receives.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
