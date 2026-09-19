#!/usr/bin/env python3
"""Exercise rx_filter_band in every mode class.

The conversion between TCI's carrier-relative edges and the K3's BW + IS is
mode-dependent, and the failure mode is silent: in CW/AM the TCI band
straddles the carrier, so a naive (lo+hi)/2 gives zero and would drag the
passband to DC. This checks each class round-trips sensibly.

Receive only. The radio stores a filter per mode, and this rewrites them,
so each mode's passband is recorded first and written back at the end, and
the starting mode is restored. DATA is left on sub-mode DATA A, which is
what `digu` selects.
"""
import asyncio
import sys

from websockets.asyncio.client import connect

URL = "ws://127.0.0.1:50001"


async def init(ws):
    """Returns the starting mode."""
    mode = None
    while True:
        m = await asyncio.wait_for(ws.recv(), 8.0)
        if not isinstance(m, str):
            continue
        for t in m.split(";"):
            if t.strip().startswith("modulation:"):
                mode = t.strip().split(",")[1]
        if m.strip().rstrip(";") == "start":
            return mode


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
        start_mode = await init(ws)
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

        saved = {}
        for mode in dict.fromkeys(m for m, _, _ in cases):
            await ws.send(f"modulation:0,{mode};")
            # DATA verifies DT and MD in turn, which can outlast the usual
            # window; a passband missed here cannot be restored.
            saved[mode] = band_of(await collect(ws, 3.0))
        print(f"  saved per-mode filters: {saved}\n")

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

        print("\n  restoring:")
        for mode, band in saved.items():
            if band is None:
                print(f"    {mode}: nothing saved, left as tested")
                continue
            await ws.send(f"modulation:0,{mode};")
            await collect(ws)
            await ws.send(f"rx_filter_band:0,{band[0]},{band[1]};")
            back = band_of(await collect(ws))
            print(f"    {mode:<5} {band} -> {back}")
            if back != band:
                fails.append(f"{mode}: restored {back}, saved {band}")
        if start_mode:
            await ws.send(f"modulation:0,{start_mode};")
            await collect(ws)
            print(f"    mode back to {start_mode}")

    print()
    if fails:
        for f in fails:
            print(f"  FAIL: {f}")
        sys.exit(1)
    print("  rx_filter_band behaves correctly in all mode classes")


if __name__ == "__main__":
    asyncio.run(main())
