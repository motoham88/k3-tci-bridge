#!/usr/bin/env python3
"""Resolve the RA (attenuator) command format empirically.

The reference selects between two RA formats based on whether OM reports
'R' (K3S RF board). This radio's OM does NOT report R, which contradicts
the assumption in the command map. Rather than guess, ask the hardware:
K3S format accepts RA05/RA10/RA15; legacy K3 format only knows RA00/RA01.

Restores the original attenuator setting on exit.
"""
import time

import serial

PORT = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL047393-if00-port0"


def ask(ser, cmd, wait=0.25):
    ser.reset_input_buffer()
    ser.write(cmd.encode() + b";")
    ser.flush()
    time.sleep(wait)
    return ser.read(ser.in_waiting or 256).decode("ascii", "replace").strip()


with serial.Serial(PORT, 38400, timeout=0.5) as ser:
    time.sleep(0.2)
    ser.write(b"K31;")
    ser.flush()
    time.sleep(0.3)

    original = ask(ser, "RA")
    print(f"original attenuator state: {original!r}")

    print("\n=== RA format probe ===")
    for attempt in ("RA05", "RA10", "RA15", "RA01"):
        ask(ser, attempt, wait=0.3)
        back = ask(ser, "RA")
        verdict = "accepted" if back == attempt + ";" else f"-> {back}"
        print(f"  sent {attempt:<6} read back {back!r:<10} {verdict}")

    restore = original[2:-1] if original.startswith("RA") else "00"
    ask(ser, f"RA{restore}", wait=0.3)
    print(f"\nrestored: {ask(ser, 'RA')!r}")

    print("\n=== sub-receiver command probe (OM shows no 'S') ===")
    for cmd in ("MD$", "SM$", "AG$", "SB"):
        print(f"  {cmd:<5} -> {ask(ser, cmd)!r}")
