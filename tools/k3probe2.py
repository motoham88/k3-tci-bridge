#!/usr/bin/env python3
"""Second-stage CAT diagnostic.

Rules out modem-control-line state and listens passively for any unsolicited
traffic (which would appear if CONFIG:AUTOINF is set, or if the operator
touches a front-panel control while this runs).

NOT read-only any more, and it never was on a radio configured for line
keying. The sweep below deliberately asserts DTR and RTS, and the K3 can be
told to read DTR as KEY and RTS as PTT (its RS232 menu) -- so on a radio set
that way, this transmits. That is the one thing this tool is for now, and
the reason the rest of tools/ goes through k3serial.open_k3 instead.

TURN ON TX TEST, or set the radio's RS232 menu to OFF/OFF, before running
the sweep. The passive listen below is safe either way -- it opens with both
lines down.
"""
import time

import serial

import k3serial

PORT = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL047393-if00-port0"


def probe(baud, dtr, rts):
    try:
        ser = serial.Serial()
        ser.port = PORT
        ser.baudrate = baud
        ser.timeout = 0.5
        ser.rtscts = False
        ser.dsrdtr = False
        ser.dtr = dtr
        ser.rts = rts
        ser.open()
    except Exception as exc:  # noqa: BLE001
        return f"ERROR {exc}"
    with ser:
        time.sleep(0.25)
        ser.reset_input_buffer()
        ser.write(b"ID;")
        ser.flush()
        time.sleep(0.4)
        got = ser.read(ser.in_waiting or 256)
        cts = f"cts={ser.cts} dsr={ser.dsr} cd={ser.cd} ri={ser.ri}"
        return f"{got!r:<20} {cts}"


print("=== modem control line sweep (all at 38400) ===")
print("    THIS KEYS A RADIO SET TO DTR=KEY OR RTS=PTT. TX TEST first.")
for dtr in (True, False):
    for rts in (True, False):
        print(f"  dtr={int(dtr)} rts={int(rts)}: {probe(38400, dtr, rts)}")

print("\n=== passive listen, 6 s at 38400 (turn the K3 VFO now) ===")
with k3serial.open_k3(PORT) as ser:
    ser.reset_input_buffer()
    seen = bytearray()
    end = time.monotonic() + 6.0
    while time.monotonic() < end:
        chunk = ser.read(256)
        if chunk:
            seen += chunk
    print(f"  received {len(seen)} bytes: {bytes(seen)!r}")
