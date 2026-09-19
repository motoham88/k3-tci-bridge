#!/usr/bin/env python3
"""Exercise the band command on the radio.

Two things only the radio can answer:

  1. Does the BN numbering land on the band each button is labelled with?
     An off-by-one here is silent -- 02, 06 and 08 are the WARC and 60 m
     slots in between the buttons -- so every button is pressed and the
     landing frequency checked against the band.
  2. Does a general-coverage frequency stick in a band's memory? The
     bridge's vfo handler says an out-of-band request snaps to the nearest
     amateur band; the operator's experience is that 8.050 sticks as the
     40 m memory. This parks VFO A on 8.050, reports what the radio took,
     and then presses 40 -- which must come back on 7.050 either way.

Receive only. It DOES rewrite the 40 m memory (to 7.050), and it leaves
VFO A where it found it.
"""
import asyncio
import sys

from websockets.asyncio.client import connect

URL = "ws://127.0.0.1:50001"
BUTTONS = ["160", "80", "40", "30", "20", "15", "10"]


async def init(ws):
    """Returns the band plan from the init burst, and VFO A."""
    plan, vfo = {}, None
    while True:
        m = (await asyncio.wait_for(ws.recv(), 8.0))
        if not isinstance(m, str):
            continue
        m = m.strip().rstrip(";")
        if m.startswith("band_plan:"):
            for b in m.split(":", 1)[1].split(","):
                name, lo, hi = b.split("/")
                plan[name] = (int(lo), int(hi))
        elif m.startswith("vfo:0,0,"):
            vfo = int(m.split(",")[2])
        elif m == "start":
            return plan, vfo


async def vfo_after(ws, cmd, window=2.0):
    """Send cmd and return the last VFO A the bridge broadcasts after it."""
    await ws.send(cmd + ";")
    loop, hz = asyncio.get_running_loop(), None
    end = loop.time() + window
    while loop.time() < end:
        try:
            m = await asyncio.wait_for(ws.recv(), max(0.05, end - loop.time()))
        except (asyncio.TimeoutError, TimeoutError):
            break
        if isinstance(m, str):
            for t in m.split(";"):
                if t.strip().startswith("vfo:0,0,"):
                    hz = int(t.strip().split(",")[2])
    return hz


async def main():
    fails = []
    async with connect(URL) as ws:
        plan, start = await init(ws)
        if not plan:
            print("  no band_plan in the init burst -- is the new bridge running?")
            return 1

        print("=== each button lands inside its own band ===")
        for n in BUTTONS:
            hz = await vfo_after(ws, f"band:0,{n}")
            lo, hi = plan[n]
            ok = hz is not None and lo <= hz <= hi
            print(f"  {n:>4} m  -> {hz}   {'ok' if ok else 'WRONG BAND'}")
            if not ok:
                fails.append(f"{n} m landed on {hz}")

        print("\n=== does 8.050 stick? ===")
        hz = await vfo_after(ws, "vfo:0,0,8050000")
        print(f"  asked for 8050000, radio took {hz}"
              + ("   (it sticks -- the band command's fix-up is needed)"
                 if hz == 8050000 else "   (it snapped)"))
        await vfo_after(ws, "band:0,20")          # leave 40 m holding 8.050
        hz = await vfo_after(ws, "band:0,40")
        print(f"  40 m button now lands on {hz}")
        if hz != 7050000:
            fails.append(f"40 m after 8.050 landed on {hz}, not 7050000")

        if start:
            await vfo_after(ws, f"vfo:0,0,{start}")
            print(f"\n  VFO A restored to {start}")

    print()
    if fails:
        print("FAIL:\n  " + "\n  ".join(fails))
        return 1
    print("  band command behaves on every button")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
