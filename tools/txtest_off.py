#!/usr/bin/env python3
"""Take the K3 out of TX TEST, and report the state that matters before any
RF is generated.

SWH18 toggles TX TEST; IC byte a bit 5 reports it. Nothing here transmits --
that is a separate, deliberate step once the band and load are confirmed.
"""
import time

import serial

PORT = "/dev/k3cat"


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


def tx_test(ser):
    ic = raw(ser, "IC")
    if not ic.startswith(b"IC") or len(ic) < 8:
        return None, None
    a = ic[2]
    return bool(a & 0x20), a


with serial.Serial(PORT, 38400, timeout=0.5) as ser:
    time.sleep(0.2)
    ask(ser, "K31", wait=0.3)
    ask(ser, "RX", wait=0.2)          # make sure we are not keyed

    on, a = tx_test(ser)
    print(f"TX TEST currently: {on}   (IC byte a = 0x{a:02X})")

    if on:
        print("toggling with SWH18 ...")
        ask(ser, "SWH18", wait=0.6)
        time.sleep(0.6)
        on, a = tx_test(ser)
        print(f"TX TEST now:       {on}   (IC byte a = 0x{a:02X})")

    print("\n=== state before any transmit ===")
    for c, label in (("FA", "VFO A"), ("MD", "mode"), ("PC", "power set"),
                     ("AN", "antenna"), ("TQ", "TX state"), ("BN", "band"),
                     ("SW", "last SWR reading")):
        print(f"  {label:<16} {ask(ser, c)}")
    ic = raw(ser, "IC")
    if ic.startswith(b"IC") and len(ic) >= 8:
        a = ic[2]
        print(f"  TX TEST          {bool(a & 0x20)}")
        print(f"  mW / KXV3 TEST   {bool(a & 0x10)}   "
              f"(if true, power is in hundredths of a mW)")
    print("\nNothing was transmitted.")
