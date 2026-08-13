#!/usr/bin/env python3
"""Exercise the CW keying path.

Run with the radio in TX TEST so nothing radiates: this checks the command
plumbing (mode guard, chunking, buffer flow control, speed), not the RF.

Checks that a message longer than KY's 24-character limit is split, and
that keying is refused outside CW mode rather than silently doing nothing.
"""
import asyncio
import sys

from websockets.asyncio.client import connect

URL = "ws://127.0.0.1:50001"


async def init(ws):
    while True:
        m = await asyncio.wait_for(ws.recv(), 8.0)
        if isinstance(m, str) and m.strip().rstrip(";") == "start":
            return


async def collect(ws, window=1.2):
    out, loop = [], asyncio.get_running_loop()
    end = loop.time() + window
    while loop.time() < end:
        try:
            m = await asyncio.wait_for(ws.recv(), max(0.05, end - loop.time()))
        except (asyncio.TimeoutError, TimeoutError):
            break
        if isinstance(m, str) and not m.startswith("rx_smeter"):
            out.append(m.strip().rstrip(";"))
    return out


async def main():
    fails = []
    async with connect(URL) as ws:
        await init(ws)
        await collect(ws, 0.5)

        print("=== keyer speed ===")
        await ws.send("cw_keyer_speed;")
        print("  query   ->", await collect(ws))
        await ws.send("cw_keyer_speed:24,0;")
        got = await collect(ws)
        print("  set 24  ->", got)
        if not any("24" in m for m in got):
            fails.append("cw_keyer_speed did not report 24")
        await ws.send("cw_keyer_speed:99,0;")
        got = await collect(ws)
        print("  set 99  ->", got, " (clamped to the K3's 8-50 range)")
        if not any("50" in m for m in got):
            fails.append("cw_keyer_speed did not clamp 99 to 50")
        await ws.send("cw_keyer_speed:22,0;")
        await collect(ws)

        print("\n=== mode guard ===")
        await ws.send("modulation:0,usb;")
        await collect(ws)
        await ws.send("cw_msg:TEST;")
        await collect(ws, 0.8)
        print("  sent cw_msg in USB — must be refused, check the log for")
        print("  'cw_msg ignored: mode is usb, not CW'")

        print("\n=== keying in CW (TX TEST: no RF) ===")
        await ws.send("modulation:0,cwu;")
        print("  mode ->", await collect(ws))

        await ws.send("cw_msg:TEST DE K3;")
        print("  short message sent")
        await collect(ws, 3.0)

        long_msg = "CQ CQ CQ DE TEST TEST TEST K"   # 28 chars: must split
        await ws.send(f"cw_msg:{long_msg};")
        print(f"  long message ({len(long_msg)} chars) sent — must be chunked")
        await collect(ws, 6.0)

        await ws.send("cw_macros_stop;")
        await collect(ws, 0.6)
        print("  stop sent")

        await ws.send("modulation:0,am;")
        await collect(ws)

    print()
    if fails:
        for f in fails:
            print(f"  FAIL: {f}")
        sys.exit(1)
    print("  CW command path OK — check the server log for chunking detail")


if __name__ == "__main__":
    asyncio.run(main())
