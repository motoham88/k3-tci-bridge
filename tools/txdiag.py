#!/usr/bin/env python3
"""Why won't the K3 enter transmit?

BG returned 'BG03R;' (R = receive) during both TX; and SWH16/TUNE, so the
radio never keyed. This checks the documented inhibit paths.

Every key attempt is followed immediately by RX;.
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
    return "".join(chr(b & 0x7F) for b in r[2:10])


def try_key(ser, label, cmd, hold=1.0):
    tq_before = ask(ser, "TQ")
    try:
        ask(ser, cmd, wait=0.15)
        time.sleep(hold)
        tq = ask(ser, "TQ", wait=0.1)
        bg = ask(ser, "BG", wait=0.1)
    finally:
        ask(ser, "RX", wait=0.15)
        time.sleep(0.5)
    tq_after = ask(ser, "TQ")
    print(f"  {label:<26} before={tq_before} during={tq} BG={bg} after={tq_after}")
    return tq == "TQ1;"


with serial.Serial(PORT, 38400, timeout=0.5) as ser:
    time.sleep(0.2)
    ask(ser, "K31", wait=0.3)
    saved = {c: ask(ser, c) for c in ("FA", "MD", "PC", "AN")}
    print("saved:", saved)

    # --- IC status bits (read as binary; bit 7 of each byte is always 1) ---
    ic = raw(ser, "IC")
    print(f"\nIC raw: {ic!r}")
    if ic.startswith(b"IC") and len(ic) >= 8:
        a, b, c, d, e = ic[2:7]
        print(f"  byte a = 0x{a:02X}  "
              f"BSET={bool(a&0x40)}  TXTEST={bool(a&0x20)}  "
              f"mW/KXV3-TEST={bool(a&0x10)}")
        print(f"  byte b = 0x{b:02X}  subRX_on={bool(b&0x01)}  "
              f"diversity={bool(b&0x10)}")
        print(f"  byte e = 0x{e:02X}  main_squelched={bool(e&0x10)}")

    # --- inhibit-related menu entries ---
    print()
    for num, label in (("025", "TX INH"), ("055", "KPA3 mode"),
                       ("089", "XVx ON"), ("101", "TX GATE")):
        ask(ser, f"MN{num}", wait=0.35)
        print(f"  MN{num} {label:<12} MP={ask(ser,'MP', 0.3)!r:<10} "
              f"DS={ds_text(ser)!r}")
    ask(ser, "MN255", wait=0.4)

    # --- can we key at all, in several modes? ---
    print("\n=== key attempts (each followed immediately by RX;) ===")
    ask(ser, "PC005", wait=0.3)
    print("  PC readback:", ask(ser, "PC"))

    for md, name in (("MD3", "CW"), ("MD2", "USB"), ("MD6", "DATA")):
        ask(ser, md, wait=0.5)
        got = ask(ser, "MD")
        try_key(ser, f"{name} ({got.rstrip(';')}) TX;", "TX")
    ask(ser, "MD3", wait=0.5)
    try_key(ser, "CW TUNE (SWH16)", "SWH16", hold=1.2)
    try_key(ser, "CW XMIT tap (SWT16)", "SWT16", hold=1.2)

    print("\n=== restoring ===")
    ask(ser, "RX", wait=0.3)
    for c in ("MD", "PC", "FA"):
        v = saved.get(c, "")
        if v and not v.startswith("?"):
            ask(ser, v.rstrip(";"), wait=0.45)
    time.sleep(0.6)
    now = {c: ask(ser, c) for c in ("FA", "MD", "PC", "AN")}
    print("  saved:", saved)
    print("  now:  ", now)
    print("  match:", "yes" if now == saved else "NO")
    print("  TQ:", ask(ser, "TQ"))
