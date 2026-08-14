#!/usr/bin/env python3
"""Does LINE IN still reach the transmitter with MIC+LIN switched OFF?

This matters for FT8: with MIC+LIN ON the microphone is summed into the TX
path, so the radio picks up shack noise and -- with the monitor up -- can
howl. If LINE IN works with MIC+LIN OFF, digital modes should switch it off
and the mic is out of the path entirely.

An earlier run of this test concluded "OFF removes LINE IN". That run was
confounded twice over: the codec mixer sat at 82% (-23 dB), and the mic gain
was above the point where the mic noise floor alone opens the ALC. Both are
corrected here -- mixer at 100%, and gains kept in the range where silence
reads zero.

Runs in TX TEST: no RF. Restores MIC+LIN and mic gain on exit.
"""
import subprocess
import sys
import time

import numpy as np
import serial

PORT = "/dev/k3cat"
ALSA, RATE, FRAMES = "hw:2,0", 48000, 2048


def ask(ser, cmd, wait=0.0):
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


def ds_text(ser):
    r = raw(ser, "DS")
    if not r.startswith(b"DS") or len(r) < 11:
        return repr(r)
    tbl = {"@": " ", "Q": "O", "V": "U", "M": "N", "K": "H", "W": "I"}
    return "".join(tbl.get(chr(b & 0x7F), chr(b & 0x7F)) for b in r[2:10])


def parse_bg(rsp):
    if rsp.startswith("BG") and len(rsp) >= 5:
        try:
            return int(rsp[2:4]), rsp[4]
        except ValueError:
            pass
    return None, None


def make(path, dbfs=None):
    n = FRAMES * 40
    if dbfs is None:
        np.zeros((n, 2), dtype=np.int16).tofile(path)
        return
    t = np.arange(n) / RATE
    sig = ((10 ** (dbfs / 20)) * np.sin(2 * np.pi * 1500 * t)
           * 32767).astype(np.int16)
    np.column_stack([sig, sig]).tofile(path)


def transmit(ser, path, seconds=1.4, samples=6):
    player, bars = None, []
    try:
        player = subprocess.Popen(
            ["aplay", "-D", ALSA, "-f", "S16_LE", "-r", str(RATE), "-c", "2",
             "-t", "raw", path], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        time.sleep(0.3)
        ask(ser, "TX")
        time.sleep(0.35)
        for _ in range(samples):
            n, _f = parse_bg(ask(ser, "BG"))
            if n is not None:
                bars.append(n)
            time.sleep((seconds - 0.35) / samples)
    finally:
        ask(ser, "RX")
        if player:
            player.terminate()
            player.wait(timeout=3)
        time.sleep(0.35)
    return (max(bars) if bars else None), bars


def miclin(ser, want_on):
    """Toggle MIC+LIN (menu 015) with UP/DN -- MP returns '?;' for it."""
    ask(ser, "MN015", wait=0.4)
    cur = ds_text(ser).strip()
    for _ in range(3):
        if (cur == "ON") == want_on:
            break
        ask(ser, "DN", wait=0.4)
        cur = ds_text(ser).strip()
    ask(ser, "MN255", wait=0.4)
    return cur


def main():
    subprocess.run(["amixer", "-c", "2", "sset", "PCM", "100%"],
                   capture_output=True)
    with serial.Serial(PORT, 38400, timeout=0.5) as ser:
        time.sleep(0.2)
        ask(ser, "K31", wait=0.3)
        ic = raw(ser, "IC")
        if not (ic.startswith(b"IC") and len(ic) >= 8 and ic[2] & 0x20):
            print("ABORT: radio is not in TX TEST — this would radiate.")
            return 1
        print("TX TEST on: no RF.\n")

        saved_mg = ask(ser, "MG")
        ask(ser, "MN015", wait=0.4)
        saved_ml = ds_text(ser).strip()
        ask(ser, "MN255", wait=0.4)
        print(f"saved: MG={saved_mg} MIC+LIN={saved_ml!r}")

        ask(ser, "RX")
        ask(ser, "DT0", wait=0.4)
        ask(ser, "MD6", wait=0.5)
        ask(ser, "PC005", wait=0.3)
        ask(ser, "TM1", wait=0.3)
        make("/tmp/sil.raw")
        make("/tmp/tone.raw", -6)

        try:
            for want in (True, False):
                state = miclin(ser, want)
                print(f"\n=== MIC+LIN = {state!r} ===")
                print(f"   {'MG':<7} {'silence':<20} {'tone -6 dBFS':<20} verdict")
                for mg in ("005", "015", "025"):
                    ask(ser, f"MG{mg}", wait=0.3)
                    s, s_ser = transmit(ser, "/tmp/sil.raw")
                    t, t_ser = transmit(ser, "/tmp/tone.raw")
                    if s is not None and t is not None and t > s + 1:
                        v = "*** LINE IN REACHES TX ***"
                    elif (s or 0) >= 2:
                        v = "mic noise floor"
                    else:
                        v = "nothing"
                    print(f"   MG{mg:<5} {str(s_ser):<20} {str(t_ser):<20} {v}")
        finally:
            print("\n=== restoring ===")
            miclin(ser, saved_ml == "ON")
            ask(ser, saved_mg.rstrip(";"), wait=0.4)
            ask(ser, "TM0", wait=0.3)
            ask(ser, "MD3", wait=0.5)
            ask(ser, "MN015", wait=0.4)
            print(f"  MIC+LIN back to {ds_text(ser).strip()!r} "
                  f"(was {saved_ml!r}), MG {ask(ser, 'MG')}")
            ask(ser, "MN255", wait=0.4)
    return 0


if __name__ == "__main__":
    sys.exit(main())
