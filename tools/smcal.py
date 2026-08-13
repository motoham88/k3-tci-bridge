#!/usr/bin/env python3
"""Calibrate the S-meter conversions against the one precisely known step
this radio can produce: the 10 dB attenuator (RA00 vs RA01).

The documented anchors imply 6 dB per S-unit below S9. Measurement says
otherwise, so this samples the slope at two different signal levels (preamp
out and in) to check whether it is constant, and fits dB-per-count from the
real steps rather than from the assumption.

Receive only.
"""
import time

import numpy as np
import serial

PORT = "/dev/k3cat"


def ask(ser, cmd, wait=0.12):
    ser.reset_input_buffer()
    ser.write(cmd.encode() + b";")
    ser.flush()
    if wait:
        time.sleep(wait)
    return ser.read_until(b";").decode("ascii", "replace").strip()


def meters(ser, n=12, settle=1.5):
    """Median of several reads; the AGC wanders, so a single sample is noisy."""
    time.sleep(settle)
    sm, smh = [], []
    for _ in range(n):
        r = ask(ser, "SMH")
        if r.startswith("SMH") and len(r) >= 7:
            try:
                smh.append(int(r[3:6]))
            except ValueError:
                pass
        r = ask(ser, "SM")
        if r.startswith("SM") and len(r) >= 7:
            try:
                sm.append(int(r[2:6]))
            except ValueError:
                pass
        time.sleep(0.05)
    return (float(np.median(sm)) if sm else None,
            float(np.median(smh)) if smh else None)


with serial.Serial(PORT, 38400, timeout=0.5) as ser:
    time.sleep(0.2)
    ask(ser, "K31", wait=0.3)
    saved = {c: ask(ser, c) for c in ("PA", "RA", "FA", "MD")}
    print(f"saved: {saved}\n")

    try:
        print("=== 10 dB attenuator step, sampled at two signal levels ===")
        print(f"   {'preamp':<8} {'RA':<6} {'SM':>6} {'SMH':>6}")
        rows = {}
        for pa in ("0", "1"):
            ask(ser, f"PA{pa}", wait=0.3)
            for ra in ("00", "01"):
                ask(ser, f"RA{ra}", wait=0.3)
                sm, smh = meters(ser)
                rows[(pa, ra)] = (sm, smh)
                print(f"   PA{pa:<6} RA{ra:<4} {sm:>6} {smh:>6}")

        print("\n=== counts per 10 dB (from the attenuator step) ===")
        sm_slopes, smh_slopes = [], []
        for pa in ("0", "1"):
            a, b = rows[(pa, "00")], rows[(pa, "01")]
            if None in a or None in b:
                continue
            d_sm, d_smh = a[0] - b[0], a[1] - b[1]
            sm_slopes.append(d_sm)
            smh_slopes.append(d_smh)
            print(f"   preamp {pa}:  SM {a[0]:.0f} -> {b[0]:.0f} "
                  f"({d_sm:.0f} counts)   "
                  f"SMH {a[1]:.0f} -> {b[1]:.0f} ({d_smh:.0f} counts)")

        if sm_slopes and smh_slopes:
            sm_cnt = float(np.mean(sm_slopes))
            smh_cnt = float(np.mean(smh_slopes))
            print(f"\n   SM : {sm_cnt:.2f} counts per 10 dB "
                  f"-> {10/sm_cnt:.2f} dB per count")
            print(f"   SMH: {smh_cnt:.2f} counts per 10 dB "
                  f"-> {10/smh_cnt:.2f} dB per count")
            print(f"\n   (my formulas assumed SM 6.0 dB/count below S9 and "
                  f"SMH 1.37 dB/count)")

            # Anchor at S9. The reference is explicit that SM reads 9 at S9
            # under K31 and SMH reads 40, and S9 = -73 dBm by definition.
            print("\n=== corrected conversions, anchored at S9 = -73 dBm ===")
            print(f"   SM : dBm = -73 + (n - 9)  * {10/sm_cnt:.2f}")
            print(f"   SMH: dBm = -73 + (n - 40) * {10/smh_cnt:.2f}")
            for label, (sm_v, smh_v) in rows.items():
                if None in (sm_v, smh_v):
                    continue
                print(f"   PA{label[0]} RA{label[1]}: "
                      f"SM -> {-73 + (sm_v-9)*(10/sm_cnt):7.1f} dBm    "
                      f"SMH -> {-73 + (smh_v-40)*(10/smh_cnt):7.1f} dBm")
    finally:
        print("\n=== restoring ===")
        for c in ("PA", "RA"):
            ask(ser, saved[c].rstrip(";"), wait=0.35)
        print(f"  {({c: ask(ser, c) for c in ('PA', 'RA')})}")
