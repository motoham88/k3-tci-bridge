#!/usr/bin/env python3
"""TCI WebSocket server for the K3.

The serial side is blocking and lives in its own thread; the WebSocket side
is asyncio. CAT work is pushed to a worker thread so the event loop never
blocks on the port, and unsolicited AI2 messages are marshalled back into
the loop from the reader thread.

TCI is a stateful broker: any state change, from any source, is echoed to
EVERY connected client -- not just the one that asked.

Audio is opened lazily on the first `audio_start` and closed when the last
subscriber goes away, so the ALSA devices are free while nothing is
listening.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time
from pathlib import Path

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

import audio
import k3cat
import tci

log = logging.getLogger("server")

DEFAULT_PORT = 50001
DEFAULT_DEV = "/dev/k3cat"
DEFAULT_ALSA = "hw:2,0"

UI_FILE = Path(__file__).with_name("ui.html")


def http_handler(connection, request):
    """Serve the web UI over the SAME port as the WebSocket.

    Same origin for page and socket, which removes the whole class of
    mixed-content problems: a browser will not open a ws:// socket from an
    https:// page, so the UI has to be plain http -- and serving it here
    means there is no second server to run or keep in sync.

    Returning None lets the request continue to the WebSocket handshake.
    """
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None                      # a real client: hand off to WS
    if request.path.split("?")[0] not in ("/", "/index.html", "/ui.html"):
        return connection.respond(404, "not found\n")
    try:
        body = UI_FILE.read_bytes()
    except OSError as exc:
        log.error("cannot read %s: %s", UI_FILE, exc)
        return connection.respond(500, "ui.html missing\n")
    return Response(200, "OK", Headers([
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
    ]), body)


# Server-level commands: these never reach the protocol layer.
AUDIO_ECHO = ("audio_stream_samples", "tx_stream_audio_buffering",
              "line_out_start", "line_out_stop", "line_out_recorder")


class Server:
    def __init__(self, bridge: tci.Bridge, host: str, port: int,
                 alsa: str, no_audio: bool = False):
        self.bridge = bridge
        self.host, self.port = host, port
        self.alsa = alsa
        self.no_audio = no_audio
        self.clients: set = set()
        self.audio_clients: set = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.capture: audio.AudioCapture | None = None
        self.playback: audio.AudioPlayback | None = None
        self.chrono_task: asyncio.Task | None = None

    # ---------- sending ----------

    async def _send(self, ws, messages) -> None:
        for m in messages:
            try:
                await ws.send(m.rstrip(";") + ";")
            except ConnectionClosed:
                return

    async def broadcast(self, messages, exclude=None) -> None:
        if not messages:
            return
        for ws in list(self.clients):
            if ws is exclude:
                continue
            await self._send(ws, messages)

    def broadcast_threadsafe(self, messages) -> None:
        """Called from the CAT reader thread."""
        if messages and self.loop:
            asyncio.run_coroutine_threadsafe(
                self.broadcast(messages), self.loop)

    # ---------- audio ----------

    def _on_capture_frame(self, left_int16) -> None:
        """Capture thread. Build the frame here (cheap, ~0.4 ms measured)
        and hand the finished bytes to the loop, so no numpy work happens
        on the event loop."""
        if not self.audio_clients or not self.loop:
            return
        frame = audio.rx_frame(left_int16,
                               gain=self.bridge.audio_gain())
        asyncio.run_coroutine_threadsafe(self._send_audio(frame), self.loop)

    async def _send_audio(self, frame: bytes) -> None:
        for ws in list(self.audio_clients):
            try:
                await ws.send(frame)
            except ConnectionClosed:
                self.audio_clients.discard(ws)

    def _ensure_audio(self) -> None:
        if self.no_audio or self.capture:
            return
        self.capture = audio.AudioCapture(self.alsa, self._on_capture_frame)
        self.playback = audio.AudioPlayback(self.alsa)
        self.capture.start()
        # Playback runs continuously (writing silence when idle) so ALSA is
        # primed before PTT -- otherwise the device start-up would eat the
        # beginning of the first transmission.
        self.playback.start()

    def _release_audio(self) -> None:
        if self.audio_clients:
            return
        if self.capture:
            self.capture.stop()
            self.capture = None
        if self.playback:
            self.playback.stop()
            self.playback = None

    # ---------- TX_CHRONO ----------

    async def _chrono_loop(self, ws) -> None:
        """Pace the client's TX audio.

        TX_CHRONO is a clock, not data: a header-only frame that says "send
        me the next block". Clients like WSJT-X wait to be asked rather than
        streaming freely, so without this they key up and transmit silence.

        One frame per 1024 stereo frames of audio = 21.33 ms at 48 kHz.
        Timing accumulates against a fixed schedule rather than sleeping a
        fixed interval, so scheduler jitter does not drift the clock.
        """
        period = (audio.FRAMES / 2) / audio.RATE
        frame = audio.tx_chrono_frame()
        next_t = t0 = time.perf_counter()
        sent = 0
        log.info("TX_CHRONO started (%d-sample frames every %.2f ms)",
                 audio.FRAMES, period * 1000)
        try:
            while True:
                await ws.send(frame)
                sent += 1
                next_t += period
                delay = next_t - time.perf_counter()
                if delay < -0.25:
                    next_t = time.perf_counter()   # fell far behind; resync
                    delay = 0.0
                await asyncio.sleep(max(0.0, delay))
        except (ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("TX_CHRONO loop failed")
        finally:
            el = time.perf_counter() - t0
            log.info("TX_CHRONO stopped: %d frames in %.2f s = %.2f/s "
                     "(target %.2f/s)", sent, el, sent / el if el else 0,
                     1 / period)

    def sync_chrono(self) -> None:
        """Start or stop the chrono clock to match PTT state."""
        b = self.bridge
        want = bool(b.state.transmitting and b.ptt_wants_audio and b.ptt_owner)
        if want and self.chrono_task is None:
            self.chrono_task = asyncio.create_task(
                self._chrono_loop(b.ptt_owner))
        elif not want and self.chrono_task is not None:
            self.chrono_task.cancel()
            self.chrono_task = None

    def _handle_server_cmd(self, name: str, args: list[str],
                           raw: str, ws) -> list[str] | None:
        """Server-level commands. Returns replies, or None if not ours."""
        if name == "audio_start":
            self.audio_clients.add(ws)
            self._ensure_audio()
            log.info("audio_start from %s (%d subscriber(s))",
                     getattr(ws, "remote_address", None),
                     len(self.audio_clients))
            return [raw]
        if name == "audio_stop":
            self.audio_clients.discard(ws)
            log.info("audio_stop (%d remaining)", len(self.audio_clients))
            self._release_audio()
            return [raw]
        if name == "audio_samplerate":
            # We only ever produce 48 kHz: it is the codec's native rate, so
            # there is no resampling anywhere in the path.
            return [f"audio_samplerate:{audio.RATE}"]
        if name == "audio_stream_sample_type":
            return ["audio_stream_sample_type:float32"]
        if name == "audio_stream_channels":
            return ["audio_stream_channels:2"]
        if name in AUDIO_ECHO:
            return [raw]
        return None

    async def _handle_binary(self, data: bytes) -> None:
        """TX audio from a client."""
        hdr = audio.parse_header(data)
        if not hdr or hdr["type"] != audio.TYPE_TX_AUDIO:
            return
        if not self.playback:
            return
        pcm = audio.tx_payload_to_int16(hdr, data[audio.HEADER_BYTES:])
        if pcm is not None:
            self.playback.write(pcm)

    # ---------- client lifecycle ----------

    async def handler(self, ws) -> None:
        peer = getattr(ws, "remote_address", None)
        log.info("client connected: %s", peer)
        self.clients.add(ws)
        try:
            # One command per WebSocket message; settings, then ready, then
            # start. Clients may latch cached settings on READY.
            await self._send(ws, self.bridge.init_burst())
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    await self._handle_binary(bytes(raw))
                    continue
                for part in raw.split(";"):
                    part = part.strip()
                    if not part:
                        continue
                    name, _, argstr = part.partition(":")
                    name = name.strip().lower()
                    args = [a.strip() for a in argstr.split(",")] if argstr \
                        else []
                    srv = self._handle_server_cmd(name, args, part, ws)
                    if srv is not None:
                        await self._send(ws, srv)
                        continue
                    # Stop pacing the moment an unkey is asked for. The
                    # bridge then spends up to 1.5 s confirming TQ0, and
                    # chrono frames sent during that window arrive after
                    # the client believes it has stopped transmitting.
                    if name == "trx" and len(args) >= 2 and args[1] == "false":
                        if self.chrono_task is not None:
                            self.chrono_task.cancel()
                            self.chrono_task = None
                    reply, notify = await asyncio.to_thread(
                        self.bridge.handle, part, ws)
                    await self._send(ws, reply)
                    # A state change goes to everyone, including the client
                    # that caused it -- that is what makes the broker
                    # authoritative rather than optimistic.
                    await self.broadcast(notify)
                    if any(m.startswith("trx:") for m in notify):
                        self.sync_chrono()
        except ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)
            self.audio_clients.discard(ws)
            log.info("client disconnected: %s", peer)
            # If this client was holding PTT, unkey immediately -- other
            # clients still being connected is not a reason to keep
            # transmitting into a link that just died.
            msg = await asyncio.to_thread(self.bridge.release_client, ws)
            if msg:
                await self.broadcast([msg])
            self.sync_chrono()
            self._release_audio()
            if not self.clients:
                await asyncio.to_thread(self.bridge.force_rx)

    # ---------- background tasks ----------

    async def watchdog_task(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            msg = await asyncio.to_thread(self.bridge.ptt_watchdog)
            if msg:
                await self.broadcast([msg])
                self.sync_chrono()

    async def smeter_task(self, period: float = 0.2) -> None:
        """S-meter poll. Suppressed during transmit: SM returns 0000 in TX
        anyway, and the reference warns against polling while transmitting."""
        while True:
            await asyncio.sleep(period)
            if not self.clients or self.bridge.state.transmitting:
                continue
            dbm = await asyncio.to_thread(self.bridge.read_smeter)
            if dbm is not None:
                await self.broadcast([f"rx_smeter:0,{dbm}"])

    async def reconcile_task(self, period: float = 3.0) -> None:
        """AI2 reports only a subset of controls, so sweep periodically and
        push anything that drifted. Cheap: one IF read."""
        while True:
            await asyncio.sleep(period)
            if not self.clients:
                continue
            before = vars(self.bridge.state).copy()
            await asyncio.to_thread(self.bridge.refresh_if)
            after = vars(self.bridge.state)
            if after != before:
                s = self.bridge.state
                await self.broadcast([
                    f"vfo:0,0,{s.vfo_a}",
                    f"modulation:0,{s.mode}",
                    f"split_enable:0,{tci.bool_str(s.split)}",
                    f"trx:0,{tci.bool_str(s.transmitting)}",
                ])

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        async with serve(self.handler, self.host, self.port,
                         max_size=2 ** 20, process_request=http_handler):
            log.info("TCI server listening on ws://%s:%d", self.host, self.port)
            log.info("web UI at http://%s:%d/",
                     "<pi-address>" if self.host == "0.0.0.0" else self.host,
                     self.port)
            await asyncio.gather(self.watchdog_task(), self.reconcile_task(),
                                 self.smeter_task())


async def amain(args) -> None:
    cat = k3cat.K3Cat(args.device, args.baud)
    cat.open()
    info = cat.identify()
    log.info("radio: id=%s fw=%s options=%r k3s=%s sub_rx=%s",
             info["id"], info["fw"], info["options"],
             info["is_k3s"], info["has_subrx"])
    if info["id"] != "ID017;":
        log.warning("unexpected ID response %r -- is the radio on?",
                    info["id"])

    bridge = tci.Bridge(cat)
    bridge.prime()
    server = Server(bridge, args.host, args.port, args.alsa, args.no_audio)
    cat.on_event = lambda msg: server.broadcast_threadsafe(
        bridge.on_cat_event(msg))
    cat.enable_auto_info()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    task = asyncio.create_task(server.run())
    await stop.wait()
    log.info("shutting down")
    task.cancel()
    server.audio_clients.clear()
    server._release_audio()
    bridge.force_rx()
    cat.close()


def main() -> None:
    p = argparse.ArgumentParser(description="K3 -> TCI bridge")
    p.add_argument("--device", default=DEFAULT_DEV)
    p.add_argument("--baud", type=int, default=38400)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--alsa", default=DEFAULT_ALSA)
    p.add_argument("--no-audio", action="store_true",
                   help="control only; leave the ALSA devices alone")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-7s %(message)s",
        datefmt="%H:%M:%S")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
