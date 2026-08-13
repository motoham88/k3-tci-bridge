#!/usr/bin/env python3
"""Which control actually sets the USB RX audio level: AF GAIN or LIN OUT?

The command map maps TCI `volume` to AG. If AG does not move the USB audio,
that mapping is wrong and volume control has to go somewhere else -- either
LIN OUT (menu 032, MP-readable) or software gain in the bridge.

Measures capture RMS while sweeping each control. Receive only.
"""
import subprocess
import time

import numpy as np
import serial

PORT = "/dev/k3cat"
ALSA = "hw:2,0"
RATE = 48000


def ask(ser, cmd, wait=0.25):
    ser.reset_input_buffer()
    ser.write(cmd.encode() + b";")
    ser.flush()
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


def level(seconds=1.5):
    subprocess.run(
        ["arecord", "-D", ALSA, "-f", "S16_LE", "-r", str(RATE), "-c", "2",
         "--samples", str(int(RATE * seconds)), "-t", "raw", "/tmp/lv.raw"],
        check=True, capture_output=True)
    pcm = np.fromfile("/tmp/lv.raw", dtype=np.int16).reshape(-1, 2)
    left = pcm[:, 0].astype(np.float64) / 32768.0
    rms = float(np.sqrt((left ** 2).mean()))
    peak = float(np.abs(left).max())
    return rms, peak


with serial.Serial(PORT, 38400, timeout=0.5) as ser:
    time.sleep(0.2)
    ask(ser, "K31", wait=0.3)
    ag0 = ask(ser, "AG")
    ask(ser, "MN032", wait=0.4)
    lin0 = ask(ser, "MP", wait=0.3)
    lin0_txt = ds_text(ser)
    ask(ser, "MN255", wait=0.4)
    print(f"saved: AG={ag0!r}  LIN OUT MP={lin0!r} ({lin0_txt!r})\n")

    try:
        print("=== sweeping AF GAIN (AG) ===")
        for ag in ("000", "050", "120", "200", "250"):
            ask(ser, f"AG{ag}", wait=0.35)
            time.sleep(0.4)
            rms, peak = level()
            print(f"    AG{ag} -> rms {rms:.6f} "
                  f"({20*np.log10(max(rms,1e-9)):6.1f} dBFS)  peak {peak:.4f}")
        ask(ser, ag0.rstrip(";"), wait=0.35)

        print("\n=== sweeping LIN OUT (menu 032) ===")
        for lin in ("000", "005", "010", "020", "030", "040"):
            ask(ser, "MN032", wait=0.4)
            ask(ser, f"MP{lin}", wait=0.4)
            txt = ds_text(ser)
            ask(ser, "MN255", wait=0.4)
            time.sleep(0.4)
            rms, peak = level()
            print(f"    LIN OUT {lin} ({txt.strip()!r}) -> rms {rms:.6f} "
                  f"({20*np.log10(max(rms,1e-9)):6.1f} dBFS)  peak {peak:.4f}")
    finally:
        print("\n=== restoring ===")
        ask(ser, ag0.rstrip(";"), wait=0.4)
        ask(ser, "MN032", wait=0.4)
        if lin0.startswith("MP"):
            ask(ser, lin0.rstrip(";"), wait=0.4)
        back = ds_text(ser)
        ask(ser, "MN255", wait=0.4)
        print(f"  AG -> {ask(ser,'AG')}   LIN OUT -> {back!r}")
        print(f"  menu: {ask(ser,'MN')}")
