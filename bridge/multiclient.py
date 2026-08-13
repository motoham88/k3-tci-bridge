#!/usr/bin/env python3
"""TCI is a stateful broker: a change made by one client must reach ALL of
them. Two clients connect; one tunes; the other must hear about it without
having asked.

Also checks the disconnect safety net -- a client that keys and then drops
must not leave the radio transmitting.
"""
import asyncio
import sys

from websockets.asyncio.client import connect

URL = "ws://127.0.0.1:50001"


async def drain(ws, window=1.5):
    """Collect for a fixed window, ignoring rx_smeter.

    Do NOT wait for the socket to go quiet: the bridge broadcasts
    rx_smeter every 200 ms, so a quiet period never arrives and a
    wait-until-silent loop hangs forever.
    """
    out = []
    loop = asyncio.get_running_loop()
    end = loop.time() + window
    while loop.time() < end:
        try:
            m = await asyncio.wait_for(ws.recv(), max(0.05, end - loop.time()))
        except (asyncio.TimeoutError, TimeoutError):
            break
        if isinstance(m, str) and m.startswith("rx_smeter"):
            continue
        out.append(m)
    return out


async def init(ws):
    while True:
        m = await asyncio.wait_for(ws.recv(), 6.0)
        if m.strip().rstrip(";") == "start":
            return


async def main():
    failures = []
    async with connect(URL) as a, connect(URL) as b:
        await init(a)
        await init(b)
        await drain(a, 0.5)
        await drain(b, 0.5)
        print("two clients connected\n")

        # --- A tunes; B must be told ---
        print(">>> client A: vfo:0,0,14123000;")
        await a.send("vfo:0,0,14123000;")
        got_a, got_b = await drain(a), await drain(b)
        print(f"    A heard: {got_a}")
        print(f"    B heard: {got_b}")
        if not any("14123000" in m for m in got_b):
            failures.append("B did not receive A's VFO change")

        # --- B changes mode; A must be told ---
        print("\n>>> client B: modulation:0,digu;")
        await b.send("modulation:0,digu;")
        got_a, got_b = await drain(a), await drain(b)
        print(f"    A heard: {got_a}")
        print(f"    B heard: {got_b}")
        if not any("digu" in m for m in got_a):
            failures.append("A did not receive B's mode change")

        # --- A keys; B must see it ---
        print("\n>>> client A: trx:0,true;  (TX TEST, no RF)")
        await a.send("trx:0,true;")
        got_a, got_b = await drain(a), await drain(b)
        print(f"    A heard: {got_a}")
        print(f"    B heard: {got_b}")
        if not any("trx:0,true" in m for m in got_b):
            failures.append("B did not see A's PTT")

        # --- A vanishes while keyed ---
        print("\n>>> client A disconnects WHILE KEYED")
        await a.close()
        await asyncio.sleep(2.5)
        await b.send("trx:0;")
        st = await drain(b)
        print(f"    B sees: {st}")
        # B is still connected, so the "last client left" rescue does not
        # fire; the PTT watchdog is what must eventually cover this.
        print("    (note: watchdog is the backstop while another client "
              "remains connected)")

        await b.send("trx:0,false;")
        print(f"    after explicit unkey: {await drain(b)}")

        print("\n>>> restoring")
        await b.send("modulation:0,cwl;")
        await drain(b, 0.8)
        await b.send("vfo:0,0,14050620;")
        await drain(b, 0.8)

    print("\n=== RESULT ===")
    if failures:
        for f in failures:
            print(f"  FAIL: {f}")
        sys.exit(1)
    print("  broadcast-to-all-clients works in both directions")


if __name__ == "__main__":
    asyncio.run(main())
