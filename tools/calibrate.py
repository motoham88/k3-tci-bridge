#!/usr/bin/env python3
"""RX calibration against a real signal (WWV 10 MHz AM).

Three jobs:

  1. LIN OUT: pick a level that uses the ADC range without clipping. The
     previous "band noise at -62.5 dBFS" figure was measured into a dummy
     load, so it was the receiver's own noise floor, not a signal.

  2. SM vs SMH: the two S-meter conversions disagreed by 19 dB near the
     noise floor. The discriminator is the ATTENUATOR: RA01 is a documented
     10 dB pad, so whichever formula reports a 10 dB drop when it is
     switched in is the one tracking reality.

  3. Confirm the audio really is WWV -- its 100 Hz time code and 500/600 Hz
     tones should be visible in the spectrum.

Receive only. Restores everything except LIN OUT, which is reported and left
at the chosen value (the original is printed so it can be put back).
"""
import subprocess
import time

import numpy as np
import serial

PORT = "/dev/k3cat"
ALSA = "hw:2,0"
RATE = 48000


def ask(ser, cmd, wait=0.2):
    ser.reset_input_buffer()
    ser.write(cmd.encode() + b";")
    ser.flush()
    if wait:
        time.sleep(wait)
    return ser.read_until(b";").decode("ascii", "replace").strip()


def ds_text(ser):
    ser.reset_input_buffer()
    ser.write(b"DS;")
    ser.flush()
    time.sleep(0.3)
    raw = ser.read(ser.in_waiting or 256)
    if not raw.startswith(b"DS") or len(raw) < 11:
        return repr(raw)
    tbl = {"@": " ", "Q": "O", "V": "U", "M": "N", "K": "H", "W": "I"}
    return "".join(tbl.get(chr(b & 0x7F), chr(b & 0x7F)) for b in raw[2:10])


def set_linout(ser, val):
    ask(ser, "MN032", wait=0.35)
    ask(ser, f"MP{val}", wait=0.35)
    txt = ds_text(ser)
    ask(ser, "MN255", wait=0.35)
    return txt


def get_linout(ser):
    ask(ser, "MN032", wait=0.35)
    mp = ask(ser, "MP", wait=0.3)
    ask(ser, "MN255", wait=0.35)
    return mp


def capture(seconds=2.0):
    subprocess.run(
        ["arecord", "-D", ALSA, "-f", "S16_LE", "-r", str(RATE), "-c", "2",
         "--samples", str(int(RATE * seconds)), "-t", "raw", "/tmp/cal.raw"],
        check=True, capture_output=True)
    pcm = np.fromfile("/tmp/cal.raw", dtype=np.int16).reshape(-1, 2)
    return pcm[:, 0].astype(np.float64) / 32768.0


def db(x):
    return 20 * np.log10(max(float(x), 1e-12))


def smh_dbm(n):
    return -121 + (n - 5) * (48 / 35) if n <= 40 else -73 + (n - 40)


def sm_dbm(n):
    return -73 - 6 * (9 - n) if n <= 9 else -73 + 5 * (n - 9)


def read_meters(ser, samples=6):
    sm, smh = [], []
    for _ in range(samples):
        r = ask(ser, "SMH", wait=0.12)
        if r.startswith("SMH") and len(r) >= 7:
            try:
                smh.append(int(r[3:6]))
            except ValueError:
                pass
        r = ask(ser, "SM", wait=0.12)
        if r.startswith("SM") and len(r) >= 7:
            try:
                sm.append(int(r[2:6]))
            except ValueError:
                pass
    return (float(np.median(sm)) if sm else None,
            float(np.median(smh)) if smh else None)


with serial.Serial(PORT, 38400, timeout=0.5) as ser:
    time.sleep(0.2)
    ask(ser, "K31", wait=0.3)
    saved = {c: ask(ser, c) for c in ("FA", "MD", "PA", "RA", "AG")}
    lin0 = get_linout(ser)
    print(f"state: {saved}")
    print(f"LIN OUT as found: {lin0}\n")

    sm, smh = read_meters(ser)
    print(f"signal check: SM={sm} SMH={smh}")
    if not smh or smh < 10:
        print("  WARNING: very little signal. Is the antenna connected and "
              "WWV audible?\n")
    else:
        print(f"  SMH {smh:.0f} -> approx {smh_dbm(smh):.0f} dBm. Good.\n")

    try:
        # ---------- 1. LIN OUT sweep ----------
        print("=== LIN OUT sweep (AGC is doing the levelling) ===")
        print(f"   {'LIN OUT':<10} {'display':<12} {'RMS':>10} {'peak':>10}")
        results = []
        for val in ("005", "010", "020", "030", "040", "050", "060"):
            txt = set_linout(ser, val)
            time.sleep(0.5)
            a = capture()
            rms, peak = np.sqrt((a ** 2).mean()), np.abs(a).max()
            results.append((val, rms, peak))
            print(f"   {val:<10} {txt.strip():<12} "
                  f"{db(rms):>7.1f}dB {db(peak):>7.1f}dB")

        # Choose the highest setting whose peak still leaves ~10 dB headroom.
        ok = [r for r in results if db(r[2]) < -10.0]
        choice = ok[-1] if ok else results[0]
        print(f"\n   -> choosing LIN OUT {choice[0]}: peak "
              f"{db(choice[2]):.1f} dBFS, about "
              f"{-db(choice[2]):.0f} dB of headroom")
        set_linout(ser, choice[0])
        time.sleep(0.4)

        # ---------- 2. SM vs SMH against a known 10 dB step ----------
        print("\n=== S-meter: which formula tracks a known 10 dB step? ===")
        ask(ser, "PA0", wait=0.35)
        ask(ser, "RA00", wait=0.35)
        time.sleep(1.2)
        sm_a, smh_a = read_meters(ser)
        ask(ser, "RA01", wait=0.35)          # documented 10 dB pad
        time.sleep(1.2)
        sm_b, smh_b = read_meters(ser)
        print(f"   attenuator OUT: SM={sm_a} SMH={smh_a}")
        print(f"   attenuator IN : SM={sm_b} SMH={smh_b}   (a true -10 dB)")
        if None not in (sm_a, sm_b, smh_a, smh_b):
            d_sm = sm_dbm(sm_b) - sm_dbm(sm_a)
            d_smh = smh_dbm(smh_b) - smh_dbm(smh_a)
            print(f"   SM  formula reports {d_sm:+.1f} dB  "
                  f"(error {d_sm+10:+.1f})")
            print(f"   SMH formula reports {d_smh:+.1f} dB  "
                  f"(error {d_smh+10:+.1f})")
            better = "SMH" if abs(d_smh + 10) <= abs(d_sm + 10) else "SM"
            print(f"   -> {better} tracks the real step better")
        ask(ser, "RA00", wait=0.35)

        # ---------- 3. is it really WWV? ----------
        print("\n=== audio spectrum (expect WWV: 100 Hz code, 500/600 Hz "
              "tones) ===")
        ask(ser, saved["PA"].rstrip(";"), wait=0.35)
        time.sleep(1.0)
        a = capture(3.0)
        win = np.hanning(len(a))
        spec = np.abs(np.fft.rfft(a * win))
        freqs = np.fft.rfftfreq(len(a), 1.0 / RATE)
        band = (freqs > 40) & (freqs < 4000)
        f_b, s_b = freqs[band], spec[band]
        floor = np.median(s_b)
        idx = np.argsort(s_b)[::-1]
        keep = []
        for i in idx:
            if all(abs(f_b[i] - f_b[j]) > 30 for j in keep):
                keep.append(i)
            if len(keep) >= 6:
                break
        for i in sorted(keep, key=lambda j: -s_b[j]):
            print(f"   {f_b[i]:7.1f} Hz   {20*np.log10(s_b[i]/floor):5.1f} dB "
                  f"above floor")
        print(f"   overall RMS {db(np.sqrt((a**2).mean())):.1f} dBFS, "
              f"peak {db(np.abs(a).max()):.1f} dBFS")

    finally:
        print("\n=== restoring (LIN OUT intentionally left at the new value) ===")
        for c in ("PA", "RA", "AG", "MD", "FA"):
            v = saved.get(c, "")
            if v and not v.startswith("?"):
                ask(ser, v.rstrip(";"), wait=0.4)
        now = {c: ask(ser, c) for c in ("FA", "MD", "PA", "RA", "AG")}
        print(f"  {now}")
        print(f"  match: {'yes' if now == saved else 'NO'}")
        print(f"  LIN OUT: was {lin0}, now {get_linout(ser)}")
