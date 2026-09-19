#!/usr/bin/env python3
"""Round-trip the filter panel's switches on the radio.

AGC, PRE, ATT, NB, RIT and XIT each get flipped, read back through the
bridge's broadcast, and flipped back. What this proves that a fake radio
cannot: that each GET answers in the exact format the bridge parses --
`GT004;`, `PA1;`, `RA01;`, `NB1;` -- rather than something longer that
reads as "no change".

NR and NTCH are not here: their state cannot be read back (see
tci.Bridge._tap), so there is nothing to check but your ears.

Receive only. Everything is put back as it was found.
"""
import asyncio
import sys

from websockets.asyncio.client import connect

URL = "ws://127.0.0.1:50001"
WATCH = ("agc_mode", "preamp", "attenuator", "rx_nb_enable",
         "rit_enable", "xit_enable", "nb_levels")


def absorb(state, m):
    for t in m.split(";"):
        name, _, rest = t.strip().partition(":")
        if name in WATCH:
            state[name] = rest.split(",")[1:]


async def init(ws):
    state = {}
    while True:
        m = await asyncio.wait_for(ws.recv(), 8.0)
        if isinstance(m, str):
            absorb(state, m)
            if m.strip().rstrip(";") == "start":
                return state


async def after(ws, state, cmd, window=1.5):
    await ws.send(cmd + ";")
    loop = asyncio.get_running_loop()
    end = loop.time() + window
    while loop.time() < end:
        try:
            m = await asyncio.wait_for(ws.recv(), max(0.05, end - loop.time()))
        except (asyncio.TimeoutError, TimeoutError):
            break
        if isinstance(m, str):
            absorb(state, m)


async def main():
    fails = []
    async with connect(URL) as ws:
        state = await init(ws)
        missing = [n for n in WATCH if n not in state]
        if missing:
            print(f"  init burst lacks {missing} -- is the new bridge running?")
            return 1
        lv = state["nb_levels"]
        print(f"  NB levels: DSP {lv[0]}, IF {lv[1]}"
              + ("   (both 0: NB on will blank nothing)" if lv == ["0", "0"] else ""))

        flips = [("agc_mode", "fast", "slow")] + [
            (n, "true", "false") for n in
            ("preamp", "attenuator", "rx_nb_enable", "rit_enable", "xit_enable")]
        for name, a, b in flips:
            was = state[name][0]
            want = b if was == a else a
            await after(ws, state, f"{name}:0,{want}")
            got = state[name][0]
            await after(ws, state, f"{name}:0,{was}")
            back = state[name][0]
            ok = got == want and back == was
            print(f"  {name:<13} {was:>5} -> {got:<5} -> {back:<5}  "
                  f"{'ok' if ok else 'WRONG'}")
            if not ok:
                fails.append(f"{name}: asked {want} then {was}, "
                             f"read {got} then {back}")

    print()
    if fails:
        print("FAIL:\n  " + "\n  ".join(fails))
        return 1
    print("  every switch round-trips")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
