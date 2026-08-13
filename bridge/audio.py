"""TCI binary audio frames, and the ALSA plumbing behind them.

Frame layout (ExpertSDR3 TCI v2.0): a 64-byte header of 16 little-endian
uint32 followed by the sample payload. Cross-checked against the working
server in AetherSDR (src/core/TciServer.cpp).

Two things specific to this station:

  * The radio has no sub receiver, so capture is mono on the LEFT channel
    (the right sits at the ADC noise floor, measured -85 dBFS). We duplicate
    left into both TCI channels; sending the dead channel verbatim would put
    audio in one ear only.
  * The ALSA period MUST equal the TCI frame size. A larger period makes
    reads arrive in bursts and injects that much latency before the bridge
    sees a sample -- measured at 85 ms with a 4096-frame period against
    2048-frame reads, with CPU still near zero.
"""
from __future__ import annotations

import logging
import queue
import struct
import subprocess
import threading

import numpy as np

log = logging.getLogger("audio")

HEADER_BYTES = 64
RATE = 48000
FRAMES = 2048              # audio_stream_samples

# header.format
FMT_INT16, FMT_INT24, FMT_INT32, FMT_FLOAT32 = 0, 1, 2, 3
# header.type
TYPE_IQ, TYPE_RX_AUDIO, TYPE_TX_AUDIO, TYPE_TX_CHRONO = 0, 1, 2, 3

_HDR = struct.Struct("<16I")


def build_header(receiver: int, rate: int, fmt: int, length: int,
                 type_: int, channels: int) -> bytes:
    """length is the number of REAL SAMPLES, not frames: for stereo that is
    frames * 2. Getting this wrong makes clients play at half speed."""
    return _HDR.pack(receiver, rate, fmt, 0, 0, length, type_, channels,
                     0, 0, 0, 0, 0, 0, 0, 0)


def parse_header(data: bytes) -> dict | None:
    if len(data) < HEADER_BYTES:
        return None
    f = _HDR.unpack_from(data, 0)
    return {"receiver": f[0], "rate": f[1], "format": f[2], "codec": f[3],
            "crc": f[4], "length": f[5], "type": f[6], "channels": f[7]}


def rx_frame(left_int16: np.ndarray, receiver: int = 0,
             gain: float = 1.0) -> bytes:
    """One RX_AUDIO frame from mono int16 capture -> float32 stereo.

    `gain` is TCI master volume applied in software. It has to be done here:
    the K3's AF GAIN control does NOT affect the USB audio (measured -62.5
    dBFS at AG000 vs -61.9 at AG250, i.e. no effect), because LINE OUT is a
    fixed-level tap. The only hardware control is the LIN OUT menu entry,
    which is far too slow and too disruptive to drive from a volume slider.
    """
    mono = left_int16.astype(np.float32) / 32768.0
    if gain != 1.0:
        mono = mono * np.float32(gain)
    stereo = np.repeat(mono, 2)          # L,L  R,R  ... both channels equal
    return build_header(receiver, RATE, FMT_FLOAT32, stereo.size,
                        TYPE_RX_AUDIO, 2) + stereo.tobytes()


def tx_chrono_frame(receiver: int = 0) -> bytes:
    """Header-only frame asking the client for more TX audio (Thetis
    behaviour). Clients that stream continuously ignore it."""
    return build_header(receiver, RATE, FMT_FLOAT32, FRAMES,
                        TYPE_TX_CHRONO, 2)


def tx_payload_to_int16(hdr: dict, payload: bytes) -> np.ndarray | None:
    """Client TX audio -> int16 stereo, ready for ALSA.

    Do not trust hdr['channels'] -- WSJT-X reuses its FIFO and leaves
    garbage there. hdr['length'] is the count that matters.
    """
    n = int(hdr["length"])
    if n <= 0:
        return None
    if hdr["format"] == FMT_FLOAT32:
        avail = len(payload) // 4
        n = min(n, avail)
        if n <= 0:
            return None
        f = np.frombuffer(payload, dtype=np.float32, count=n)
        pcm = np.clip(f * 32768.0, -32768, 32767).astype(np.int16)
    elif hdr["format"] == FMT_INT16:
        avail = len(payload) // 2
        n = min(n, avail)
        if n <= 0:
            return None
        pcm = np.frombuffer(payload, dtype=np.int16, count=n)
    else:
        log.warning("unsupported TX audio format %s", hdr["format"])
        return None
    if pcm.size % 2:                     # odd sample count: pad to a frame
        pcm = np.append(pcm, np.int16(0))
    return pcm


class AudioCapture:
    """arecord -> callback, one TCI frame at a time."""

    def __init__(self, device: str, on_frame, rate: int = RATE,
                 frames: int = FRAMES):
        self.device, self.rate, self.frames = device, rate, frames
        self.on_frame = on_frame
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.frames_sent = 0
        self.underruns = 0

    def start(self) -> None:
        self._proc = subprocess.Popen(
            ["arecord", "-D", self.device, "-f", "S16_LE",
             "-r", str(self.rate), "-c", "2", "-t", "raw",
             # period == TCI frame; buffer a few periods for slack
             "--period-size", str(self.frames),
             "--buffer-size", str(self.frames * 4)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("capture started on %s (%d Hz, %d-frame periods)",
                 self.device, self.rate, self.frames)

    def _loop(self) -> None:
        want = self.frames * 2 * 2       # stereo int16
        while not self._stop.is_set():
            chunk = self._proc.stdout.read(want)
            if not chunk or len(chunk) < want:
                # Terminating arecord always produces one final short read.
                # Counting that as an underrun makes a clean shutdown look
                # like a glitch in the logs, so check for stop FIRST.
                if self._stop.is_set():
                    break
                self.underruns += 1
                log.warning("short read from arecord (%d bytes)", len(chunk))
                continue
            pcm = np.frombuffer(chunk, dtype=np.int16).reshape(-1, 2)
            try:
                self.on_frame(pcm[:, 0])          # LEFT only: see module doc
                self.frames_sent += 1
            except Exception:
                log.exception("capture callback failed")

    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._thread:
            self._thread.join(timeout=3)
        log.info("capture stopped (%d frames, %d underruns)",
                 self.frames_sent, self.underruns)


class AudioPlayback:
    """Continuous aplay stream fed from a queue.

    The stream runs even with no TX audio queued, writing silence. That
    keeps ALSA primed, so keying does not have to wait for the device to
    start -- which is what would clip the opening of a transmission.
    """

    def __init__(self, device: str, rate: int = RATE, frames: int = FRAMES,
                 depth: int = 8):
        self.device, self.rate, self.frames = device, rate, frames
        self._q: queue.Queue = queue.Queue(maxsize=depth)
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._silence = np.zeros(frames * 2, dtype=np.int16).tobytes()
        self.blocks_written = 0
        self.dropped = 0

    def start(self) -> None:
        # The codec's PCM playback control ships at 82% (-23 dB), which is
        # enough attenuation to bury TX audio before it reaches the radio's
        # LINE IN. Set it explicitly rather than depending on whatever the
        # card was last left at -- gain is calibrated with MG on the radio,
        # not here. Card index is parsed out of the ALSA device string.
        card = self.device.split(":")[-1].split(",")[0]
        r = subprocess.run(["amixer", "-c", card, "sset", "PCM", "100%"],
                           capture_output=True)
        if r.returncode:
            log.warning("could not set PCM playback to 100%% on card %s: %s",
                        card, r.stderr.decode(errors="replace").strip()[:120])
        else:
            log.info("codec PCM playback set to 100%%")
        self._proc = subprocess.Popen(
            ["aplay", "-D", self.device, "-f", "S16_LE",
             "-r", str(self.rate), "-c", "2", "-t", "raw",
             "--period-size", str(self.frames),
             "--buffer-size", str(self.frames * 4)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("playback started on %s", self.device)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                block = self._q.get(timeout=0.05)
            except queue.Empty:
                block = self._silence
            try:
                self._proc.stdin.write(block)
                self._proc.stdin.flush()
                self.blocks_written += 1
            except (BrokenPipeError, ValueError):
                if not self._stop.is_set():
                    log.error("playback pipe closed")
                return

    def write(self, pcm: np.ndarray) -> None:
        """Queue TX audio. Drops the oldest block when full: late audio is
        worse than missing audio, and an unbounded queue would grow latency
        without limit under a slow link."""
        try:
            self._q.put_nowait(pcm.tobytes())
        except queue.Full:
            try:
                self._q.get_nowait()
                self._q.put_nowait(pcm.tobytes())
            except (queue.Empty, queue.Full):
                pass
            self.dropped += 1
            if self.dropped % 50 == 1:
                log.warning("TX audio queue full, dropping (%d total)",
                            self.dropped)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._proc:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        log.info("playback stopped (%d blocks, %d dropped)",
                 self.blocks_written, self.dropped)
