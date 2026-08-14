#!/usr/bin/env python3
"""Why does the dummy load read 20.9:1?

Distinguishes three explanations:
  1. SW is stale (repeated reads during one carrier would not change)
  2. The KAT3A is inline with L/C stored for the real antenna, so a 50 ohm
     dummy load is transformed into a mismatch (SWR is read after the ATU)
  3. The load genuinely is not on this port

5 W TUNE carriers of ~1.5 s. Restores ATU mode and everything else.
"""
import time

import serial

PORT = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL047393-if00-port0"
QRG = 14_075_000


def ask(ser, cmd, wait=0.2):
    ser.reset_input_buffer()
    ser.write(cmd.encode() + b";")
    ser.flush()
    time.sleep(wait)
    return ser.read(ser.in_waiting or 256).decode("ascii", "replace").strip()


def ds_text(ser):
    ser.reset_input_buffer()
    ser.write(b"DS;")
    ser.flush()
    time.sleep(0.3)
    raw = ser.read(ser.in_waiting or 256)
    if not raw.startswith(b"DS") or len(raw) < 11:
        return repr(raw)
    return "".join(chr(b & 0x7F) for b in raw[2:10])


def tune_and_read(ser, label, seconds=1.6):
    """One TUNE carrier, sampling SW repeatedly to detect staleness."""
    swrs, pwrs = [], []
    try:
        ask(ser, "SWH16", wait=0.1)
        time.sleep(0.6)
        for _ in range(5):
            pwrs.append(ask(ser, "BG", wait=0.08))
            swrs.append(ask(ser, "SW", wait=0.08))
            time.sleep(0.15)
    finally:
        ask(ser, "RX", wait=0.1)
        time.sleep(0.6)
    print(f"  {label}")
    print(f"    BG: {pwrs}")
    print(f"    SW: {swrs}")
    return swrs


with serial.Serial(PORT, 38400, timeout=0.5) as ser:
    time.sleep(0.2)
    ask(ser, "K31", wait=0.3)
    saved = {c: ask(ser, c) for c in ("FA", "MD", "PC", "TM", "AN")}
    print("saved:", saved)

    ask(ser, "RX", wait=0.2)
    ask(ser, f"FA{QRG:011d}", wait=0.4); time.sleep(1.0)
    ask(ser, "PC005", wait=0.3)
    ask(ser, "TM0", wait=0.3)

    # what is the ATU doing?
    ask(ser, "MN023", wait=0.4)
    atu_mp = ask(ser, "MP", wait=0.3)
    atu_ds = ds_text(ser)
    print(f"\nKAT3 menu: MP={atu_mp!r}  display={atu_ds!r}")
    ask(ser, "MN255", wait=0.4)

    print("\n=== 1. ATU as-is ===")
    tune_and_read(ser, "ATU in its current mode")

    print("\n=== 2. ATU bypassed ===")
    ask(ser, "MN023", wait=0.4)
    ask(ser, "MP000", wait=0.4)      # 000 should be BYP
    byp_ds = ds_text(ser)
    ask(ser, "MN255", wait=0.4)
    print(f"  ATU now displays: {byp_ds!r}")
    tune_and_read(ser, "ATU bypassed")

    print("\n=== restoring ATU mode ===")
    ask(ser, "MN023", wait=0.4)
    if atu_mp.startswith("MP"):
        ask(ser, atu_mp.rstrip(";"), wait=0.4)
    back_ds = ds_text(ser)
    ask(ser, "MN255", wait=0.4)
    print(f"  ATU restored to: {back_ds!r}  (was {atu_ds!r})")

    for c in ("PC", "TM", "MD", "FA"):
        v = saved.get(c, "")
        if v and not v.startswith("?"):
            ask(ser, v.rstrip(";"), wait=0.45)
    time.sleep(0.6)
    now = {c: ask(ser, c) for c in ("FA", "MD", "PC", "TM", "AN")}
    print("\n  saved:", saved)
    print("  now:  ", now)
    print("  match:", "yes" if now == saved else "NO")
    print("  TQ:", ask(ser, "TQ"))
