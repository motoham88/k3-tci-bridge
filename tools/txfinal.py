#!/usr/bin/env python3
"""Decisive test of the USB TX audio path.

Earlier runs were confounded at both ends:
  * the codec's PCM playback sat at 82% (-23 dB), so a -20 dBFS tone reached
    the radio at about -43 dBFS;
  * above MG~034 the mic-path noise floor pins the ALC at 5-6 regardless of
    input, burying any tone.

So: max the codec output, and work at LOW mic gain where silence reads 0.
A tone that lifts the ALC above a silent baseline at the same MG is proof.

MIC+LIN must be ON for LINE IN to be summed into the TX path -- switching it
OFF (as the previous run did) removes line input entirely, which is why that
test could never have succeeded.

TX TEST asserted first: no RF. Restores the mixer and all radio state.
"""
import subprocess
import sys
import threading
import time

import numpy as np
import serial

PORT = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL047393-if00-port0"
ALSA, RATE, TONE_HZ = "hw:2,0", 48000, 1500.0
TEST_QRG, MAX_KEY = 14_075_000, 3.0

ser_lock = threading.Lock()
keyed_at = [None]


def ask(ser, cmd, wait=0.0):
    with ser_lock:
        ser.reset_input_buffer()
        ser.write(cmd.encode() + b";")
        ser.flush()
        if wait:
            time.sleep(wait)
        return ser.read_until(b";").decode("ascii", "replace").strip()


def raw(ser, cmd, wait=0.3):
    with ser_lock:
        ser.reset_input_buffer()
        ser.write(cmd.encode() + b";")
        ser.flush()
        time.sleep(wait)
        return ser.read(ser.in_waiting or 256)


def watchdog(ser, stop):
    while not stop.is_set():
        t = keyed_at[0]
        if t is not None and time.monotonic() - t > MAX_KEY:
            with ser_lock:
                ser.write(b"RX;"); ser.flush()
            keyed_at[0] = None
            print("      !! WATCHDOG forced RX")
        time.sleep(0.1)


def mixer(pct):
    subprocess.run(["amixer", "-c", "2", "sset", "PCM", f"{pct}%"],
                   capture_output=True)


def mixer_now():
    r = subprocess.run(["amixer", "-c", "2", "sget", "PCM"],
                       capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if "Front Left:" in line:
            return line.strip()
    return "?"


def make_tone(path, dbfs, seconds=4.0):
    n = int(RATE * seconds)
    t = np.arange(n) / RATE
    sig = ((10 ** (dbfs / 20.0)) * np.sin(2 * np.pi * TONE_HZ * t)
           * 32767).astype(np.int16)
    np.column_stack([sig, sig]).tofile(path)


def make_silence(path, seconds=4.0):
    np.zeros((int(RATE * seconds), 2), dtype=np.int16).tofile(path)


def parse_bg(rsp):
    if rsp.startswith("BG") and len(rsp) >= 5:
        try:
            return int(rsp[2:4]), rsp[4]
        except ValueError:
            pass
    return None, None


def transmit(ser, path, seconds=1.3, samples=6):
    player, bars = None, []
    try:
        if path:
            player = subprocess.Popen(
                ["aplay", "-D", ALSA, "-f", "S16_LE", "-r", str(RATE),
                 "-c", "2", "-t", "raw", path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.3)
        keyed_at[0] = time.monotonic()
        ask(ser, "TX")
        time.sleep(0.35)
        for _ in range(samples):
            n, _ = parse_bg(ask(ser, "BG"))
            if n is not None:
                bars.append(n)
            time.sleep((seconds - 0.35) / samples)
    finally:
        ask(ser, "RX")
        keyed_at[0] = None
        if player:
            player.terminate(); player.wait(timeout=3)
        time.sleep(0.35)
    return (max(bars) if bars else None), bars


def main():
    ser = serial.Serial(PORT, 38400, timeout=0.5)
    time.sleep(0.2)
    ask(ser, "K31", wait=0.3)
    stop = threading.Event()
    threading.Thread(target=watchdog, args=(ser, stop), daemon=True).start()
    saved = {c: ask(ser, c) for c in ("FA", "MD", "DT", "PC", "TM", "MG")}
    mix_before = mixer_now()
    print("mixer before:", mix_before)

    try:
        ic = raw(ser, "IC")
        a = ic[2] if ic.startswith(b"IC") and len(ic) >= 8 else 0
        if not (a & 0x20):
            print(f"ABORT: TX TEST off (IC a=0x{a:02X})."); return 1
        print(f"IC byte a = 0x{a:02X} -> TX TEST on, no RF.")

        mixer(100)
        print("mixer now:   ", mixer_now())

        ask(ser, "RX")
        ask(ser, f"FA{TEST_QRG:011d}", wait=0.4); time.sleep(1.0)
        ask(ser, "DT0", wait=0.4); ask(ser, "MD6", wait=0.5)
        for _ in range(3):
            if ask(ser, "MD") == "MD6;" and ask(ser, "DT") == "DT0;":
                break
            ask(ser, "DT0", wait=0.4); ask(ser, "MD6", wait=0.5)
        ask(ser, "PC005", wait=0.3); ask(ser, "TM1", wait=0.3)
        print("configured:", {c: ask(ser, c) for c in ("MD", "DT", "TM")})

        make_silence("/tmp/silence.raw")
        print("\n=== silence vs full-scale tone at LOW mic gain ===")
        print("   (working below the noise floor knee, where silence = 0)\n")
        print(f"   {'MG':<7} {'silence':<20} {'tone -3 dBFS':<20} verdict")
        make_tone("/tmp/tone3.raw", -3)
        good = []
        for mg in ("005", "010", "020", "026", "030", "034"):
            ask(ser, f"MG{mg}", wait=0.25)
            s, s_ser = transmit(ser, "/tmp/silence.raw")
            t, t_ser = transmit(ser, "/tmp/tone3.raw")
            if s is not None and t is not None and t > s + 1:
                verdict = "*** TONE DRIVES ALC ***"
                good.append(mg)
            elif (s or 0) >= 2:
                verdict = "noise floor -- MG too high"
            else:
                verdict = "both quiet"
            print(f"   MG{mg:<5} {str(s_ser):<20} {str(t_ser):<20} {verdict}")

        if good:
            mg = good[-1]
            print(f"\n=== tone-level curve at MG{mg} ===")
            ask(ser, f"MG{mg}", wait=0.25)
            for dbfs in (-40, -30, -20, -12, -6, -3):
                make_tone("/tmp/tone.raw", dbfs)
                m, sr = transmit(ser, "/tmp/tone.raw")
                print(f"    {dbfs:>4} dBFS -> ALC max {m}  {sr}")
            print(f"\nRESULT: USB TX audio path CONFIRMED at MG{mg} "
                  f"with PCM at 100%.")
        else:
            print("\nRESULT: still not demonstrated. USB audio is not "
                  "reaching the modulator.\nRemaining suspects: MIC SEL "
                  "(front/rear jack selection), or the codec\noutput is not "
                  "wired to the radio's LINE IN in this installation.")
        return 0

    finally:
        print("\n=== restoring ===")
        ask(ser, "RX", wait=0.3)
        if "%" in mix_before:
            pct = mix_before.split("[")[1].split("%")[0]
            mixer(int(pct))
        print("  mixer:", mixer_now())
        for c in ("MD", "DT", "PC", "TM", "MG", "FA"):
            v = saved.get(c, "")
            if v and not v.startswith("?"):
                ask(ser, v.rstrip(";"), wait=0.45)
        time.sleep(0.8)
        now = {c: ask(ser, c) for c in ("FA", "MD", "DT", "PC", "TM", "MG")}
        print("  radio match:", "yes" if now == saved else f"NO {now}")
        print("  TQ:", ask(ser, "TQ"))
        stop.set(); ser.close()


if __name__ == "__main__":
    sys.exit(main())
