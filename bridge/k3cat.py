"""K3 CAT transport.

One serial port carries two kinds of traffic: replies to our own GETs, and
unsolicited AI2 messages the radio emits when the operator touches the front
panel. A single reader thread parses every ';'-terminated message and routes
it -- to a waiting request if one matches, otherwise to the event callback.

Implements the global rules from k3-tci-command-map.md:
  * K31 + K20 at startup
  * set-verify-retry (a SET can be dropped silently, with no '?;')
  * '?;' handled as a real answer, never a hang
  * AI2 echo-loop guard, so our own SETs don't come back as "hardware" events
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time

import serial

log = logging.getLogger("k3cat")

# Leading command letters, optionally '$' for the sub receiver.
_CMD_RE = re.compile(r"^([A-Z]{2,3})(\$?)")


def cmd_prefix(s: str) -> str:
    m = _CMD_RE.match(s.upper())
    return (m.group(1) + m.group(2)) if m else ""


class K3Cat:
    def __init__(self, port: str, baud: int = 38400, on_event=None):
        self.port, self.baud = port, baud
        self.on_event = on_event
        self._ser: serial.Serial | None = None
        self._tx_lock = threading.RLock()     # one request in flight
        self._pending: tuple[str, queue.Queue] | None = None
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        # echo-loop guard: prefix -> expiry time
        self._recent_sets: dict[str, float] = {}
        self.tx_test: bool | None = None

    # ---------- lifecycle ----------

    def open(self) -> None:
        self._ser = serial.Serial(self.port, self.baud, timeout=0.1)
        time.sleep(0.2)
        self._ser.reset_input_buffer()

        # Global rule 1: K3 extended mode on, K2 mode left at its K20 default.
        self._ser.write(b"K31;")
        self._ser.flush()
        time.sleep(0.25)

        # Read IC here, BEFORE the reader thread exists. Every IC byte has
        # bit 7 set, and the reader is line-oriented and decodes as text, so
        # it would replace those bytes and lose the flags. Doing it now also
        # avoids racing the reader for the response.
        self._ser.reset_input_buffer()
        self._ser.write(b"IC;")
        self._ser.flush()
        time.sleep(0.35)
        raw = self._ser.read(self._ser.in_waiting or 64)
        self.tx_test = (bool(raw[2] & 0x20)
                        if raw.startswith(b"IC") and len(raw) >= 8 else None)

        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._stop.set()
        if self._reader:
            self._reader.join(timeout=2)
        if self._ser:
            try:
                self._ser.write(b"AI0;")   # stop unsolicited traffic
                self._ser.flush()
            except Exception:
                pass
            self._ser.close()

    # ---------- reader ----------

    def _read_loop(self) -> None:
        buf = bytearray()
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(256)
            except Exception as exc:
                log.error("serial read failed: %s", exc)
                break
            if not chunk:
                continue
            buf += chunk
            while b";" in buf:
                raw, _, rest = buf.partition(b";")
                buf = bytearray(rest)
                msg = raw.decode("ascii", "replace").strip() + ";"
                if msg != ";":
                    self._dispatch(msg)

    def _dispatch(self, msg: str) -> None:
        prefix = cmd_prefix(msg)
        with self._pending_lock:
            pending = self._pending
            # '?;' is a valid answer to whatever we last asked (busy/limited
            # access), so it resolves the request rather than hanging it.
            if pending and (prefix == pending[0] or msg == "?;"):
                self._pending = None
                pending[1].put(msg)
                return
        # Unsolicited (AI2). Suppress the echo of our own recent SETs so a
        # command we issued is not re-broadcast as a hardware-origin change.
        now = time.monotonic()
        expiry = self._recent_sets.get(prefix)
        if expiry and expiry > now:
            del self._recent_sets[prefix]
            log.debug("swallowed own echo: %s", msg)
            return
        for k, v in list(self._recent_sets.items()):
            if v <= now:
                del self._recent_sets[k]
        if self.on_event:
            try:
                self.on_event(msg)
            except Exception:
                log.exception("on_event failed for %s", msg)

    # ---------- requests ----------

    def send(self, cmd: str) -> None:
        """Fire-and-forget SET. Marks the prefix so AI2's echo is ignored."""
        body = cmd.rstrip(";")
        with self._tx_lock:
            self._recent_sets[cmd_prefix(body)] = time.monotonic() + 1.5
            self._ser.write(body.encode() + b";")
            self._ser.flush()

    def ask(self, cmd: str, timeout: float = 0.6) -> str | None:
        """GET. Returns the response, '?;', or None on timeout.

        Band changes defer all command handling for up to 500 ms, so callers
        crossing a band edge should pass a longer timeout.
        """
        body = cmd.rstrip(";")
        prefix = cmd_prefix(body)
        q: queue.Queue = queue.Queue(maxsize=1)
        with self._tx_lock:
            with self._pending_lock:
                self._pending = (prefix, q)
            self._ser.write(body.encode() + b";")
            self._ser.flush()
            try:
                return q.get(timeout=timeout)
            except queue.Empty:
                with self._pending_lock:
                    self._pending = None
                log.warning("timeout waiting for %s", body)
                return None

    def set_verified(self, set_cmd: str, query: str, expect: str,
                     tries: int = 3, settle: float = 0.35) -> bool:
        """A SET can be dropped with no error at all -- observed with MD on
        the bench. Anything whose failure would corrupt later decisions goes
        through here rather than send()."""
        for attempt in range(tries):
            self.send(set_cmd)
            time.sleep(settle)
            got = self.ask(query)
            if got == expect:
                return True
            log.debug("set %s attempt %d: %s reads %s (want %s)",
                      set_cmd, attempt + 1, query, got, expect)
            time.sleep(0.2)
        log.warning("SET %s did not take (%s reads %s, wanted %s)",
                    set_cmd, query, got, expect)
        return False

    # ---------- startup helpers ----------

    def identify(self) -> dict:
        """ID / OM / firmware. OM is the only reliable sub-RX detection --
        the '$' commands answer even with no KRX3A fitted."""
        info = {"id": self.ask("ID"), "om": self.ask("OM"),
                "fw": self.ask("RVM"), "k3": self.ask("K3")}
        om = info["om"] or ""
        data = om[2:].strip(";").strip() if om.startswith("OM") else ""
        info["is_k3s"] = "R" in data
        info["has_subrx"] = len(data) > 3 and data[3] == "S"
        info["options"] = data
        return info

    def tx_test_active(self) -> bool | None:
        """Is the radio in TX TEST? True means transmissions produce NO RF.

        Sampled once at open() -- see there for why it cannot be read later.
        """
        return self.tx_test

    def enable_auto_info(self) -> None:
        """AI2 covers most front-panel events, but the reference warns only a
        subset of controls report. Callers still need a slow reconcile poll."""
        self.send("AI2")
        time.sleep(0.2)
