#!/usr/bin/env python3
"""Full initial state sweep against the K3S.

Sends K31; (extended mode, per the command map's global rule 1) and then
GETs every parameter the v1 TCI bridge needs. Everything else is read-only.
"""
import time

import serial

PORT = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL047393-if00-port0"

GETS = [
    ("ID", "radio id (expect ID017)"),
    ("K3", "extended mode state"),
    ("OM", "option modules -- R=K3S, S=KRX3A sub RX"),
    ("RVM", "MCU firmware rev"),
    ("IF", "general info (38 chars)"),
    ("FA", "VFO A"),
    ("FB", "VFO B"),
    ("MD", "mode"),
    ("DT", "data sub-mode"),
    ("BW", "filter bandwidth (10 Hz units)"),
    ("IS", "IF shift / AF center"),
    ("XF", "crystal filter number"),
    ("BN", "band number"),
    ("RT", "RIT on/off"),
    ("XT", "XIT on/off"),
    ("RO", "RIT/XIT offset"),
    ("PC", "power setpoint"),
    ("AG", "AF gain"),
    ("RG", "RF gain"),
    ("SQ", "squelch"),
    ("GT", "AGC time constant"),
    ("NB", "noise blanker on/off"),
    ("NL", "noise blanker levels"),
    ("PA", "preamp"),
    ("RA", "attenuator"),
    ("AN", "antenna"),
    ("MG", "mic gain"),
    ("KS", "keyer speed"),
    ("SM", "S-meter (K31: 0000-0021)"),
    ("SMH", "S-meter high-res (0-140)"),
    ("TQ", "transmit query"),
    ("SB", "sub RX / dual watch"),
]


def ask(ser, cmd, wait=0.25):
    ser.reset_input_buffer()
    ser.write(cmd.encode() + b";")
    ser.flush()
    time.sleep(wait)
    return ser.read(ser.in_waiting or 256)


with serial.Serial(PORT, 38400, timeout=0.5) as ser:
    time.sleep(0.2)

    # Global rule 1: K3 extended mode on, K2 mode left at default K20.
    ser.write(b"K31;")
    ser.flush()
    time.sleep(0.3)
    ser.reset_input_buffer()

    print(f"{'CMD':<5} {'RESPONSE':<24} MEANING")
    print("-" * 76)
    for cmd, meaning in GETS:
        reply = ask(ser, cmd)
        text = reply.decode("ascii", "replace").strip()
        flag = "  <-- BUSY/UNSUPPORTED" if text == "?;" else ""
        if not text:
            flag = "  <-- NO RESPONSE"
        print(f"{cmd:<5} {text!r:<24} {meaning}{flag}")

    # Decode the IF response field-by-field per the map's layout table.
    raw = ask(ser, "IF")
    s = raw.decode("ascii", "replace")
    print("\n=== IF decode ===")
    print(f"raw: {s!r}  (len={len(s)}, expect 38)")
    if len(s) >= 38 and s.startswith("IF"):
        print(f"  freq        {s[2:13]}  ({int(s[2:13]):,} Hz)")
        print(f"  rit/xit ofs {s[18]}{s[19:23]}")
        print(f"  RIT on      {s[23]}")
        print(f"  XIT on      {s[24]}")
        print(f"  TX          {s[28]}")
        print(f"  mode        {s[29]}")
        print(f"  rx vfo      {s[30]}  (always 0 on K3)")
        print(f"  scan        {s[31]}")
        print(f"  split       {s[32]}")
        print(f"  band chg    {s[33]}  (K22 only)")
        print(f"  data submod {s[34]}  (K31 only)")
