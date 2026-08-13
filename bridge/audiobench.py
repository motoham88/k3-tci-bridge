#!/usr/bin/env python3
"""Does a Pi 3B carry the TCI audio path in Python?

Simulates the real RX pipeline end to end: ALSA capture -> int16->float32 ->
duplicate left into both channels (this radio has no sub RX) -> pack into
TCI-sized frames. Runs a concurrent CAT S-meter poll, because in the real
bridge the serial thread and the audio thread compete for the same cores.

Reports per-frame timing distribution, CPU cost, and any capture gaps.
Receive only -- nothing here keys the transmitter.
"""
import os
import subprocess
import threading
import time

import numpy as np
import psutil
import serial

ALSA = "hw:2,0"
RATE = 48000
FRAMES = 2048           # TCI audio_stream_samples
BYTES = FRAMES * 2 * 2  # stereo, int16
NOMINAL = FRAMES / RATE  # 42.67 ms
DURATION = 30.0
PORT = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL047393-if00-port0"

stop = threading.Event()
cat_lat = []


def cat_poller():
    """S-meter poll at 200 ms, exactly as the bridge will do it."""
    try:
        with serial.Serial(PORT, 38400, timeout=0.5) as ser:
            time.sleep(0.2)
            while not stop.is_set():
                t0 = time.perf_counter()
                ser.reset_input_buffer()
                ser.write(b"SMH;")
                ser.flush()
                # read_until stops at the terminator; a fixed-size read()
                # would block for the whole timeout on a short reply.
                buf = ser.read_until(b";")
                if buf.endswith(b";"):
                    cat_lat.append((time.perf_counter() - t0) * 1000)
                time.sleep(0.2)
    except Exception as exc:  # noqa: BLE001
        print(f"  (CAT poller stopped: {exc})")


def main():
    print(f"capturing {DURATION:.0f}s, {FRAMES}-sample frames "
          f"(nominal {NOMINAL*1000:.2f} ms/frame)\n")

    proc = subprocess.Popen(
        # period-size MUST match the TCI frame, or reads come in bursts:
        # a larger period makes one read block for N frames and the next
        # return instantly, and adds that much latency before the bridge
        # even sees the audio.
        ["arecord", "-D", ALSA, "-f", "S16_LE", "-r", str(RATE), "-c", "2",
         "-t", "raw", "--period-size", str(FRAMES),
         "--buffer-size", str(FRAMES * 4)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    me = psutil.Process(os.getpid())
    cat = threading.Thread(target=cat_poller, daemon=True)
    cat.start()

    gaps, deltas, work = [], [], []
    me.cpu_percent(None)
    cpu0 = time.process_time()
    t_start = time.perf_counter()
    prev = t_start
    n = 0

    while time.perf_counter() - t_start < DURATION:
        chunk = proc.stdout.read(BYTES)
        if len(chunk) < BYTES:
            gaps.append(n)
            break
        now = time.perf_counter()
        deltas.append((now - prev) * 1000)
        prev = now

        w0 = time.perf_counter()
        # --- the actual per-frame work the bridge does ---
        pcm = np.frombuffer(chunk, dtype=np.int16).reshape(-1, 2)
        left = pcm[:, 0].astype(np.float32) / 32768.0
        stereo = np.repeat(left, 2)          # duplicate L into both channels
        payload = stereo.tobytes()           # TCI float32 stereo interleaved
        # --------------------------------------------------
        work.append((time.perf_counter() - w0) * 1000)
        n += 1

    elapsed = time.perf_counter() - t_start
    cpu_used = time.process_time() - cpu0
    stop.set()
    proc.terminate()
    proc.wait(timeout=5)
    time.sleep(0.4)

    d = np.array(deltas[2:])   # drop startup transient
    w = np.array(work[2:])

    def pct(a, p):
        return float(np.percentile(a, p))

    print(f"frames: {n}  ({n/elapsed:.2f}/s, expected {RATE/FRAMES:.2f}/s)")
    print(f"payload: {len(payload)} bytes/frame -> "
          f"{len(payload)*RATE/FRAMES/1024:.0f} KiB/s "
          f"({len(payload)*8*RATE/FRAMES/1e6:.2f} Mbit/s per direction)")
    print("\ninter-frame interval (ms)   nominal 42.67")
    print(f"  p50 {pct(d,50):6.2f}   p95 {pct(d,95):6.2f}   "
          f"p99 {pct(d,99):6.2f}   max {d.max():6.2f}   min {d.min():6.2f}")
    print("\nper-frame processing (ms)   budget 42.67")
    print(f"  p50 {pct(w,50):6.3f}   p95 {pct(w,95):6.3f}   "
          f"p99 {pct(w,99):6.3f}   max {w.max():6.3f}")
    print(f"  -> {100*pct(w,99)/ (NOMINAL*1000):.2f}% of the frame budget at p99")
    print(f"\nCPU: {cpu_used:.2f}s of {elapsed:.1f}s wall "
          f"= {100*cpu_used/elapsed:.1f}% of one core "
          f"({100*cpu_used/elapsed/psutil.cpu_count():.1f}% of the box)")
    print(f"capture gaps/underruns: {len(gaps)}")

    if cat_lat:
        c = np.array(cat_lat)
        print(f"\nconcurrent CAT S-meter poll: n={len(c)}  "
              f"p50={np.percentile(c,50):.1f}ms  p95={np.percentile(c,95):.1f}ms  "
              f"max={c.max():.1f}ms")

    err = proc.stderr.read().decode(errors="replace").strip()
    if err:
        print(f"\narecord stderr: {err}")


if __name__ == "__main__":
    main()
