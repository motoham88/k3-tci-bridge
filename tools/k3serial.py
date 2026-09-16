"""Open the K3's CAT port without keying the radio.

A port comes up with DTR and RTS asserted unless it is told otherwise, and
the K3 can be told to read DTR as KEY and RTS as PTT (its RS232 menu). So
opening this port the default way is a key-down at a radio configured for
it -- which happened at this station, and took unplugging the USB lead to
stop. Every tool here goes through this function for that reason.

The twin of bridge/k3cat.open_serial, duplicated rather than imported
because these scripts run standalone from this directory.
"""
import serial

PORT = "/dev/k3cat"


def open_k3(port: str = PORT, baud: int = 38400,
            timeout: float = 0.5) -> serial.Serial:
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = timeout
    ser.rtscts = False
    ser.dsrdtr = False
    ser.dtr = False                 # applied by open(), not after it
    ser.rts = False
    ser.open()
    return ser
