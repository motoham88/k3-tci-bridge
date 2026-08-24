#!/usr/bin/env python3
"""Exercise the TCI skeleton the way a real client would.

Connects, collects the init handshake, then drives vfo / modulation / trx
and checks that each change is broadcast back. Restores the starting
frequency and mode at the end.

Safe to run while the radio is in TX TEST -- the PTT step produces no RF.
"""
import asyncio
import sys

from websockets.asyncio.client import connect

URL = "ws://127.0.0.1:50001"


async def drain(ws, window=1.0):
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


async def collect_init(ws, timeout=6.0):
    msgs = []
    while True:
        m = await asyncio.wait_for(ws.recv(), timeout)
        msgs.append(m)
        if m.strip().rstrip(";") == "start":
            return msgs


async def step(ws, send, label):
    print(f"\n>>> {send}")
    await ws.send(send)
    got = await drain(ws)
    for g in got:
        print(f"    <- {g}")
    if not got:
        print("    <- (nothing)")
    return got


async def main():
    async with connect(URL) as ws:
        print("=== INIT HANDSHAKE ===")
        init = await collect_init(ws)
        for m in init:
            print(f"    <- {m}")

        required = ["protocol", "device", "receive_only", "trx_count",
                    "channels_count", "vfo_limits", "if_limits",
                    "modulations_list", "audio_samplerate", "ready", "start"]
        names = [m.split(":")[0].rstrip(";").strip() for m in init]
        missing = [r for r in required if r not in names]
        print(f"\n  {len(init)} messages; missing required: "
              f"{missing or 'none'}")
        if names.index("ready") < max(
                names.index(n) for n in names if n not in ("ready", "start")):
            print("  ORDER PROBLEM: 'ready' arrived before some settings")
        else:
            print("  order OK: settings, then ready, then start")

        # remember where we started
        vfo0 = next(m for m in init if m.startswith("vfo:0,0,"))
        mod0 = next(m for m in init if m.startswith("modulation:"))
        start_hz = int(vfo0.split(",")[2].rstrip(";"))
        start_mode = mod0.split(",")[1].rstrip(";")
        rit0 = next((m for m in init if m.startswith("rit_enable:")), None)
        off0 = next((m for m in init if m.startswith("rit_offset:")), None)
        start_rit = rit0.split(",")[1].rstrip(";") if rit0 else "false"
        start_off = int(off0.split(",")[1].rstrip(";")) if off0 else 0
        print(f"\n  starting point: {start_hz} Hz, {start_mode}, "
              f"RIT {start_rit} @ {start_off} Hz")

        # xit_offset rides the init burst so a client modelling the two
        # offsets separately cannot start out of sync -- the K3 has one RO
        # register behind both.
        if not any(m.startswith("xit_offset:") for m in init):
            print("  WARNING: init burst carries no xit_offset")

        print("\n=== QUERIES ===")
        await step(ws, "vfo:0,0;", "vfo query")
        await step(ws, "modulation:0;", "modulation query")
        await step(ws, "trx:0;", "trx query")
        await step(ws, "split_enable:0;", "split query")
        await step(ws, "rit_enable:0;", "RIT enable query")
        await step(ws, "rit_offset:0;", "RIT offset query")

        print("\n=== SETS ===")
        target = 14_055_000
        await step(ws, f"vfo:0,0,{target};", "set VFO A")
        await step(ws, "modulation:0,cwl;", "set mode CWL")
        await step(ws, "modulation:0,digu;", "set mode DIGU")

        print("\n=== RIT / XIT ===")
        # These are read-backs of the RADIO, not echoes of the request: the
        # bridge re-reads IF and broadcasts what was accepted. A reply that
        # simply mirrors what was sent would pass a naive check while the
        # radio ignored the command entirely, which is how the IF length-guard
        # bug hid for months (see k3-tci-command-map.md).
        got = await step(ws, "rit_enable:0,true;", "RIT on")
        if not any("rit_enable:0,true" in g for g in got):
            print("    PROBLEM: radio did not report RIT on")

        got = await step(ws, "rit_offset:0,500;", "RIT +500 Hz")
        if not any("rit_offset:0,500" in g for g in got):
            print("    PROBLEM: radio did not report the 500 Hz offset")
        # One register, so BOTH offsets must be echoed or a client that
        # models them separately silently diverges from the radio.
        if not any("xit_offset:0,500" in g for g in got):
            print("    PROBLEM: xit_offset not echoed with rit_offset")

        got = await step(ws, "rit_offset:0,-250;", "RIT -250 Hz (sign)")
        if not any("rit_offset:0,-250" in g for g in got):
            print("    PROBLEM: negative offset did not read back")

        await step(ws, "xit_enable:0,true;", "XIT on")
        # `if:` is TCI's combined verb: a zero offset must also clear RIT,
        # or the radio sits enabled at zero with nothing to show for it.
        got = await step(ws, "if:0,0,0;", "if -> 0 (clears RIT)")
        if not any("rit_enable:0,false" in g for g in got):
            print("    PROBLEM: if:0,0,0 did not turn RIT off")

        # Out of range: the register holds +/-9999 and the radio clamps.
        await step(ws, "rit_offset:0,99999;", "RIT offset out of range")

        print("\n=== PTT (no RF: radio is in TX TEST) ===")
        await step(ws, "trx:0,true;", "key")
        await asyncio.sleep(0.5)
        await step(ws, "trx:0,false;", "unkey")

        print("\n=== BATCHED COMMANDS IN ONE FRAME ===")
        await step(ws, "vfo:0,0;modulation:0;trx:0;", "three in one frame")

        print("\n=== UNKNOWN COMMAND (must be ignored, not fatal) ===")
        await step(ws, "no_such_command:1,2;", "unknown")

        print("\n=== RESTORE ===")
        await step(ws, f"rit_offset:0,{start_off};", "restore RIT offset")
        await step(ws, f"rit_enable:0,{start_rit};", "restore RIT")
        await step(ws, "xit_enable:0,false;", "restore XIT")
        await step(ws, f"modulation:0,{start_mode};", "restore mode")
        await step(ws, f"vfo:0,0,{start_hz};", "restore VFO")
        print("\ndone")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        sys.exit(1)
