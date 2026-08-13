#!/usr/bin/env python3
"""Confirm TCI `volume` and `mute` actually change the audio.

This matters because the obvious mapping is wrong: the K3's AF GAIN does
NOT affect the USB audio (measured: 0.6 dB across AG000..AG250), so volume
has to be applied in software. This checks that it is.
"""
import asyncio
import sys

import numpy as np
from websockets.asyncio.client import connect

URL = "ws://127.0.0.1:50001"


async def init(ws):
    while True:
        m = await asyncio.wait_for(ws.recv(), 8.0)
        if isinstance(m, str) and m.strip().rstrip(";") == "start":
            return


async def measure(ws, seconds=2.5):
    """Mean RMS of the audio frames arriving over `seconds`."""
    vals = []
    end = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < end:
        try:
            m = await asyncio.wait_for(ws.recv(), 2.0)
        except (asyncio.TimeoutError, TimeoutError):
            break
        if isinstance(m, (bytes, bytearray)):
            s = np.frombuffer(bytes(m)[64:], dtype=np.float32)
            if s.size:
                vals.append(float(np.sqrt((s.astype(np.float64) ** 2).mean())))
    return float(np.mean(vals)) if vals else 0.0


async def drain(ws, seconds=1.0):
    """Discard frames already queued.

    A sleep is NOT enough after changing volume: frames sent before the
    change are still sitting in the client's receive queue, and averaging
    them in makes a working mute look broken.
    """
    end = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < end:
        try:
            await asyncio.wait_for(ws.recv(), 0.3)
        except (asyncio.TimeoutError, TimeoutError):
            return


def db(x):
    return 20 * np.log10(max(x, 1e-12))


async def main():
    fails = []
    async with connect(URL, max_size=2 ** 20) as ws:
        await init(ws)
        await ws.send("audio_start:0;")
        await asyncio.sleep(1.0)

        await ws.send("volume:0;")
        await drain(ws)
        base = await measure(ws)
        print(f"  volume:0   -> rms {base:.6f} ({db(base):6.1f} dBFS)")

        await ws.send("volume:-20;")
        await drain(ws)
        v20 = await measure(ws)
        print(f"  volume:-20 -> rms {v20:.6f} ({db(v20):6.1f} dBFS)  "
              f"delta {db(v20)-db(base):+.1f} dB (expect -20)")
        if not (-26 < db(v20) - db(base) < -14):
            fails.append("volume:-20 did not attenuate by ~20 dB")

        await ws.send("volume:-40;")
        await drain(ws)
        v40 = await measure(ws)
        print(f"  volume:-40 -> rms {v40:.6f} ({db(v40):6.1f} dBFS)  "
              f"delta {db(v40)-db(base):+.1f} dB (expect -40)")
        if not (-48 < db(v40) - db(base) < -32):
            fails.append("volume:-40 did not attenuate by ~40 dB")

        await ws.send("volume:0;")
        await drain(ws, 0.5)
        await ws.send("mute:0,true;")
        await drain(ws)
        m = await measure(ws)
        print(f"  mute:true  -> rms {m:.9f}")
        if m != 0.0:
            fails.append(f"mute did not silence the stream (rms {m})")

        await ws.send("mute:0,false;")
        await drain(ws)
        back = await measure(ws)
        print(f"  mute:false -> rms {back:.6f} ({db(back):6.1f} dBFS)")
        if back <= 0.0:
            fails.append("unmute did not restore audio")

        await ws.send("audio_stop;")
        await asyncio.sleep(0.3)

    print()
    if fails:
        for f in fails:
            print(f"  FAIL: {f}")
        sys.exit(1)
    print("  software volume and mute both work")


if __name__ == "__main__":
    asyncio.run(main())
