#!/usr/bin/env python3
"""Parsing that no radio can be in the loop for. The only test here that
runs without one.

Covers the `TB` decoded-text path and the `IF` length guard. They share a
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

    def ask_text(self, timeout=0.6):
        return self.reply

    def ask(self, cmd, timeout=0.6):
        return self.asks.get(cmd.rstrip(";"))


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

    print("\n=== an S-meter count is range-checked before it is a signal ===")
    # Both curves are unbounded above -- SMH 999 converts to +853 dBm -- and
    # the UI clamps its bar at S9+60, so ANY over-range count paints the
    # same full-scale meter as a real S9+60 signal. Nothing downstream can
    # tell one from the other, which is why it is caught here: a pinned
    # meter is over in a fifth of a second and leaves nothing behind.
    for label, asks, want in [
        ("SMH at S1",            {"SMH": "SMH005;"}, -121),
        ("SMH at S9",            {"SMH": "SMH040;"},  -73),
        ("SMH at S9+60",         {"SMH": "SMH100;"},  -13),
        ("SMH at full scale",    {"SMH": "SMH140;"},   27),
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

    print()
    if fails:
        for f in fails:
            print(f"  FAIL: {f}")
        sys.exit(1)
    print("  TB framing OK")


if __name__ == "__main__":
    main()
