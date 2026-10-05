#!/usr/bin/env python3
"""Can a CW message be stopped part-way? Measured over CAT, on the radio.

`cw_macros_stop` through the bridge does not shorten a message: measured on
2026-10-04, a stop 1.5 s into ~16 s of CW let it run to the end. The bridge
writes every chunk with the `W` form, which defers following commands --
including the stop's `RX;` -- until the text has been sent, and its flow
control lets the radio take nearly the whole message before a stop arrives.

This answers what the reference leaves open, before the bridge is changed:

  1. KYW chunks, then RX;      -- today's method, the baseline
  2. KY  chunks, then RX;      -- without W, is RX; acted on at once, and is
                                  the buffered text discarded or resumed?
  3. KY  chunks, then KY @;    -- does '@' cut off a message in progress?
  4. KY  short message alone   -- when does TB's to-send count reach 0,
                                  relative to the last character?

Each case keys with TX; like the bridge and always ends with RX;.

⚠ TRANSMITS FOR REAL unless the radio is in TX TEST. Dummy load or TX TEST.

Talks to the serial port directly, so the bridge must not be running:

    sudo systemctl stop k3-tci
    ./venv/bin/python cwstoptest.py [/dev/k3cat]
    sudo systemctl start k3-tci
"""
import sys
import time

import k3cat

TEXT = "TEST TEST TEST TEST"   # 19 characters: fits one KY packet
STOP_AFTER = 1.5               # seconds after the first packet
WATCH = 16.0                   # seconds to watch after the stop


def t_ms(t0):
    return int((time.monotonic() - t0) * 1000)


def tq(cat):
    r = cat.ask("TQ", timeout=0.3, quiet=True)
    return None if r is None else r == "TQ1;"


def tb_count(cat):
    """Characters still to be sent (0-9, saturating), or None if unanswered."""
    r = cat.ask_text(timeout=0.3)
    if r and r.startswith("TB") and len(r) >= 3 and r[2].isdigit():
        return int(r[2])
    return None


def watch(cat, t0, seconds):
    """Poll TQ and TB; print every change. Returns (unkey_ms, rekeyed)."""
    last = None
    unkey = None
    rekeyed = False
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        state = (tq(cat), tb_count(cat))
        if state != last:
            print(f"   {t_ms(t0):6d} ms  TQ={state[0]}  TB to-send={state[1]}")
            if last and last[0] and state[0] is False and unkey is None:
                unkey = t_ms(t0)
            if unkey is not None and state[0]:
                rekeyed = True
            last = state
        time.sleep(0.05)
    return unkey, rekeyed


def run_case(cat, label, packets, stopper):
    print(f"\n-- {label}")
    cat.send("TX")
    time.sleep(0.3)
    t0 = time.monotonic()
    for p in packets:
        cat.send(p)
    print(f"   sent {packets}")
    time.sleep(STOP_AFTER)
    stop_t = t_ms(t0)
    for s in stopper:
        cat.send(s)
    print(f"   {stop_t:6d} ms  sent {stopper}")
    try:
        unkey, rekeyed = watch(cat, t0, WATCH)
    finally:
        cat.send("RX")     # whatever happened, end in receive
    if unkey is None:
        print("   == radio did not unkey while watched")
    else:
        verdict = "STOPPED" if unkey - stop_t < 1500 else "RAN ON"
        print(f"   == unkeyed {unkey - stop_t} ms after the stop: {verdict}"
              + ("  (then RE-KEYED)" if rekeyed else ""))
    time.sleep(2.0)


def end_of_message(cat):
    print("\n-- 4. KY \"TEST\" left to finish: when does TB reach 0?")
    cat.send("TX")
    time.sleep(0.3)
    t0 = time.monotonic()
    cat.send("KY TEST;")
    print("   sent KY TEST;  (TEST is 21 dot units)")
    try:
        watch(cat, t0, 4.0)
    finally:
        cat.send("RX")
    time.sleep(1.0)


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/k3cat"
    cat = k3cat.K3Cat(port)
    cat.open()
    try:
        md = cat.ask("MD")
        fa = cat.ask("FA")
        ks = cat.ask("KS")
        if md not in ("MD3;", "MD7;"):
            print(f"!! not in CW (MD reply {md!r}); refusing to transmit")
            return 1
        print(f"radio: {md} {fa} {ks}  TX TEST: {cat.tx_test_active()}")
        print("\n⚠  ABOUT TO TRANSMIT. Ctrl-C now to abort.")
        for i in (5, 4, 3, 2, 1):
            print(f"   {i}...", flush=True)
            time.sleep(1)

        run_case(cat, "1. KYW chunks, then RX;  (today's method)",
                 [f"KYW{TEXT} ", f"KYW{TEXT}"], ["RX"])
        run_case(cat, "2. KY chunks, then RX;",
                 [f"KY {TEXT} ", f"KY {TEXT}"], ["RX"])
        run_case(cat, "3. KY chunks, then KY @;",
                 [f"KY {TEXT} ", f"KY {TEXT}"], ["KY @"])
        end_of_message(cat)
        print("\n== done")
    except KeyboardInterrupt:
        print("\n!! interrupted")
    finally:
        cat.send("RX")
        cat.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
