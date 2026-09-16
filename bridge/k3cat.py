"""K3 CAT transport.

One serial port carries two kinds of traffic: replies to our own GETs, and
unsolicited AI2 messages the radio emits when the operator touches the front
panel. A single reader thread parses every ';'-terminated message and routes
it -- to a waiting request if one matches, otherwise to the event callback.
`TB` is the one exception: its decoded text may itself contain semicolons,
so it is framed by the character count it carries. See `ask_text`.

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


# Outstanding TB replies tolerated before assuming they are lost. Poll
# intervals are longer than the request timeout, so this only grows when the
# radio is deferring commands, and only by one per poll.
_TB_MAX_OWED = 3


def _tb_len(buf: bytes) -> int | None:
    """Total byte length of the `TB` frame at the head of `buf`, or None.

    `TBtrrs;` -- t is the count of TX characters still to be sent, rr the
    count of RX characters available (00-40), s exactly rr characters of
    decoded text. So the frame is 5 header + rr text + 1 terminator, and
    the empty reply `TB000;` is 6.

    None means rr did not parse, which should not happen; the caller falls
    back to ';' framing rather than blocking the reader forever on a count
    that will never be satisfied.
    """
    try:
        rr = int(buf[3:5])
    except ValueError:
        return None
    return 5 + rr + 1


def open_serial(port: str, baud: int = 38400,
                timeout: float = 0.1) -> serial.Serial:
    """Open the CAT port with DTR and RTS LOW.

    Every program that opens this port must do it this way. pyserial
    asserts both lines by default, and the K3 can be told to read DTR as
    KEY and RTS as PTT -- so a port opened the default way is a key-down at
    a radio configured for it. That is not hypothetical: it happened here,
    and stopping it took unplugging the USB lead.

    The states are set before open() rather than after, because pyserial
    applies them inside open() as soon as it has the descriptor.
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = timeout
    ser.rtscts = False
    ser.dsrdtr = False
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


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
        # TB replies asked for but not yet consumed; see _read_loop.
        self._tb_owed = 0
        self.tx_test: bool | None = None

    # ---------- lifecycle ----------

    def open(self) -> None:
        # BOTH MODEM LINES LOW, BEFORE THE PORT IS OPENED. pyserial asserts
        # DTR and RTS by default, and the K3 can be told to read them as KEY
        # and PTT (its RS232 menu). With DTR=KEY set at the radio, opening
        # this port is a key-down: starting the bridge put a carrier on the
        # air and held it, and it took unplugging the USB lead to stop --
        # confirmed at this station, which is why the radio now sits at
        # OFF/OFF. That workaround should not be what keeps it safe.
        #
        # The lines are configured rather than merely left alone: a port
        # opened with them low is safe whatever the radio is set to, and it
        # is also the precondition for ever driving PTT from RTS, which is
        # fail-safe in a way CAT cannot be (the process dying drops the
        # line, where `TX;` needs something still alive to send `RX;`).
        #
        # The old bench note read "modem control lines are irrelevant, the
        # bridge needn't manage them". They were irrelevant in that session
        # because the radio had them switched off.
        self._ser = open_serial(self.port, self.baud, timeout=0.1)
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

    def set_ptt_line(self, on: bool) -> None:
        """Drive RTS, which the K3 reads as PTT when its RS232 menu says so.

        Harmless when it does not: the line moves and the radio ignores it.
        That is what makes it safe to drop this line on every unkey, keying
        path regardless -- the cheapest way to be sure a transmission cannot
        outlive the thing that started it.
        """
        if self._ser is None:
            return
        try:
            self._ser.rts = on
        except OSError as exc:              # a port that went away
            log.error("could not set RTS %s: %s", "on" if on else "off", exc)

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
            while True:
                # TB carries its own length, because the decoded text it
                # returns may contain semicolons -- legal in RTTY and PSK
                # (programmer's reference, TB note 1). Partitioning on the
                # first ';' would cut such a reply in half and hand the tail
                # to _dispatch as an unsolicited message, where a fragment
                # like " DE;" reads as the real command DE. So while a TB
                # GET is outstanding and one is at the head of the buffer,
                # take exactly the bytes it declares.
                #
                # Scoped as tightly as possible: the radio never sends TB
                # unsolicited (it is GET only), so this path can only open
                # for a reply we asked for.
                if self._tb_owed and buf.startswith(b"TB") and len(buf) >= 5:
                    need = _tb_len(buf)
                    if need is None:            # malformed count: fall
                        self._tb_owed = 0       # through and resync on ';'
                    elif len(buf) < need:
                        break                   # rest of the text in flight
                    else:
                        msg = bytes(buf[:need]).decode("ascii", "replace")
                        buf = bytearray(buf[need:])
                        self._tb_owed -= 1
                        # Only the NEWEST reply answers the request now in
                        # flight. Anything still owed behind this one means
                        # it belongs to a request that timed out and was
                        # abandoned; delivering it would answer the current
                        # poll with the previous poll's text and leave the
                        # reply that really belongs to it to be framed on
                        # ';'. Frame it either way -- that is what keeps a
                        # semicolon in the text out of the command stream --
                        # but drop it rather than dispatch it.
                        if self._tb_owed == 0:
                            self._dispatch(msg)
                        else:
                            log.debug("dropped stale TB reply: %r", msg)
                        continue
                if b";" not in buf:
                    break
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

    def ask(self, cmd: str, timeout: float = 0.6,
            quiet: bool = False) -> str | None:
        """GET. Returns the response, '?;', or None on timeout.

        Band changes defer all command handling for up to 500 ms, so callers
        crossing a band edge should pass a longer timeout.

        `quiet` is for the callers where NO ANSWER IS THE ANSWER. While the
        radio is playing a `KYW` message it defers every following command
        until the message has been sent, so a poll that times out there is
        reporting "still sending" rather than a fault -- and logging each one
        as a warning buried a real problem under fifteen false ones per
        message. Those callers handle the None themselves.
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
                (log.debug if quiet else log.warning)(
                    "timeout waiting for %s", body)
                return None

    def ask_text(self, timeout: float = 0.6) -> str | None:
        """`TB;` -- the received-text buffer, framed by count, not by ';'.

        Returns the raw `TBtrrs;` response, `'?;'`, or None on timeout.

        This is a normal GET in every respect except framing: the reader
        needs to know a TB reply is coming so it can take the declared
        number of characters instead of stopping at the first semicolon.
        See _read_loop for why that matters.
        """
        q: queue.Queue = queue.Queue(maxsize=1)
        with self._tx_lock:
            if self._tb_owed >= _TB_MAX_OWED:
                # Replies that never came at all, which should not happen --
                # TB always answers. Assume they are gone rather than
                # discarding every future reply as stale forever.
                log.warning("%d TB replies never arrived; resyncing",
                            self._tb_owed)
                self._tb_owed = 0
            with self._pending_lock:
                self._pending = ("TB", q)
            self._tb_owed += 1
            self._ser.write(b"TB;")
            self._ser.flush()
            try:
                r = q.get(timeout=timeout)
            except queue.Empty:
                with self._pending_lock:
                    self._pending = None
                # The reply is still owed, and saying so is what protects
                # the next poll: the reader keeps framing by count, so a
                # semicolon in the abandoned text cannot reach the command
                # stream, and it knows to drop that reply rather than hand
                # it to whoever asks next.
                log.warning("timeout waiting for TB")
                return None
            if r and not r.startswith("TB"):
                # '?;' answered the request, so no TB frame is coming for it.
                self._tb_owed = max(0, self._tb_owed - 1)
            return r

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
