#!/usr/bin/env python3
"""Watch WSJT-X transmissions through the bridge and check their timing.

FT8 slots begin on UTC seconds 0/15/30/45 and each transmission lasts
12.64 s. A transmission that starts late or ends early still decodes on a
local monitor but will be missed or mis-decoded by everyone else, so the
timing is worth measuring rather than assuming.

This only observes -- it never keys anything. Run it alongside WSJT-X:

    ./venv/bin/python wsjtxmon.py

Then look at the server log for the matching TX audio statistics:

    journalctl -u k3-tci -f | grep -E "TX audio|CHRONO"
"""
import asyncio
import time

from websockets.asyncio.client import connect

URL = "ws://127.0.0.1:50001"
FT8_SLOT = 15.0
FT8_TX = 12.64


def utc_phase(t: float) -> float:
    """Seconds past the most recent FT8 slot boundary."""
    return t % FT8_SLOT


async def main() -> None:
    print(f"watching {URL} — Ctrl-C to stop\n")
    print(f"  FT8: slots every {FT8_SLOT:.0f} s, "
          f"transmission {FT8_TX:.2f} s\n")
    n = 0
    async with connect(URL, max_size=2 ** 20) as ws:
        while True:
            m = await ws.recv()
            if isinstance(m, str) and m.strip().rstrip(";") == "start":
                break
        keyed_at = None
        async for m in ws:
            if not isinstance(m, str):
                continue
            for part in m.split(";"):
                part = part.strip()
                if not part.startswith("trx:"):
                    continue
                on = part.endswith("true")
                now = time.time()
                if on and keyed_at is None:
                    keyed_at = now
                    ph = utc_phase(now)
                    late = ph if ph < FT8_SLOT / 2 else ph - FT8_SLOT
                    flag = "ok" if abs(late) < 0.35 else "LATE/EARLY"
                    print(f"  TX  start  {time.strftime('%H:%M:%S')} "
                          f"slot offset {late:+.3f} s   {flag}")
                elif not on and keyed_at is not None:
                    dur = now - keyed_at
                    keyed_at = None
                    n += 1
                    err = dur - FT8_TX
                    flag = "ok" if abs(err) < 0.5 else "DURATION OFF"
                    print(f"  TX  end    {time.strftime('%H:%M:%S')} "
                          f"duration {dur:6.2f} s ({err:+.2f} vs "
                          f"{FT8_TX})   {flag}")
                    print(f"       ({n} transmission(s) seen)\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
