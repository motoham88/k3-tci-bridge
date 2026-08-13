#!/usr/bin/env python3
"""K3 sideband polarity via WWV -- v2.

Fixes two flaws in v1:
  1. Mode SETs were never verified. Every SET now reads back and retries,
     and the run aborts if the radio didn't actually change mode.
  2. It searched for the strongest peak anywhere in the audio band. WWV is
     amplitude-modulated with 500/600 Hz tones and a 100 Hz time code, and
     those can exceed the carrier beat. v2 measures energy at the
     *predicted* frequency instead, which is what the hypothesis is about.

Receive only. Restores VFO, mode, sub-mode and filters on exit.
"""
import subprocess
import sys
import time
import wave

import numpy as np
import serial

PORT = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL047393-if00-port0"
ALSA = "hw:2,0"
RATE = 48000
CAP = "/tmp/wwv_cap.wav"
WWV = [15_000_000, 10_000_000, 20_000_000, 5_000_000]
OFF_SSB = 1000      # dial offset for SSB/DATA tests
OFF_CW = 200        # dial offset for CW tests
PRESENT = 10.0      # dB above floor to count as present


def ask(ser, cmd, wait=0.2):
    ser.reset_input_buffer()
    ser.write(cmd.encode() + b";")
    ser.flush()
    time.sleep(wait)
    return ser.read(ser.in_waiting or 256).decode("ascii", "replace").strip()


def set_verified(ser, cmd, query, expect, tries=3):
    """Send a SET and confirm the radio actually took it."""
    for _ in range(tries):
        ask(ser, cmd, wait=0.45)
        time.sleep(0.35)
        got = ask(ser, query)
        if got == expect:
            return True
        time.sleep(0.3)
    print(f"      !! {cmd}; did not take -- {query}; reads {got!r}, "
          f"expected {expect!r}")
    return False


def capture(seconds=2.0):
    subprocess.run(
        ["arecord", "-D", ALSA, "-f", "S16_LE", "-r", str(RATE), "-c", "2",
         "--samples", str(int(seconds * RATE)), "-t", "wav", CAP],
        check=True, capture_output=True,
    )
    with wave.open(CAP) as w:
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return raw.reshape(-1, 2)[:, 0].astype(np.float64) / 32768.0


def spectrum(audio):
    win = np.hanning(len(audio))
    spec = np.abs(np.fft.rfft(audio * win))
    freqs = np.fft.rfftfreq(len(audio), 1.0 / RATE)
    band = (freqs >= 80) & (freqs <= 3800)
    return freqs[band], spec[band]


def energy_at(freqs, spec, target, halfwidth=10.0):
    """dB of the strongest bin within +/-halfwidth of target, above the
    median noise floor of the audio band."""
    floor = float(np.median(spec)) or 1e-12
    sel = (freqs >= target - halfwidth) & (freqs <= target + halfwidth)
    if not sel.any():
        return -99.0
    return float(20 * np.log10(spec[sel].max() / floor))


def top_peaks(freqs, spec, n=3):
    floor = float(np.median(spec)) or 1e-12
    idx = np.argsort(spec)[::-1]
    keep = []
    for i in idx:
        if all(abs(freqs[i] - freqs[j]) > 40 for j in keep):
            keep.append(i)
        if len(keep) >= n:
            break
    return "  ".join(f"{freqs[i]:6.1f}Hz/{20*np.log10(spec[i]/floor):.0f}dB"
                     for i in keep)


def look(ser, dial, targets, label, settle=1.4):
    """Tune, capture, and report energy at each predicted frequency."""
    ask(ser, f"FA{dial:011d}", wait=0.3)
    time.sleep(settle)
    fa = ask(ser, "FA")
    freqs, spec = spectrum(capture())
    got = {t: energy_at(freqs, spec, t) for t in targets}
    shown = "  ".join(f"{t:.0f}Hz={got[t]:5.1f}dB" for t in targets)
    print(f"    {label:<26} {fa}  {shown}    [peaks {top_peaks(freqs, spec)}]")
    return got


def main():
    ser = serial.Serial(PORT, 38400, timeout=0.5)
    time.sleep(0.2)
    ask(ser, "K31", wait=0.3)
    saved = {c: ask(ser, c) for c in ("FA", "MD", "DT", "BW", "IS")}
    print("saved state:", saved, "\n")

    try:
        # ---------- pick a WWV frequency, in verified USB ----------
        print("=== selecting WWV frequency (USB, dial 1 kHz low) ===")
        if not set_verified(ser, "MD2", "MD", "MD2;"):
            print("Cannot enter USB. Aborting."); return 1
        set_verified(ser, "BW0280", "BW", "BW0280;")
        best, best_e = None, -99.0
        for f in WWV:
            e = look(ser, f - OFF_SSB, [OFF_SSB], f"WWV {f/1e6:.0f} MHz")
            if e[OFF_SSB] > best_e:
                best, best_e = f, e[OFF_SSB]
        if best_e < PRESENT:
            print("\nNo WWV carrier anywhere. Propagation/antenna, not the "
                  "bridge. Aborting."); return 1
        print(f"\n  --> WWV {best/1e6:.0f} MHz, carrier beat {best_e:.1f} dB\n")

        # ---------- controls ----------
        print("=== CONTROLS: energy at the 1000 Hz carrier beat ===")
        results = {}
        for md, name in (("MD2", "USB"), ("MD1", "LSB")):
            if not set_verified(ser, md, "MD", md + ";"):
                print(f"  cannot enter {name}; aborting"); return 1
            set_verified(ser, "BW0280", "BW", "BW0280;")
            lo = look(ser, best - OFF_SSB, [OFF_SSB], f"{name} dial LOW")
            hi = look(ser, best + OFF_SSB, [OFF_SSB], f"{name} dial HIGH")
            side = "UPPER" if lo[OFF_SSB] > hi[OFF_SSB] else "LOWER"
            results[md] = side
            print(f"    => {name} = {side} sideband\n")

        ok = results["MD2"] == "UPPER" and results["MD1"] == "LOWER"
        print(f"  CONTROLS {'PASS' if ok else 'FAIL'} "
              f"(USB={results['MD2']}, LSB={results['MD1']})\n")
        if not ok:
            print("  Method is not discriminating -- stopping rather than "
                  "reporting unsafe results.")
            return 1

        # ---------- DATA modes ----------
        print("=== DATA modes (DATA A sub-mode) ===")
        data = {}
        for md in ("MD6", "MD9"):
            if not set_verified(ser, md, "MD", md + ";"):
                continue
            set_verified(ser, "DT0", "DT", "DT0;")
            set_verified(ser, "BW0280", "BW", "BW0280;")
            lo = look(ser, best - OFF_SSB, [OFF_SSB], f"{md} dial LOW")
            hi = look(ser, best + OFF_SSB, [OFF_SSB], f"{md} dial HIGH")
            data[md] = "UPPER" if lo[OFF_SSB] > hi[OFF_SSB] else "LOWER"
            print(f"    => {md} = {data[md]} sideband\n")

        # ---------- CW modes ----------
        print("=== CW modes ===")
        if not set_verified(ser, "MD3", "MD", "MD3;"):
            print("  cannot enter CW"); return 1
        set_verified(ser, "BW0100", "BW", "BW0100;")
        is_s = ask(ser, "IS")
        pitch = float(is_s[3:7]) if is_s.startswith("IS ") else 600.0
        up, dn = pitch + OFF_CW, pitch - OFF_CW
        print(f"  pitch={pitch:.0f} Hz. Dial 200 Hz LOW: upper-sideband CW "
              f"beats at {up:.0f}, lower at {dn:.0f}\n")
        cw = {}
        for md in ("MD3", "MD7"):
            if not set_verified(ser, md, "MD", md + ";"):
                continue
            set_verified(ser, "BW0100", "BW", "BW0100;")
            lo = look(ser, best - OFF_CW, [up, dn], f"{md} dial LOW")
            hi = look(ser, best + OFF_CW, [up, dn], f"{md} dial HIGH")
            # dial LOW: carrier above dial. Upper-sideband CW -> pitch+200.
            # dial HIGH: carrier below dial. Upper-sideband CW -> pitch-200.
            votes = []
            votes.append("UPPER" if lo[up] > lo[dn] else "LOWER")
            votes.append("UPPER" if hi[dn] > hi[up] else "LOWER")
            cw[md] = votes[0] if votes[0] == votes[1] else "INCONSISTENT"
            print(f"    votes {votes}  => {md} = {cw[md]}\n")

        print("=" * 70)
        print("MAPPING VERDICT")
        print("=" * 70)
        for md, side in cw.items():
            print(f"  {md} -> TCI "
                  f"{'cwu' if side=='UPPER' else 'cwl' if side=='LOWER' else '??'}"
                  f"   ({side} sideband)")
        for md, side in data.items():
            print(f"  {md} -> TCI "
                  f"{'digu' if side=='UPPER' else 'digl'}   ({side} sideband)")
        return 0

    finally:
        print("\n=== restoring ===")
        for cmd in ("MD", "DT", "BW", "IS", "FA"):
            v = saved.get(cmd, "")
            if v and not v.startswith("?"):
                ask(ser, v.rstrip(";"), wait=0.5)
                time.sleep(0.3)
        time.sleep(0.8)
        now = {c: ask(ser, c) for c in ("FA", "MD", "DT", "BW", "IS")}
        print("  saved:", saved)
        print("  now:  ", now)
        print("  match:", "yes" if now == saved else "NO -- restore by hand")
        ser.close()


if __name__ == "__main__":
    sys.exit(main())
