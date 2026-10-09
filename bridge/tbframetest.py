#!/usr/bin/env python3
"""Parsing that no radio can be in the loop for. The only test here that
runs without one.

Covers the `TB` decoded-text path, the `DS` icon path and the `IF` length
guard. They share a
file because they share a failure mode rather than a feature: both are
parsing bugs that produce no error, no log line and no visible breakage --
the kind that survives for months because everything downstream keeps
working off some other path. `IF`'s has now been found twice.

The decoded text a `TB` reply carries may contain semicolons, which is legal
in RTTY and PSK (programmer's reference, TB note 1). The CAT reader is
otherwise ';'-framed, so a reply like `TB008CQ; DE W;` would be cut in two:
a truncated `TB008CQ;` answering the request, and ` DE W;` dispatched as an
unsolicited message -- where `DE;` is a real K3 command.

Nothing about that failure is visible from outside: the decode panel shows
slightly wrong text and some other command appears to have been sent by the
operator. So it is tested here rather than on the air, and this is the one
test in the directory that needs no radio.

    python3 tbframetest.py
"""
import sys
import threading
import time

sys.modules.setdefault("serial", type(sys)("serial"))   # k3cat imports it

import k3cat
import tci


class FakeSerial:
    """Feeds canned bytes, optionally a few at a time, and records writes."""

    def __init__(self, chunk=256):
        self.outgoing = bytearray()
        self.written = []
        self.chunk = chunk
        self.lock = threading.Lock()
        self.replies = {}

    def feed(self, data: bytes):
        with self.lock:
            self.outgoing += data

    def read(self, n=1):
        time.sleep(0.002)
        with self.lock:
            take = min(len(self.outgoing), n, self.chunk)
            out, self.outgoing = bytes(self.outgoing[:take]), self.outgoing[take:]
        return out

    def write(self, data: bytes):
        self.written.append(data)
        reply = self.replies.get(bytes(data))
        if reply is not None:
            threading.Timer(0.01, self.feed, args=(reply,)).start()
        return len(data)

    def flush(self):
        pass


class StubCat:
    """Stands in for K3Cat where only a canned reply is needed."""

    def __init__(self, reply, asks=None):
        self.reply = reply
        self.asks = asks or {}          # command -> reply, for ask()
        self.sent = []                  # every SET, in order
        self.rts = False                # the PTT line, as the radio sees it

    def ask_text(self, timeout=0.6):
        return self.reply

    def ask_display(self, timeout=0.6, quiet=False):
        return self.reply

    def ask(self, cmd, timeout=0.6, quiet=False):
        return self.asks.get(cmd.rstrip(";"))

    def send(self, cmd):
        self.sent.append(cmd)

    def set_ptt_line(self, on):
        self.rts = on
        self.sent.append(f"<rts {'up' if on else 'down'}>")


def make_cat(chunk=256):
    events = []
    cat = k3cat.K3Cat("/dev/null", on_event=events.append)
    ser = FakeSerial(chunk=chunk)
    cat._ser = ser
    cat._stop.clear()
    cat._reader = threading.Thread(target=cat._read_loop, daemon=True)
    cat._reader.start()
    return cat, ser, events


def check(name, got, want, fails):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"       got  {got!r}")
        print(f"       want {want!r}")
        fails.append(name)


def main():
    fails = []

    print("=== TB framing ===")
    for label, frame, chunk in [
        ("empty buffer",        b"TB000;",                     256),
        ("plain text",          b"TB005CQ CQ;",                256),
        ("embedded semicolon",  b"TB008CQ; DE W;",             256),
        ("only a semicolon",    b"TB001;;",                    256),
        ("full 40 chars",       b"TB040" + b"A" * 40 + b";",   256),
        ("split byte by byte",  b"TB008CQ; DE W;",               1),
        ("leading spaces kept", b"TB006  CQ  ;",               256),
        ("tx count non-zero",   b"TB905DE W1;",                256),
    ]:
        cat, ser, events = make_cat(chunk)
        ser.replies[b"TB;"] = frame
        got = cat.ask_text(timeout=2.0)
        check(label, got, frame.decode(), fails)
        cat._stop.set()

    print("\n=== the reader stays in sync afterwards ===")
    # A TB reply with an embedded ';' followed immediately by a real
    # unsolicited message. If the frame were split, the tail would land in
    # events and FA would be mangled.
    cat, ser, events = make_cat()
    ser.replies[b"TB;"] = b"TB008CQ; DE W;FA00014030000;"
    got = cat.ask_text(timeout=2.0)
    check("TB reply intact", got, "TB008CQ; DE W;", fails)
    time.sleep(0.3)
    check("following FA intact", events, ["FA00014030000;"], fails)
    cat._stop.set()

    print("\n=== an unsolicited message arriving before the TB reply ===")
    cat, ser, events = make_cat()
    ser.replies[b"TB;"] = b"FA00007020880;TB005CQ CQ;"
    got = cat.ask_text(timeout=2.0)
    check("TB found behind it", got, "TB005CQ CQ;", fails)
    time.sleep(0.3)
    check("FA dispatched as event", events, ["FA00007020880;"], fails)
    cat._stop.set()

    print("\n=== malformed count resyncs rather than hanging ===")
    # A count that will never be satisfied must not park the reader forever
    # waiting for characters that are not coming. It gives up on the count,
    # falls back to ';' framing, and the NEXT request still works -- which
    # is the property that matters. (The malformed reply itself is not
    # delivered to the caller: cmd_prefix reads three leading letters where
    # it can, so "TBxyz" prefixes as "TBX" and does not match the pending
    # "TB". A real reply always has a digit third, so this is unreachable
    # short of a corrupted port.)
    cat, ser, events = make_cat()
    ser.replies[b"TB;"] = b"TBxyz;"
    check("malformed reply not delivered", cat.ask_text(timeout=0.4), None, fails)
    ser.replies[b"TB;"] = b"TB005CQ CQ;"
    check("reader recovered", cat.ask_text(timeout=2.0), "TB005CQ CQ;", fails)
    cat._stop.set()

    print("\n=== '?;' answers TB without leaving the reader armed ===")
    cat, ser, events = make_cat()
    ser.replies[b"TB;"] = b"?;"
    got = cat.ask_text(timeout=2.0)
    check("busy radio", got, "?;", fails)
    check("nothing left owed", cat._tb_owed, 0, fails)
    cat._stop.set()

    print("\n=== a late reply is framed, not split ===")
    # ask_text gives up, then the reply lands. It must still be taken by
    # count: the tail of a split frame would be dispatched as a command.
    cat, ser, events = make_cat()
    got = cat.ask_text(timeout=0.05)
    check("timed out", got, None, fails)
    ser.feed(b"TB008CQ; DE W;")
    time.sleep(0.3)
    check("late reply framed whole", events, ["TB008CQ; DE W;"], fails)
    cat._stop.set()

    print("\n=== an abandoned request does not poison the next one ===")
    # A TB GET can time out for real: a band change defers command handling
    # for up to 500 ms, inside the 0.6 s window. The reply then lands while
    # the NEXT poll is already waiting. If the reader delivers it, that poll
    # is answered with the previous poll's text -- decoded characters shown
    # twice, and the reply that actually belonged to it left to be framed on
    # ';' with a tail that dispatches as a command.
    cat, ser, events = make_cat()
    check("first request times out", cat.ask_text(timeout=0.05), None, fails)
    ser.feed(b"TB005AAAAA;")                    # reply to the abandoned one
    ser.replies[b"TB;"] = b"TB008BB; DE W;"     # reply to the next one
    got = cat.ask_text(timeout=2.0)
    check("stale reply never delivered", got != "TB005AAAAA;", True, fails)
    check("fresh reply framed by count", got, "TB008BB; DE W;", fails)
    time.sleep(0.3)
    check("no fragment dispatched as a command", events, [], fails)
    cat._stop.set()

    print("\n=== read_text parses the count, not the terminator ===")
    for label, reply, want in [
        ("empty",                "TB000;",           ""),
        ("plain",                "TB005CQ CQ;",      "CQ CQ"),
        ("embedded semicolon",   "TB008CQ; DE W;",   "CQ; DE W"),
        ("trailing spaces kept", "TB006  CQ  ;",     "  CQ  "),
        ("count beats length",   "TB003ABCDE;",      "ABC"),
        ("short of its count",   "TB020AB;",         None),
        ("busy radio",           "?;",               None),
        ("no reply",             None,               None),
        ("truncated header",     "TB0;",             None),
    ]:
        b = tci.Bridge(StubCat(reply))
        check(label, b.read_text(), want, fails)

    print("\n=== escaping survives the wire ===")
    # ';' and ',' are what the framing is made of -- the web UI splits
    # incoming data on ';' before it parses anything -- so neither may
    # survive escaping. Non-ASCII must come out as UTF-8 percent-encoding
    # or decodeURIComponent throws on it at the far end.
    for label, raw, want in [
        ("plain text",     "CQ CQ DE W1AW K",  "CQ CQ DE W1AW K"),
        ("semicolon",      "RST 599; QTH",     "RST 599%3B QTH"),
        ("comma",          "TNX, 73",          "TNX%2C 73"),
        ("percent",        "100% CPY",         "100%25 CPY"),
        ("control chars",  "A\r\nB",           "A%0D%0AB"),
        ("spaces kept",    " de w1aw ",        " de w1aw "),
        ("utf-8, not 8859", "\u00dcMLAUT",      "%C3%9CMLAUT"),
        ("unreadable byte", "\ufffd",           "%EF%BF%BD"),
    ]:
        check(label, tci.tci_escape(raw), want, fails)

    print("\n=== DS framing keeps the high bytes ===")
    # The icon bytes always have bit 7 set, and the display bytes can too
    # (a decimal point). ASCII decoding turned all of them into U+FFFD, which
    # is why DS was kept out of the reader until it was framed by length.
    # 0x3B is ';' -- legal as a display byte, and it must not end the frame.
    ds = b"DS" + b"14\xb03;59@" + bytes([0x80 | 0x10, 0x80 | 0x06]) + b";"
    assert len(ds) == k3cat._DS_LEN
    for label, chunk in [("whole", 256), ("byte by byte", 1)]:
        cat, ser, events = make_cat(chunk)
        ser.replies[b"DS;"] = ds
        got = cat.ask_display(timeout=2.0)
        check(f"DS {label}", got, ds.decode("latin-1"), fails)
        check(f"DS {label}: nothing unsolicited", events, [], fails)
        check(f"DS {label}: nothing left owed", cat._ds_owed, 0, fails)
        cat._stop.set()

    cat, ser, events = make_cat()
    ser.replies[b"DS;"] = ds + b"FA00007050000;"
    cat.ask_display(timeout=2.0)
    time.sleep(0.1)
    check("DS: the reader stays in sync", events, ["FA00007050000;"], fails)
    cat._stop.set()

    # A frame that does not end where its length says resyncs on ';'
    # rather than handing a misaligned frame to the parser.
    cat, ser, events = make_cat()
    ser.replies[b"DS;"] = b"DS1234567;"
    got = cat.ask_display(timeout=0.5)
    check("DS short frame: not delivered as DS", (got or "").startswith("DS")
          and len(got) == 13, False, fails)
    cat._stop.set()

    print("\n=== NR and notch read from the icon-flash byte ===")
    for label, f, nr, notch in [
        ("all off",      0x80,               False, "off"),
        ("NR on",        0x80 | 0x04,        True,  "off"),
        ("auto notch",   0x80 | 0x02,        False, "auto"),
        ("manual notch", 0x80 | 0x03,        False, "manual"),
        ("everything",   0x80 | 0x07,        True,  "manual"),
        ("other icons",  0x80 | 0x38,        False, "off"),
    ]:
        b = tci.Bridge(StubCat(("DS" + "@" * 8 + chr(0x80) + chr(f) + ";")))
        b.refresh_display()
        check(f"DS {label}", (b.state.nr, b.state.notch), (nr, notch), fails)
    b = tci.Bridge(StubCat("DS" + "@" * 8 + "@" + "\x04" + ";"))
    b.state.nr, b.state.notch = False, "auto"
    b.refresh_display()
    check("DS without bit 7 leaves state alone",
          (b.state.nr, b.state.notch), (False, "auto"), fails)

    print("\n=== IF is checked for the fields read, not a total length ===")
    # Found twice now. refresh_if had `len(r) < 38` and rejected every reply
    # from a radio that sends 37; the fix missed the identical guard in
    # on_cat_event, where it was even quieter -- a band change auto-reports
    # FA and MD next to IF, and those branches worked, so frequency and mode
    # tracked while split and the TX flag waited on the 3 s reconcile.
    SHORT = "IF00007020880     +000000 0003000001;"      # 37, this radio
    REF   = "IF00014030000     -000000 0003000011 ;"     # 38, the reference
    for label, msg, want in [
        ("37-char form (this radio)", SHORT, 7020880),
        ("38-char form (reference)",  REF,  14030000),
        ("exactly the last field",    SHORT[:33], 7020880),
    ]:
        b = tci.Bridge(StubCat(None))
        b.state.vfo_a = 1
        out = b.on_cat_event(msg)
        check(label + ": parsed", b.state.vfo_a, want, fails)
        check(label + ": broadcast",
              any(m.startswith(f"vfo:0,0,{want}") for m in out), True, fails)

    for label, msg in [("one short of it", SHORT[:32]), ("stub", "IF;")]:
        b = tci.Bridge(StubCat(None))
        b.state.vfo_a = 1
        check(f"too short to read ({label}): ignored",
              (b.on_cat_event(msg), b.state.vfo_a), ([], 1), fails)

    print("\n=== KY chunks leave room for the space that follows them ===")
    # Every chunk but the last is written with a trailing space, so that
    # words do not run together across a boundary -- which means the space
    # is part of KY's 24-character budget, not an extra. A 24-character
    # chunk plus that space is a 25-character command, and what the radio
    # does with the overrun is its business, not something to find out on
    # the air. The first case below produced exactly that.
    b = tci.Bridge(StubCat(None))
    for label, text, want in [
        ("chunk that used to overrun", "testing a word or two of cw",
         ["testing a word or two ", "of cw"]),
        ("fits exactly, stays whole", "x" * 24, ["x" * 24]),
        ("one past, splits",          "x" * 25, ["x" * 24, "x"]),
        # A word too long to fit anywhere is cut, and no separator goes into
        # the cut -- the old code put a space there and sent it as two words.
        ("word longer than a chunk",  "x" * 50,
         ["x" * 24, "x" * 24, "x" * 2]),
        # The remainder of a cut word packs with what follows it, so the
        # space reappears where it belongs -- at the word boundary.
        ("cut word, then a real one", "x" * 30 + " de",
         ["x" * 24, "x" * 6 + " de"]),
        ("nothing to send",           "", []),
    ]:
        check(label, b._cw_chunks(text), want, fails)

    # The two invariants behind all of them: no payload is over the limit,
    # and joining them reproduces the text exactly -- spaces included, which
    # is the whole point of carrying the separator inside the chunk.
    for text in ["cq cq de kx3h k", "x" * 24, "x" * 71,
                 "testing a word or two of cw from here",
                 "x" * 30 + " de kx3h", " ".join(["word"] * 30)]:
        chunks = b._cw_chunks(text)
        check(f"payload width within {tci.Bridge.CW_MAX} for {text[:18]!r}",
              max((len(c) for c in chunks), default=0) <= tci.Bridge.CW_MAX,
              True, fails)
        check(f"nothing lost for {text[:18]!r}", "".join(chunks), text, fails)

    print("\n=== cw_macros: the receiver is not part of the text ===")
    # `cw_macros:<trx>,<text>` leads with the receiver. Passing it through
    # as text keyed "0,CQ TEST" -- a stray zero and comma ahead of every
    # macro a logger sent -- and the radio gives no sign anything is wrong.
    # Driven through handle() so the arg split is the real one.
    for label, line, want in [
        ("receiver dropped",          "cw_macros:0,CQ TEST KX3H;", ["CQ TEST KX3H"]),
        ("commas in the text kept",   "cw_macros:0,CQ,CQ;",        ["CQ,CQ"]),
        ("text that starts with digits", "cw_macros:0,599 TU;",    ["599 TU"]),
        ("no receiver: all text",     "cw_macros:CQ TEST;",        ["CQ TEST"]),
        ("receiver we do not have",   "cw_macros:1,CQ TEST;",      []),
        ("negative receiver",         "cw_macros:-1,CQ;",          []),
        ("receiver and no text",      "cw_macros:0,;",             []),
        ("receiver only",             "cw_macros:0;",              []),
        ("cw_msg is unchanged",       "cw_msg:TEST DE K3;",        ["TEST DE K3"]),
    ]:
        b = tci.Bridge(StubCat(None))
        b.state.mode = "cwl"
        sent = []
        b._cw_send = lambda text, sent=sent: sent.append(text) or True
        b.handle(line)
        check(label, sent, want, fails)

    print("\n=== CW: a stop interrupts, and a message unkeys only once sent ===")
    # Measured on the air (cwstoptest.py, 2026-10-04): with `KYW` chunks the
    # radio defers every following command until its text has been sent, so
    # the stop's RX; waited behind the whole message -- a stop 1.5 s into
    # 16 s of CW ended it 15 s later. With plain `KY` it unkeyed within half
    # a second and discarded the buffer. And TB's to-send count reaches 0
    # when the LAST character starts, so the unkey waits one character more.
    class FakeK3(StubCat):
        """KY room, TQ that follows TX;/RX;, and a scripted TB to-send count."""

        def __init__(self, tb=None):
            super().__init__(None, {"VX": "VX1;", "KS": "KS020;"})
            self.tq = "TQ0;"
            self.tb = list(tb or [])     # to-send counts, one per TB poll
            self.lock = threading.Lock()

        def ask(self, cmd, timeout=0.6, quiet=False):
            c = cmd.rstrip(";")
            if c == "KY":
                time.sleep(0.02)
                return "KY0;"
            if c == "TQ":
                return self.tq
            return self.asks.get(c)

        def ask_text(self, timeout=0.6):
            with self.lock:
                n = self.tb.pop(0) if len(self.tb) > 1 else (self.tb[0] if self.tb else 0)
            self.sent.append(f"TB?{n}")
            time.sleep(0.02)
            return f"TB{n}00;"

        def send(self, cmd):
            super().send(cmd)
            if cmd == "TX":
                self.tq = "TQ1;"
            elif cmd == "RX":
                self.tq = "TQ0;"

    def run(b):
        """Wait for the CW worker, whichever thread is current, to finish."""
        for _ in range(100):
            t = b._cw_thread
            if t is None or not t.is_alive():
                return
            t.join(timeout=0.1)

    # A normal message: written as plain KY, unkeyed only after TB reads 0
    # AND the last character has had time to go out.
    b = tci.Bridge(FakeK3(tb=[2, 1, 0]))
    b.state.mode = "cwl"
    t0 = time.monotonic()
    check("message accepted", b._cw_send("tu kx3h"), True, fails)
    run(b)
    sent = b.cat.sent
    check("written as plain KY, never KYW",
          ([c for c in sent if c.startswith("KY")]), ["KY tu kx3h"], fails)
    check("keyed first with TX;", sent[0], "TX", fails)
    check("unkeyed once, after the to-send count reached 0",
          sent[-3:], ["TB?0", "<rts down>", "RX"], fails)
    check("one RX in all", sent.count("RX"), 1, fails)
    # "H" is 7 dot units: 0.42 s at 20 WPM, plus a dot of margin.
    check("waited out the last character after TB reached 0",
          time.monotonic() - t0 >= 0.45, True, fails)
    check("worker gone", b._cw_thread, None, fails)
    check("the bridge knows the message is over", b.state.transmitting, False, fails)

    # ...and says so to clients at once, rather than at the next reconcile.
    b = tci.Bridge(FakeK3(tb=[1, 0]))
    b.state.mode = "cwl"
    told = []
    b.broadcast = told.extend
    b._cw_send("e")
    run(b)
    check("end of a message is broadcast as trx:0,false", told, ["trx:0,false"], fails)

    # A stop mid-message: RX at once, nothing written after it.
    b = tci.Bridge(FakeK3(tb=[9]))        # the radio stays busy
    b.state.mode = "cwl"
    long_text = " ".join(["kx3h"] * 40)
    b._cw_send(long_text)
    time.sleep(0.2)
    stop_at = len(b.cat.sent)
    b.cw_stop()
    after = b.cat.sent[stop_at:]
    check("stop unkeys by both routes at once",
          after[:2], ["<rts down>", "RX"], fails)
    run(b)
    later = [c for c in b.cat.sent[stop_at:] if c.startswith("KY ")]
    check("nothing written after the stop", later, [], fails)
    check("queue emptied", b._cw_queue, [], fails)
    check("worker gone after a stop", b._cw_thread, None, fails)
    check("stopped well before writing everything",
          len([c for c in b.cat.sent if c.startswith("KY ")])
          < len(b._cw_chunks(long_text)), True, fails)

    # More text while the worker waits for the end: carried on, one unkey.
    b = tci.Bridge(FakeK3(tb=[3, 3, 3, 3, 3, 2, 1, 0]))
    b.state.mode = "cwl"
    b._cw_send("cq test")
    time.sleep(0.15)                       # worker now waiting on TB
    b._cw_send("de kx3h")
    run(b)
    writes = [c for c in b.cat.sent if c.startswith("KY ")]
    check("text queued during the end-wait is sent too",
          writes, ["KY cq test", "KY de kx3h"], fails)
    check("and the radio is unkeyed once, at the very end",
          (b.cat.sent.count("RX"), b.cat.sent[-1]), (1, "RX"), fails)
    check("not re-keyed for the follow-on text",
          b.cat.sent.count("TX"), 1, fails)

    # cw_macros_stop over TCI: confirms the unkey and tells every client,
    # so a message sent straight after it keys the radio again.
    b = tci.Bridge(FakeK3(tb=[9]))
    b.state.mode = "cwl"
    b.handle("cw_macros:0,cq cq cq de kx3h kx3h kx3h test;")
    time.sleep(0.15)
    check("transmitting while the macro runs", b.state.transmitting, True, fails)
    _, bcast = b.handle("cw_macros_stop;")
    check("stop broadcasts trx:0,false", bcast, ["trx:0,false"], fails)
    check("and the bridge knows it is receiving", b.state.transmitting, False, fails)
    stop_at = len(b.cat.sent)
    b.handle("cw_macros:0,tu;")
    check("a message straight after the stop keys the radio again",
          "TX" in b.cat.sent[stop_at:], True, fails)
    b.cw_stop()
    run(b)

    # A stop with nothing sending reports nothing, and sends no RX; to a
    # radio that says it is receiving: a bare RX; hangs a receiving K3's DSP
    # when TEXT DEC is on, and a logger sends a stop every time it closes.
    b = tci.Bridge(FakeK3())
    b.state.mode = "cwl"
    check("stop while idle: no trx broadcast", b.handle("cw_macros_stop;"), ([], []), fails)
    check("and no RX into a receiving radio", "RX" in b.cat.sent, False, fails)
    b.handle("trx:0,false;")
    check("nor for an unkey while receiving", "RX" in b.cat.sent, False, fails)

    # Keyed from the front panel, which the bridge did not do: TQ1 says so,
    # and the stop unkeys.
    b = tci.Bridge(FakeK3())
    b.state.mode = "cwl"
    b.cat.tq = "TQ1;"
    b.handle("cw_macros_stop;")
    check("stop while keyed by someone else still sends RX", "RX" in b.cat.sent, True, fails)

    # No answer to TQ: unkey anyway.
    b = tci.Bridge(FakeK3())
    b.state.mode = "cwl"
    b.cat.tq = None
    b.cw_stop()
    check("stop with TQ unanswered still sends RX", "RX" in b.cat.sent, True, fails)

    check("cw_char_seconds: E is one dot", round(tci.cw_char_seconds("E", 20), 3), 0.06, fails)
    check("cw_char_seconds: 0 is 19 units", round(tci.cw_char_seconds("0", 20), 3), 1.14, fails)
    check("cw_char_seconds: unknown is timed as the longest",
          tci.cw_char_seconds("~", 20) >= tci.cw_char_seconds("0", 20), True, fails)

    print("\n=== keying by line, and coming back from it ===")
    # The line is worth having because it fails safe -- it drops when this
    # process dies, where TX; needs something alive to send RX;. But a radio
    # whose RS232 menu is not set to PTT ignores RTS completely and says
    # nothing, so an unconfirmed line must fall back to TX; rather than be
    # trusted: the bridge would otherwise report transmitting, the client
    # would send audio, and nothing would go out.
    KEYED, UNKEYED = {"TQ": "TQ1;"}, {"TQ": "TQ0;"}

    b = tci.Bridge(StubCat(None, KEYED), ptt_line=True)
    check("line keys, and no TX; is needed",
          (b._key_on(), b.cat.sent), (True, ["<rts up>"]), fails)

    # A radio that ignores the line never reaches TQ1, so TX; must follow.
    b = tci.Bridge(StubCat(None, UNKEYED), ptt_line=True)
    check("line ignored: falls back to TX;",
          (b._key_on(), b.cat.sent),
          (False, ["<rts up>", "<rts down>", "TX"]), fails)

    b = tci.Bridge(StubCat(None, KEYED), ptt_line=False)
    check("line mode off: TX; only",
          (b._key_on(), b.cat.sent), (True, ["TX"]), fails)

    # Unkeying takes BOTH routes whatever keyed it: dropping an unused line
    # costs nothing, while getting it wrong costs a transmitter left running.
    # But only when the radio may be keyed: RX; into a receiving K3 with
    # TEXT DEC on hangs its receive DSP.
    for label, line in [("line mode", True), ("CAT mode", False)]:
        b = tci.Bridge(StubCat(None, KEYED), ptt_line=line)
        b._key_off()
        check(f"unkey drops the line and sends RX ({label})",
              b.cat.sent, ["<rts down>", "RX"], fails)
        b = tci.Bridge(StubCat(None, UNKEYED), ptt_line=line)
        b._key_off()
        check(f"unkey of a receiving radio sends nothing ({label})",
              b.cat.sent, [], fails)

    print("\n=== the PTT deadline does not outlive the transmission ===")
    # The CW path arms the watchdog in case its queued RX; goes missing.
    # Nothing disarmed it when that RX; worked, so the watchdog fired into a
    # radio already receiving -- and a deadline left lying around can fire
    # into a LATER transmission, one started at the front panel, which sets
    # no deadline of its own to overwrite it.
    RX_IF = "IF00007020880     +000000 0003000001;"      # field 28 = 0
    TX_IF = RX_IF[:28] + "1" + RX_IF[29:]
    for label, msg, want in [
        ("radio seen receiving: disarmed", RX_IF, None),
        ("radio seen transmitting: kept",  TX_IF, 1234.0),
    ]:
        b = tci.Bridge(StubCat(None))
        b._ptt_deadline = 1234.0
        b.on_cat_event(msg)
        check(label, b._ptt_deadline, want, fails)

    # The owner goes with it. A CW stop leaves no deadline to clear, so the
    # owner used to outlive the transmission -- and disconnecting minutes
    # later logged "PTT owner disconnected while keyed" at a radio that had
    # been receiving throughout, and sent it a pointless RX.
    b = tci.Bridge(StubCat(None))
    b._ptt_owner = "someone"
    b.on_cat_event(RX_IF)
    check("radio seen receiving: owner released", b._ptt_owner, None, fails)

    # But not in the moment just after keying, where an IF already in flight
    # can report the state from before it.
    b = tci.Bridge(StubCat(None))
    b._ptt_owner, b._ptt_deadline = "someone", 1234.0
    b._keyed_at = time.monotonic()
    b.on_cat_event(RX_IF)
    check("a stale snapshot does not disarm a fresh transmission",
          (b._ptt_owner, b._ptt_deadline), ("someone", 1234.0), fails)

    print("\n=== the CW watchdog estimate covers the message ===")
    # Too long only delays a backstop that should never fire; too short cuts
    # the operator off mid-word. So it must exceed PARIS timing at any speed
    # the K3 offers.
    for wpm in (8, 20, 50):
        for text in ["hello", "cq cq de kx3h k", "x" * 200]:
            paris = 12 * len(text) / wpm
            check(f"{len(text)} chars at {wpm} wpm covers {paris:.0f}s",
                  tci.cw_seconds(text, wpm) > paris, True, fails)

    print("\n=== an S-meter count is range-checked before it is a signal ===")
    # Both curves are unbounded above -- SMH 999 converts to +853 dBm -- and
    # the UI clamps its bar at S9+60, so ANY over-range count paints the
    # same full-scale meter as a real S9+60 signal. Nothing downstream can
    # tell one from the other, which is why it is caught here: a pinned
    # meter is over in a fifth of a second and leaves nothing behind.
    for label, asks, want in [
        # Expected values follow the generator calibration in read_smeter
        # (S9 = SMH 37, bend at 55), not the programmer's reference anchors,
        # which read 3-8 dB low on this radio.
        ("SMH near the floor",   {"SMH": "SMH005;"}, -113),
        ("SMH at S9",            {"SMH": "SMH037;"},  -73),
        ("SMH just above bend",  {"SMH": "SMH056;"},  -51),
        ("SMH at S9+60",         {"SMH": "SMH104;"},  -13),
        ("SMH at full scale",    {"SMH": "SMH140;"},   15),
        ("SMH over full scale",  {"SMH": "SMH141;"},  None),
        ("SMH wildly over",      {"SMH": "SMH999;"},  None),
        ("SMH not a number",     {"SMH": "SMHxyz;"},  None),
        # SM is only reached when SMH gives nothing at all.
        ("SM at S9",             {"SM": "SM0009;"},   -73),
        ("SM at S9+60",          {"SM": "SM0021;"},   -13),
        # Above SM's K31 range: the field stays four digits when K31 is
        # lost, so this is also what a radio back in K2x mode looks like.
        ("SM over its K31 range", {"SM": "SM0022;"},  None),
    ]:
        b = tci.Bridge(StubCat(None, asks))
        check(label, b.read_smeter(), want, fails)

    # The preamp and the attenuator sit in front of the meter, 10 dB each
    # by assumption. With the pad in, the same count is a signal 10 dB
    # stronger at the antenna; with the preamp on, 10 dB weaker; with both,
    # they cancel. Out-of-range stays None.
    for label, asks, att, pre, want in [
        ("ATT on: S9 count reads 10 dB up", {"SMH": "SMH037;"}, 1, 0, -63),
        ("ATT on: SM fallback too",         {"SM": "SM0009;"},  1, 0, -63),
        ("ATT on: over range still None",   {"SMH": "SMH141;"}, 1, 0, None),
        ("PRE on: S9 count reads 10 dB down", {"SMH": "SMH037;"}, 0, 1, -83),
        ("PRE and ATT cancel",              {"SMH": "SMH037;"}, 1, 1, -73),
    ]:
        b = tci.Bridge(StubCat(None, asks))
        b.state.att, b.state.preamp = bool(att), bool(pre)
        check(label, b.read_smeter(), want, fails)

    print()
    if fails:
        for f in fails:
            print(f"  FAIL: {f}")
        sys.exit(1)
    print("  TB framing OK")


if __name__ == "__main__":
    main()
