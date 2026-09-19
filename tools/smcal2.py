#!/usr/bin/env python3
"""Calibrate the S-meter against a signal generator -- absolute, not relative.

`smcal.py` measures the SLOPE of the meter from the one precisely known step
the radio can make by itself (the 10 dB attenuator, RA00 vs RA01). That is
worth having and is independent of any generator's absolute accuracy: 10 dB
is 10 dB whatever the source's level error. What it can never give is the
ANCHOR -- where the curve actually sits in dBm -- because the radio has no
idea what is arriving at its antenna jack.

A generator gives the anchor. Known level in, count out, across the range,
which is the only way to test three things the current conversion assumes
rather than knows:

  1. That S9 really is SMH 40 / SM 9 on THIS radio.
  2. That the SMH curve is piecewise with a break at 40 -- the reference
     calls its figures "approximate values", so the break may not be real.
  3. What the SM scale does below S9, where the reference gives no anchor at
     all and the present 6 dB/count term is pure extrapolation.

Receive only. Nothing here keys the radio.

    Bench order
    -----------
    1. Connect nothing yet. Run `txtest_off.py` and confirm TX TEST is ON.
       This tool re-checks it and aborts if it is not.
    2. Generator to the K3 antenna jack. Set it to minimum output.
    3. Run this. It measures the noise floor first, then prompts per level.

    WHY TX TEST MATTERS HERE, and it is not about this script: nothing below
    transmits. The hazard is a key-down from any other cause -- the front
    panel, a stuck PTT, the DTR/RTS trap recorded in tools/README.md that
    already keyed this radio once -- while a service monitor's generator
    output is on the antenna jack. That puts the transmitter into the
    generator's step attenuator, which is the expensive part of the
    instrument. TX TEST means no RF leaves the radio, so it is asserted
    before any level is read and re-checked at the end.
"""
import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone

import numpy as np

import k3serial

PORT = "/dev/k3cat"

# Documented full-scale counts, mirroring bridge/tci.py. A reading outside
# these means the reply was not what we think it was, not a huge signal.
SMH_MAX, SM_MAX = 140, 21

# How far the P3 may disagree with the generator before the point is flagged
# on the bench. The two agreed within 0.01 dB at -73 and -125.
P3_TOL = 1.5

# State pinned for the run, saved and put back afterwards.
SAVE = ("PA", "RA", "GT", "FA", "MD")

# Restored afterwards, and each readback checked: a dropped SET here is the
# one that leaves the station parked on the test frequency.
RESTORE = ("PA", "RA", "GT", "FA", "MD")


def ask(ser, cmd, wait=0.12):
    ser.reset_input_buffer()
    ser.write(cmd.encode() + b";")
    ser.flush()
    if wait:
        time.sleep(wait)
    return ser.read_until(b";").decode("ascii", "replace").strip()


def raw(ser, cmd, wait=0.3):
    ser.reset_input_buffer()
    ser.write(cmd.encode() + b";")
    ser.flush()
    time.sleep(wait)
    return ser.read(ser.in_waiting or 256)


# --- Generator level <-> dBm, 50 ohm. ---------------------------------
#
# dBm = 20*log10(uV) - 106.99, which puts 50 uV at -73.01 dBm. That is not a
# coincidence and it is the check worth remembering: S9 is DEFINED as
# -73 dBm, and 50 uV into 50 ohms is exactly that.
#
# THE 6 dB TRAP. A generator calibrated in microvolts states either the
# open-circuit voltage (EMF) or the voltage across a matched load (PD).
# EMF is twice PD -- 6.02 dB -- because the source's own 50 ohms forms a
# divider with the 50 ohm load. Read the wrong one and every point in the
# run is off by 6.02 dB, in the same direction, by exactly the amount that
# a clean straight-line fit with tiny residuals will happily absorb into
# its intercept. The slope survives; the anchor is wrong. Since the anchor
# is the entire reason this tool exists, --emf is not a convenience flag.
#
# Settle it from the CE-4000's manual, not from the front panel legend.
UV_REF_DB = 106.9897   # 10*log10(1e-12 / 50 / 1e-3), exact for 50 ohm
EMF_TO_PD_DB = 6.0206  # 20*log10(2)


def uv_to_dbm(uv, emf=False):
    """Microvolts to dBm. `emf` if the generator states open-circuit volts."""
    if uv <= 0:
        return None
    import math
    dbm = 20 * math.log10(uv) - UV_REF_DB
    return dbm - EMF_TO_PD_DB if emf else dbm


def dbm_to_uv(dbm, emf=False):
    if emf:
        dbm += EMF_TO_PD_DB
    return 10 ** ((dbm + UV_REF_DB) / 20)


# --- The conversions as they stand today, so every point prints predicted
# --- vs actual while the bench is still set up. A units mistake (a
# --- generator reading dBuV entered as dBm is ~107 dB out) then shows on
# --- the first point rather than in the fit hours later. Kept in step with
# --- bridge/tci.py read_smeter(). SMH is the curve measured 2026-09-19.
def smh_to_dbm(n):
    if n <= 55:
        return -118.71 + 1.222 * n
    return -51.5 + 0.78 * (n - 55)


def sm_to_dbm(n):
    if n <= 9:
        return -73 - 6 * (9 - n)
    return -73 + 5 * (n - 9)


def col(v, width, spec):
    """One column, or a dash when the meter gave nothing usable.

    A blank or a zero here would read as a measurement; a dash cannot.
    """
    return f"{'--':>{width}}" if v is None else f"{v:{spec}}"


def tx_test_on(ser):
    """(state, byte) from IC byte a bit 5, or (None, None) if unreadable."""
    ic = raw(ser, "IC")
    if not ic.startswith(b"IC") or len(ic) < 8:
        return None, None
    return bool(ic[2] & 0x20), ic[2]


def meters(ser, n=12, settle=1.5):
    """Median of several reads of both meters.

    The AGC wanders, so a single sample is noise. Median rather than mean
    because a single corrupted digit at 38400 baud is a wild outlier, and
    out-of-range counts are dropped outright for the reason bridge/tci.py
    range-checks them: nothing downstream can tell a wrong number from a
    strong signal.
    """
    time.sleep(settle)
    sm, smh = [], []
    for _ in range(n):
        r = ask(ser, "SMH")
        if r.startswith("SMH") and len(r) >= 7:
            try:
                v = int(r[3:6])
                if 0 <= v <= SMH_MAX:
                    smh.append(v)
            except ValueError:
                pass
        r = ask(ser, "SM")
        if r.startswith("SM") and len(r) >= 7:
            try:
                v = int(r[2:6])
                if 0 <= v <= SM_MAX:
                    sm.append(v)
            except ValueError:
                pass
        time.sleep(0.05)
    return (float(np.median(sm)) if sm else None,
            float(np.median(smh)) if smh else None,
            len(sm), len(smh))


def level(p):
    """The input level a point is fitted at: the P3 reading when one was
    recorded, otherwise the generator level."""
    return p["p3_dbm"] if p.get("p3_dbm") is not None else p["dbm"]


def fit(points, floor_smh, floor_sm):
    """Report what the measured points actually say.

    Deliberately does NOT emit a replacement formula for bridge/tci.py. It
    prints the fits and their residuals; deciding the shape of the curve is
    a judgement about which segments had enough points and enough range,
    and that belongs to whoever reads this with the bench notes beside them.
    """
    print("\n=== fit ===")
    for key, floor, brk, assumed, label in (
            ("smh", floor_smh, 40, 40, "SMH"),
            ("sm", floor_sm, 9, 9, "SM")):
        pts = [p for p in points if p.get(key) is not None]

        # The P3 is the reference: the CE-4000 is uncalibrated and ageing, so
        # where it and the P3 disagree the P3 is believed. Points it corrects
        # are listed, not excluded. Runs without a p3_dbm column fall back to
        # the generator level.
        moved = [p for p in pts if p.get("p3_dbm") is not None
                 and abs(p["p3_dbm"] - p["dbm"]) > P3_TOL]
        if moved:
            print(f"\n{key.upper()}: {len(moved)} point(s) fitted at the P3 "
                  f"level, not the generator's: "
                  + ", ".join(f"{p['dbm']:.0f}->{p['p3_dbm']:.1f}"
                              for p in moved))

        # Drop anything sitting in the receiver's own noise. Below about two
        # counts above the measured floor the meter is reading the radio,
        # not the generator, and those points would flatten the low end into
        # something that looks like a real calibration result.
        if floor is not None:
            clean = [p for p in pts if p[key] >= floor + 2]
            dropped = len(pts) - len(clean)
        else:
            clean, dropped = pts, 0

        if len(clean) < 3:
            print(f"\n{label}: {len(clean)} points clear of the noise floor "
                  f"-- not fitting")
            continue

        x = np.array([p[key] for p in clean], float)
        y = np.array([level(p) for p in clean], float)
        slope, intercept = np.polyfit(x, y, 1)
        resid = y - (slope * x + intercept)

        print(f"\n{label}: {len(clean)} points"
              + (f" ({dropped} dropped as noise-floor)" if dropped else ""))
        print(f"  single line : dBm = {intercept:+.2f} + {slope:.4f} * n"
              f"   ({slope:.3f} dB/count)")
        print(f"  residual    : max {np.abs(resid).max():.2f} dB, "
              f"rms {np.sqrt((resid ** 2).mean()):.2f} dB")

        # Where does this radio actually put S9 (-73 dBm by definition)?
        if slope:
            n_s9 = (-73 - intercept) / slope
            print(f"  S9 (-73 dBm) falls at n = {n_s9:.1f}"
                  f"   (conversion assumes {assumed})")

        # Test the assumed breakpoint by fitting each side separately. If the
        # slopes agree the piecewise structure is not earning its place; if
        # they differ the break is real and this says how much work it does.
        lo = [p for p in clean if p[key] <= brk]
        hi = [p for p in clean if p[key] > brk]
        if len(lo) >= 3 and len(hi) >= 3:
            ls, _ = np.polyfit([p[key] for p in lo], [level(p) for p in lo], 1)
            hs, _ = np.polyfit([p[key] for p in hi], [level(p) for p in hi], 1)
            print(f"  below n={brk}: {ls:.3f} dB/count    "
                  f"above n={brk}: {hs:.3f} dB/count")
            print(f"  -> slopes {'agree; the break may not be real' if abs(ls - hs) < 0.15 else 'differ; the break is doing work'}")
        else:
            print(f"  ({len(lo)} below / {len(hi)} above n={brk} "
                  f"-- cannot test the breakpoint)")


def controls(ser, args):
    """Run every control first and abort if any fails.

    The house pattern, and it is not ceremony: three results in this project
    looked conclusive and were wrong, including the S-meter calibration this
    tool replaces, which was measuring propagation rather than the
    attenuator. Each was caught by a control.
    """
    print("=== controls ===")

    ask(ser, "K31", wait=0.3)
    k3 = ask(ser, "K3")
    print(f"  K3 reply           {k3!r}")
    if not k3.startswith("K3"):
        sys.exit("  ABORT: no sane reply to K3; is this the right port?")

    tq = ask(ser, "TQ")
    print(f"  TX state           {tq!r}")
    if tq != "TQ0;":
        sys.exit("  ABORT: radio is transmitting, or TQ is unreadable.")

    on, a = tx_test_on(ser)
    byte = "??" if a is None else f"0x{a:02X}"
    print(f"  TX TEST            {on}   (IC byte a = {byte})")
    if on is not True:
        sys.exit("  ABORT: TX TEST is not confirmed ON. With a generator on "
                 "the antenna jack,\n  any key-down goes into its step "
                 "attenuator. Run txtest_off.py to inspect it.")

    saved = {c: ask(ser, c) for c in SAVE}
    print(f"  saved              {saved}")
    return saved


def pin(ser, args):
    """Fix every gain stage that moves the meter, and verify each one.

    A CAT SET can be dropped silently on this radio -- an MD2 was ignored
    with no `?;` and no other sign -- so each of these is read back. An
    unnoticed preamp left in is a 10-plus dB error in every point that
    follows, and nothing later in the run would reveal it.
    """
    ask(ser, "PA0", wait=0.3)
    ask(ser, "RA00", wait=0.3)
    ask(ser, args.agc, wait=0.3)
    ask(ser, f"FA{int(round(args.freq * 1e6)):011d}", wait=0.3)
    ask(ser, args.mode, wait=0.3)

    pinned = {c: ask(ser, c) for c in SAVE}
    print(f"  pinned             {pinned}")
    if not pinned["PA"].startswith("PA0"):
        sys.exit("  ABORT: preamp did not go out; a CAT SET was dropped.")
    if not pinned["RA"].startswith("RA00"):
        sys.exit("  ABORT: attenuator did not go out; a CAT SET was dropped.")
    if not pinned["MD"].startswith(args.mode):
        sys.exit(f"  ABORT: mode did not take ({pinned['MD']!r}); "
                 f"a CAT SET was dropped.")
    # The K3's S-meter is derived from the AGC voltage, so AGC slow vs fast
    # moves the count at an unchanged input level. Pinned and verified
    # rather than left to the operator to remember.
    if not pinned["GT"].startswith(args.agc):
        sys.exit(f"  ABORT: AGC did not take ({pinned['GT']!r}, wanted "
                 f"{args.agc}); a CAT SET was dropped.")

    print("\n  RF GAIN must be at maximum for the whole run -- it moves the "
          "meter\n  and is not re-read between points. AGC is pinned above.")
    input("  Confirm RF GAIN is at max, then press Enter: ")
    return pinned


def noise_floor(ser, args):
    print("\n=== noise floor ===")
    print("  Set the generator to MINIMUM output, or disconnect it.")
    input("  Press Enter when done: ")
    f_sm, f_smh, nsm, nsmh = meters(ser, args.n, args.settle)
    print(f"  floor: SM {f_sm}  ({nsm} reads)    SMH {f_smh}  ({nsmh} reads)")
    if f_smh is None:
        sys.exit("  ABORT: no usable SMH reading even at the noise floor.")
    print("  Points within 2 counts of this are measuring the receiver, not "
          "the\n  generator, and are dropped from the fit.")
    return f_sm, f_smh


def sweep(ser, args):
    unit = "uV" if args.units == "uv" else "dBm"
    print("\n=== sweep ===")
    print(f"  Enter the level the CE-4000 front panel reads, in {unit}"
          + (" (EMF)" if args.emf and args.units == "uv" else "")
          + ".")
    print("  Blank = accept the suggested level.   s = skip.   q = stop.")
    print(f"\n  {'target':>10} {'entered':>10} {'dBm':>8} {'SM':>6} "
          f"{'SMH':>6} {'SM->dBm':>9} {'SMH->dBm':>9} {'SMH err':>8}")

    # --levels replaces the even sweep with named points, for re-measuring
    # the few that misbehaved without walking the whole range again.
    if args.levels:
        levels = [float(v) for v in args.levels.split(",")]
    else:
        levels, v = [], args.start
        while v <= args.stop + 1e-9:
            levels.append(v)
            v += args.step

    points = []
    i = 0
    while i < len(levels):
        level = levels[i]
        if args.units == "uv":
            tgt = dbm_to_uv(level, args.emf)
            shown = f"{tgt:.4g} uV"
        else:
            tgt = level
            shown = f"{level:.1f} dBm"
        try:
            s = input(f"  set generator to {shown:>14}, "
                      f"then Enter/value/s/q: ")
        except EOFError:
            break
        s = s.strip().lower()
        if s == "q":
            break
        if s == "s":
            i += 1
            continue
        if s:
            try:
                entered = float(s)
            except ValueError:
                print("    not a number; try again")
                continue
        else:
            entered = tgt

        if args.units == "uv":
            actual = uv_to_dbm(entered, args.emf)
            if actual is None:
                print("    microvolts must be positive; try again")
                continue
        else:
            actual = entered

        sm, smh, nsm, nsmh = meters(ser, args.n, args.settle)
        p_sm = sm_to_dbm(sm) if sm is not None else None
        p_smh = smh_to_dbm(smh) if smh is not None else None
        err = None if p_smh is None else p_smh - actual

        print(f"  {tgt:>10.4g} {entered:>10.4g} {actual:>8.1f} "
              f"{col(sm, 6, '>6.1f')} {col(smh, 6, '>6.1f')} "
              f"{col(p_sm, 9, '>9.1f')} {col(p_smh, 9, '>9.1f')} "
              f"{col(err, 8, '>+8.1f')}")

        # The independent control. The partial run had it and was clean; the
        # full sweep was run without it and its level column is only what the
        # script SUGGESTED, so its outliers could not be told apart from a
        # generator that was not where it was asked to be.
        p3 = None
        while True:
            t = input("    P3 reads (dBm, blank = no reading): ").strip()
            if not t:
                break
            try:
                p3 = float(t)
                break
            except ValueError:
                print("    not a number; try again")
        if p3 is not None and abs(p3 - actual) > P3_TOL:
            print(f"    P3 DISAGREES by {p3 - actual:+.1f} dB -- the generator "
                  f"is not at {actual:.1f} dBm.\n    Fitted at the P3's "
                  f"{p3:.1f} dBm. Re-set the generator and redo it if a\n"
                  f"    level near {actual:.1f} matters.")

        points.append({"requested": level, "dbm": actual,
                       "entered": entered, "units": args.units,
                       "sm": sm, "smh": smh, "n_sm": nsm, "n_smh": nsmh,
                       "pred_sm": p_sm, "pred_smh": p_smh, "p3_dbm": p3})
        i += 1

    return points


def restore(ser, saved):
    print("\n=== restoring ===")
    if not saved:
        print("  nothing saved to restore")
        return
    for c in RESTORE:
        v = saved.get(c, "")
        if v.startswith(c) and v.endswith(";"):
            ask(ser, v[:-1], wait=0.35)

    # Checked, not just printed. pin() verifies its own SETs because a
    # dropped one corrupts the measurements; this one is verified because a
    # dropped one leaves the radio on the test frequency after the run, and
    # nothing else would say so.
    bad = []
    for c in RESTORE:
        want, got = saved.get(c, ""), ask(ser, c)
        print(f"  {c:<4} {got!r}" + ("" if got == want else f"   WANTED {want!r}"))
        if got != want:
            bad.append(c)
    if bad:
        print(f"  WARNING: {', '.join(bad)} did not restore -- a CAT SET was "
              f"dropped.\n  Put these back at the radio before operating.")

    on, _ = tx_test_on(ser)
    print(f"  TX TEST            {on}")
    if on is not True:
        print("  WARNING: TX TEST is no longer on. Disconnect the generator "
              "before\n  anything can key this radio.")


def write_out(path, points, f_sm, f_smh, pinned, args):
    """Raw pairs to disk.

    Bench time is the expensive part of this, so the fit is redoable with
    --fit without occupying the radio again.
    """
    meta = {
        "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "freq_mhz": args.freq, "mode": args.mode, "agc": args.agc,
        "units": args.units, "emf": args.emf,
        "generator": args.generator,
        "generator_accuracy_db": args.gen_accuracy,
        "floor_sm": f_sm, "floor_smh": f_smh,
        "radio_state": pinned,
        "n_per_point": args.n, "settle_s": args.settle,
    }
    with open(path, "w", newline="") as f:
        f.write("# " + json.dumps(meta) + "\n")
        w = csv.DictWriter(f, fieldnames=list(points[0].keys()))
        w.writeheader()
        w.writerows(points)
    print(f"\nwrote {len(points)} points to {path}")


def load(path):
    meta, rows = {}, []
    with open(path) as f:
        first = f.readline()
        if first.startswith("# "):
            meta = json.loads(first[2:])
        else:
            f.seek(0)
        for r in csv.DictReader(f):
            row = {}
            for k, v in r.items():
                if v in ("", "None"):
                    row[k] = None
                else:
                    try:
                        row[k] = float(v)
                    except ValueError:
                        row[k] = v
            rows.append(row)
    return rows, meta


def main():
    ap = argparse.ArgumentParser(
        description="Calibrate the K3 S-meter against a signal generator.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--freq", type=float, default=14.100,
                    help="test frequency in MHz (default 14.100). The K3's "
                         "S-meter is not identical across bands -- record "
                         "which one this run used.")
    ap.add_argument("--mode", default="MD3",
                    help="CAT mode command (default MD3 = CW, whose narrow "
                         "filter puts the noise floor lowest and so extends "
                         "usable range at the bottom)")
    ap.add_argument("--start", type=float, default=-125.0,
                    help="sweep start in dBm (the steps are even in dB even "
                         "when the generator is dialled in uV)")
    ap.add_argument("--stop", type=float, default=-20.0, help="in dBm")
    ap.add_argument("--step", type=float, default=5.0, help="in dB")
    ap.add_argument("--levels", metavar="DBM,DBM,...",
                    help="measure only these levels, in order, instead of "
                         "the --start/--stop/--step sweep")
    ap.add_argument("--units", choices=("uv", "dbm"), default="uv",
                    help="what the generator's front panel reads. The "
                         "CE-4000 at this station reads microvolts.")
    ap.add_argument("--emf", action="store_true",
                    help="the generator states OPEN-CIRCUIT microvolts (EMF) "
                         "rather than volts across a matched load (PD). EMF "
                         "is 6.02 dB higher. Getting this wrong biases every "
                         "point by 6 dB with no visible symptom -- the fit "
                         "absorbs it into the intercept and still looks "
                         "clean. Check the CE-4000 manual.")
    ap.add_argument("--agc", default="GT004",
                    help="AGC pinned for the run (GT004 = slow, GT002 = "
                         "fast; K3 has only the two). The S-meter is derived "
                         "from the AGC voltage, so this must not move "
                         "mid-run. Slow by default: steadier on a carrier.")
    ap.add_argument("--n", type=int, default=12, help="reads per point")
    ap.add_argument("--settle", type=float, default=1.5,
                    help="seconds to let the AGC settle before reading")
    ap.add_argument("--out", default="smcal2.csv")
    ap.add_argument("--generator", default="Cushman CE-4000")
    ap.add_argument("--gen-accuracy", type=float, default=None,
                    help="the generator's own level accuracy in dB, from its "
                         "manual. The calibration cannot honestly claim to be "
                         "better than its source, so record the bound.")
    ap.add_argument("--fit", metavar="CSV",
                    help="re-fit a saved run; does not touch the radio")
    ap.add_argument("--port", default=PORT)
    args = ap.parse_args()

    if args.fit:
        points, meta = load(args.fit)
        print(f"=== {args.fit} ===")
        for k, v in meta.items():
            print(f"  {k}: {v}")
        fit(points, meta.get("floor_smh"), meta.get("floor_sm"))
        return

    if args.gen_accuracy is None:
        print("NOTE: --gen-accuracy not given. Look up the CE-4000's level "
              "accuracy and\n      record it -- the result is only as good "
              "as its source.\n")

    saved, points, f_sm, f_smh, pinned = None, [], None, None, {}
    with k3serial.open_k3(args.port) as ser:
        time.sleep(0.2)
        try:
            saved = controls(ser, args)
            pinned = pin(ser, args)
            f_sm, f_smh = noise_floor(ser, args)
            points = sweep(ser, args)
        finally:
            # A failed restore must not lose the measurements, so the write
            # and the fit happen after this, outside the serial context.
            try:
                restore(ser, saved)
            except Exception as e:
                print(f"  restore failed: {e}")

    if not points:
        print("no points collected")
        return
    write_out(args.out, points, f_sm, f_smh, pinned, args)
    fit(points, f_smh, f_sm)


if __name__ == "__main__":
    main()
