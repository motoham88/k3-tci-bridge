#!/usr/bin/env python3
"""Exercise rx_filter_band in every mode class.

The conversion between TCI's carrier-relative edges and the K3's BW + IS is
mode-dependent, and the failure mode is silent: in CW/AM the TCI band
straddles the carrier, so a naive (lo+hi)/2 gives zero and would drag the
passband to DC. This checks each class round-trips sensibly.

Receive only.
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


async def collect(ws, window=1.4):
    """Fixed window, ignoring rx_smeter -- the stream never goes quiet."""
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


def band_of(msgs):
    for m in reversed(msgs):
        if m.startswith("rx_filter_band:"):
            p = m.split(":")[1].split(",")
            return int(p[1]), int(p[2])
    return None


async def main():
    fails = []
    async with connect(URL) as ws:
        await init(ws)
        await collect(ws, 0.5)

        # mode, requested band, what we expect back
        cases = [
            ("usb",  (300, 2700),   "offset, positive"),
            ("usb",  (300, 1800),   "offset, narrower"),
            ("lsb",  (-2700, -300), "offset, negative"),
            ("cwu",  (-350, 350),   "symmetric about carrier"),
            ("cwu",  (-200, 200),   "symmetric, narrow"),
            ("am",   (-3000, 3000), "symmetric, wide"),
            ("digu", (300, 2700),   "offset, positive"),
        ]

        for mode, (lo, hi), note in cases:
            await ws.send(f"modulation:0,{mode};")
            got = await collect(ws)
            await ws.send(f"rx_filter_band:0,{lo},{hi};")
            got = await collect(ws)
            band = band_of(got)
            if band is None:
                print(f"  {mode:<5} {lo:>6}..{hi:<6} -> NO RESPONSE")
                fails.append(f"{mode}: no rx_filter_band response")
                continue
            glo, ghi = band
            width_req, width_got = hi - lo, ghi - glo
            print(f"  {mode:<5} {lo:>6}..{hi:<6} -> {glo:>6}..{ghi:<6}  "
                  f"width {width_req} -> {width_got}   ({note})")

            # The radio quantises hard, so only sanity-check the shape.
            if width_got <= 0:
                fails.append(f"{mode}: non-positive width {width_got}")
            if mode in ("cwu", "cwl", "am", "nfm"):
                if abs(glo + ghi) > 60:
                    fails.append(f"{mode}: band not symmetric about the "
                                 f"carrier ({glo},{ghi}) -- IS was probably "
                                 f"written when it should not have been")
            if mode in ("usb", "digu") and glo < 0:
                fails.append(f"{mode}: lower edge went negative ({glo})")
            if mode == "lsb" and ghi > 0:
                fails.append(f"lsb: upper edge went positive ({ghi})")

        # a mode change must re-report the passband, since filters are
        # stored per mode on the radio
        print("\n  mode change re-reports the band:")
        await ws.send("modulation:0,usb;")
        got = await collect(ws)
        print(f"    -> {[m for m in got if m.startswith(('modulation','rx_filter_band'))]}")
        if not any(m.startswith("rx_filter_band") for m in got):
            fails.append("mode change did not re-broadcast rx_filter_band")

        await ws.send("modulation:0,am;")
        await collect(ws, 0.8)

    print()
    if fails:
        for f in fails:
            print(f"  FAIL: {f}")
        sys.exit(1)
    print("  rx_filter_band behaves correctly in all mode classes")


if __name__ == "__main__":
    asyncio.run(main())
