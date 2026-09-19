"""TCI protocol layer: TCI text commands <-> K3 CAT.

Mapping and ordering come from k3-tci-command-map.md. The parts that are
easy to get subtly wrong, and are therefore spelled out here:

  * cwl -> MD3 and cwu -> MD7. Plain "CW" on this radio is the LOWER
    sideband -- measured against WWV, not assumed. Do not "fix" this.
  * DT is set BEFORE MD: norm/reverse is stored per sub-mode pair, so
    setting DT can move MD between 6 and 9.
  * Every SET that matters is verified by reading back, and what gets
    broadcast is the value the radio ACCEPTED, never the value requested.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from urllib.parse import quote

log = logging.getLogger("tci")

DEVICE = "Elecraft K3"
PROTOCOL = "ExpertSDR3,1.5"

# Advertise only what the radio can actually honour. DSB and SAM are
# omitted: no settable K3 equivalent, and a client control that does nothing
# is worse than one that is greyed out.
MODULATIONS = ["lsb", "usb", "cwl", "cwu", "nfm", "am", "digl", "digu"]

# TCI name -> (data sub-mode or None, mode command)
TCI_TO_K3 = {
    "lsb": (None, "MD1"), "usb": (None, "MD2"),
    "cwl": (None, "MD3"), "cwu": (None, "MD7"),
    "nfm": (None, "MD4"), "am": (None, "MD5"),
    "digu": ("DT0", "MD6"), "digl": ("DT0", "MD9"),
}
K3_TO_TCI = {"1": "lsb", "2": "usb", "3": "cwl", "7": "cwu",
             "4": "nfm", "5": "am", "6": "digu", "9": "digl"}
# Aliases: accept what other TCI servers emit, even though we advertise ours.
MOD_ALIASES = {"cw": "cwu", "cwr": "cwl", "fm": "nfm",
               "rtty": "digl", "dig": "digu"}

VFO_LIMITS = (100_000, 54_000_000)   # KSYN3A extends the low end
IF_LIMITS = (-9999, 9999)            # tied to the RIT/XIT offset range
PTT_WATCHDOG_S = 90.0

# Every band the K3 has: TCI-facing name (metres) -> (BN number, low edge,
# high edge, default). Edges are the US amateur allocations; the default is
# where a band button lands when the band's memory turns out to be off-band.
#
# WHY THE DEFAULT EXISTS. The K3 keeps one memory per band -- whatever the
# VFO was on when the band was last left -- and files a general-coverage
# frequency under the band whose range it falls in. So a mistyped 8.050
# becomes the 40 m memory, and every later trip to 40 m, from the radio's
# own BAND key too, lands on 8.050. The band command recalls the memory
# first, so a sane last-used spot is kept, and overwrites it with the
# default only when it is outside the band -- which also repairs the memory
# for the operator at the front panel.
#
# The web UI's band buttons -- one per entry -- and its MHz-digit band
# stepping are both driven from this table (see init_burst's band_plan), so it lives only here.
BANDS = {
    "160": (0,  1_800_000,  2_000_000,  1_830_000),
    "80":  (1,  3_500_000,  4_000_000,  3_550_000),
    # 60 m is channelised in the US and CW must sit on a channel centre:
    # channel 1 (5330.5 kHz USB dial) is centred on 5332.0.
    "60":  (2,  5_330_500,  5_406_500,  5_332_000),
    "40":  (3,  7_000_000,  7_300_000,  7_050_000),
    "30":  (4, 10_100_000, 10_150_000, 10_120_000),
    "20":  (5, 14_000_000, 14_350_000, 14_050_000),
    "17":  (6, 18_068_000, 18_168_000, 18_080_000),
    "15":  (7, 21_000_000, 21_450_000, 21_050_000),
    "12":  (8, 24_890_000, 24_990_000, 24_900_000),
    "10":  (9, 28_000_000, 29_700_000, 28_050_000),
    "6":   (10, 50_000_000, 54_000_000, 50_100_000),
}
# The band number takes up to 500 ms to settle, during which the radio
# defers every command; the command map asks for at least 300 ms after BN.
BAND_SETTLE_S = 0.5


def bool_str(v: bool) -> str:
    return "true" if v else "false"


def cw_seconds(text: str, wpm: int) -> float:
    """A generous upper bound on how long `text` takes to send.

    PARIS timing: 50 dot units per five-character word, so ten units per
    character, and a dot is 1.2/wpm seconds -- 12 * chars / wpm. Doubled
    with ten seconds on top, because this bounds a watchdog: too long only
    delays a backstop that should never fire, while too short cuts the
    operator off mid-word.

    NOT CAPPED. A ceiling of four minutes looked like prudence and was the
    opposite: 200 characters at 8 WPM is five minutes of sending, so the cap
    would have expired mid-message and forced the operator off the air --
    the watchdog doing exactly the damage it exists to prevent. The estimate
    is derived from the length and the speed, so it is already as large as
    the message requires and no larger.
    """
    return 2 * (12 * len(text) / max(1, wpm)) + 10.0


def ro_command(hz: int) -> str:
    """RIT/XIT offset -> `RO<sign><4 digits>`.

    One register, shared by RIT and XIT (see `_cmd_rit_offset`). The sign
    character is independent of the magnitude -- the radio itself emits
    `RO-0000` -- so it is written unconditionally rather than omitted at zero.
    Clamped to the +/-9999 the register holds and that `if_limits` advertises;
    the K3 would clamp anyway and echo back a value nobody asked for.
    """
    hz = max(-9999, min(9999, int(hz)))
    return f"RO{'-' if hz < 0 else '+'}{abs(hz):04d}"


# Everything printable that is not framing. `quote` escapes the rest.
_SAFE = "".join(c for c in map(chr, range(0x20, 0x7F)) if c not in "%,;")


def tci_escape(text: str) -> str:
    """Percent-encode anything that would break TCI framing.

    A TCI message is `;`-terminated with `,`-separated arguments, so neither
    character can appear raw inside one -- and decoded text may contain both
    (semicolons are legal in RTTY and PSK; commas turn up everywhere). The
    web UI splits incoming data on `;` before it parses anything, so a raw
    one there does not merely garble a field, it invents a second message.

    `%` goes too, so the encoding round-trips, and so does everything
    outside printable ASCII -- RTTY can deliver control characters, and the
    CAT reader turns any byte it cannot read as ASCII into U+FFFD. The
    result is ordinary UTF-8 percent-encoding, which a client decodes with
    `decodeURIComponent` and nothing bespoke.

    Encoding UTF-8 rather than one byte per character is the whole reason
    this uses `quote`: `%DC` for U+00DC is not valid percent-encoding, and
    `decodeURIComponent` does not return it -- it throws.
    """
    return quote(text, safe=_SAFE)


class RadioState:
    """Cache of everything the bridge advertises, so clients can be answered
    without hitting the serial port for every query."""

    def __init__(self):
        self.vfo_a = 0
        self.vfo_b = 0
        self.mode = "usb"
        self.split = False
        self.rit_on = False
        self.xit_on = False
        self.rit_offset = 0
        self.transmitting = False
        # Passband edges in Hz relative to the carrier, as TCI states them.
        self.filter_lo = -1500
        self.filter_hi = 1500
        # TCI master volume, in dB on the wire (-60..0). Applied in software
        # to the audio frames -- see audio.rx_frame.
        self.volume_db = 0
        self.muted = False
        self.mon_level = 0
        self.tm_mode = 0            # 0 = RF power on the bargraph, 1 = ALC
        self.agc = "slow"           # TCI agc_mode: the K3 has fast and slow
        self.preamp = False
        self.att = False            # a single 10 dB pad on this K3: RA00/RA01
        self.nb = False
        self.nb_levels = (0, 0)     # NL: DSP blanker, IF blanker, 00-21 each
        self.nr = False             # from DS only -- see refresh_display
        self.sql_level = 0          # TCI 0-100; kept while squelch is open
        self.sql_on = False         # SQ000 is open: there is no on/off
        self.lock = False           # VFO A lock
        self.notch = "off"          # off / auto / manual, likewise


class Bridge:
    """Owns the radio state and translates TCI <-> CAT."""

    def __init__(self, cat, ptt_line: bool = False):
        self.cat = cat
        # Key with RTS rather than `TX;`. Off by default, because it takes a
        # radio configured for it -- the K3's RS232 menu has to read RTS=PTT
        # -- and asserting a line at a radio set to OFF keys nothing at all.
        #
        # WHY IT IS WORTH HAVING: it fails safe. A line is held up by the
        # process, so the process dying drops it and the radio unkeys, where
        # `TX;` needs something still alive to send `RX;`. The watchdog
        # covers a client that vanishes; nothing in CAT covers the bridge
        # itself vanishing.
        #
        # NOT USED FOR CW. The unkey at the end of a keyed message has to
        # wait for the message to finish, and only a CAT command can do that
        # -- `KYW` defers following commands until the text has been sent.
        # A line drops the instant it is told to, which would cut the
        # message off. So CW keeps its `TX;` … `RX;` bracket either way.
        self.ptt_line = ptt_line
        self.state = RadioState()
        self.broadcast = None      # set by the server: callable(str)
        self._ptt_deadline: float | None = None
        # Which client currently holds PTT. If that client vanishes we
        # unkey immediately rather than waiting out the watchdog -- other
        # clients being connected is no reason to keep transmitting.
        self._ptt_owner = None
        # Whether the keying client intends to send TX audio. WSJT-X and
        # friends wait to be asked (TX_CHRONO) rather than streaming freely,
        # so the server needs to know whether to start that clock.
        self.ptt_wants_audio = False
        self._current_client = None
        self._lock = threading.RLock()
        # CW sending runs on its own thread: see _cw_send. The queue is
        # chunks not yet written to the radio, and the stop button empties
        # it -- that, rather than anything CAT can say to a radio already
        # holding text, is what a stop actually is here.
        self._cw_lock = threading.Lock()
        self._cw_queue: list[str] = []
        self._cw_abort = threading.Event()
        self._cw_thread: threading.Thread | None = None
        self._cw_wpm_now = 20
        self._cw_keyed = False
        self._keyed_at = 0.0

    @property
    def ptt_owner(self):
        """The client currently holding PTT, or None."""
        return self._ptt_owner

    # ---------- startup ----------

    def prime(self) -> None:
        """Initial state sweep. Runs once, before any client is served."""
        self.refresh_vfo()
        self.refresh_mode()
        self.refresh_if()
        self.refresh_filter()
        self.refresh_tm()
        self.refresh_agc()
        self.refresh_rx_frontend()
        self.refresh_display()
        self.refresh_sql_lock()
        log.info("primed: A=%d B=%d mode=%s split=%s",
                 self.state.vfo_a, self.state.vfo_b,
                 self.state.mode, self.state.split)

    def refresh_vfo(self) -> None:
        for cmd, attr in (("FA", "vfo_a"), ("FB", "vfo_b")):
            r = self.cat.ask(cmd)
            if r and r.startswith(cmd) and len(r) >= 14:
                try:
                    setattr(self.state, attr, int(r[2:13]))
                except ValueError:
                    pass

    def refresh_mode(self) -> None:
        md = self.cat.ask("MD")
        if md and md.startswith("MD") and len(md) >= 4:
            self.state.mode = K3_TO_TCI.get(md[2], self.state.mode)

    def refresh_filter(self) -> None:
        """BW + IS -> TCI carrier-relative passband edges.

        BW is a width in 10 Hz units; IS is an ABSOLUTE AF centre frequency.
        Converting between them and TCI's carrier-relative pair is
        mode-dependent, because where the carrier sits inside the AF
        passband differs by mode.
        """
        bw = self.cat.ask("BW")
        is_ = self.cat.ask("IS")
        width = None
        if bw and bw.startswith("BW") and len(bw) >= 7:
            try:
                width = int(bw[2:6]) * 10
            except ValueError:
                pass
        if width is None:
            return
        centre = None
        if is_ and is_.startswith("IS ") and len(is_) >= 8:
            try:
                centre = int(is_[3:7])
            except ValueError:
                pass
        half = width // 2
        mode = self.state.mode
        if mode in ("usb", "digu") and centre is not None:
            lo, hi = centre - half, centre + half
        elif mode in ("lsb", "digl") and centre is not None:
            # LSB audio maps to NEGATIVE offsets from the carrier.
            lo, hi = -(centre + half), -(centre - half)
        else:
            # CW, AM, FM: the carrier sits at the centre of the passband
            # (in CW that centre is the sidetone PITCH), so the TCI band is
            # symmetric regardless of what IS reads.
            lo, hi = -half, half
        self.state.filter_lo, self.state.filter_hi = lo, hi

    # Highest fixed offset this parser reads (`split`, at index 32). The
    # length guard below is expressed in terms of it rather than the overall
    # response length -- see refresh_if.
    IF_LAST_FIELD = 32

    def refresh_if(self) -> None:
        """One read gives TX state, mode, split and RIT/XIT, each at a fixed
        offset.

        LENGTH: the reference sample in the command map is 38 characters and
        ends `...0003000011 ;` -- a space before the terminator. This radio
        (K3, RVM05.67) returns 37 and ends `...0003000001;` with no space, so
        a `len(r) < 38` guard rejected EVERY reply and this function returned
        without ever updating anything. Nothing looked broken from outside:
        frequency and mode track through the AI2 unsolicited stream instead,
        so only the fields that have no AI2 path -- split, and now RIT/XIT --
        were silently frozen at whatever they were at startup.

        So the guard asks the question that actually matters: is the response
        long enough to contain the fields being read? Every field sits at
        index 32 or below, which both the 37- and 38-character forms satisfy.
        """
        # Deferred rather than lost while the radio is keying a message --
        # see K3Cat.ask's `quiet`. The caller already treats no reply as
        # "state left stale", which is the right answer during transmit.
        r = self.cat.ask("IF", timeout=0.8, quiet=self.state.transmitting)
        if not r or not r.startswith("IF") or len(r) <= self.IF_LAST_FIELD:
            # SAY SO. This returned silently, which made every caller's
            # read-back indistinguishable from a confirmed one: the cached
            # state is re-broadcast as though the radio had accepted the SET.
            # `?;` from a busy radio (global rule 3) lands here.
            log.debug("refresh_if: no usable IF reply (%r) -- state left stale", r)
            return
        s = self.state
        try:
            s.vfo_a = int(r[2:13])
            sign = -1 if r[18] == "-" else 1
            s.rit_offset = sign * int(r[19:23])
            s.rit_on = r[23] == "1"
            s.xit_on = r[24] == "1"
            s.transmitting = r[28] == "1"
            s.mode = K3_TO_TCI.get(r[29], s.mode)
            s.split = r[32] == "1"
            self._clear_deadline_if_receiving()
        except (ValueError, IndexError):
            log.warning("could not parse IF: %r", r)

    # ---------- init burst ----------

    def init_burst(self) -> list[str]:
        """Settings first, then 'ready', then 'start' -- some clients latch
        cached settings on READY, so nothing may follow it."""
        s = self.state
        out = [
            f"protocol:{PROTOCOL}",
            f"device:{DEVICE}",
            "receive_only:false",
            "trx_count:1",          # no KRX3A: there is no second receiver
            "channels_count:2",     # VFO A = channel 0, VFO B = channel 1
            f"vfo_limits:{VFO_LIMITS[0]},{VFO_LIMITS[1]}",
            f"if_limits:{IF_LIMITS[0]},{IF_LIMITS[1]}",
            f"modulations_list:{','.join(MODULATIONS)}",
            "iq_samplerate:48000",
            "audio_samplerate:48000",
            "audio_stream_sample_type:float32",
            "audio_stream_channels:2",
            "audio_stream_samples:2048",
            f"vfo:0,0,{s.vfo_a}",
            f"vfo:0,1,{s.vfo_b}",
            f"modulation:0,{s.mode}",
            f"rx_filter_band:0,{s.filter_lo},{s.filter_hi}",
            f"rx_enable:0,true",
            f"split_enable:0,{bool_str(s.split)}",
            f"rit_enable:0,{bool_str(s.rit_on)}",
            f"xit_enable:0,{bool_str(s.xit_on)}",
            f"rit_offset:0,{s.rit_offset}",
            # Both, from the one shared RO register. Without this a client
            # that models the two offsets separately starts with its XIT
            # readout at 0 while the radio is shifted -- and every SET after
            # that reports both, so only the initial state was ever wrong.
            f"xit_offset:0,{s.rit_offset}",
            f"trx:0,{bool_str(s.transmitting)}",
            f"drive:0,{self._read_pc()}",
            f"mic_level:{self._read_mic()}",
            f"mon_volume:{self._read_mon()}",
            f"volume:{s.volume_db}",
            f"mute:0,{bool_str(s.muted)}",
            f"agc_mode:0,{s.agc}",
            *self.rx_frontend_notifications(),
            *self.display_notifications(),
            *self.sql_lock_notifications(),
            # The bridge's own message, like rx_text; other TCI clients
            # ignore what they do not know. name/low/high per band.
            "band_plan:" + ",".join(f"{n}/{lo}/{hi}" for n, (_, lo, hi, _)
                                    in BANDS.items()),
            "ready",
            "start",
        ]
        return out

    # ---------- command handling ----------

    def handle(self, line: str, client=None) -> tuple[list[str], list[str]]:
        """Returns (reply_to_requester, broadcast_to_everyone)."""
        with self._lock:
            self._current_client = client
            return self._handle_locked(line)

    def _handle_locked(self, line: str) -> tuple[list[str], list[str]]:
        line = line.strip().rstrip(";").strip()
        if not line:
            return [], []
        name, _, argstr = line.partition(":")
        name = name.strip().lower()
        args = [a.strip() for a in argstr.split(",")] if argstr else []
        fn = getattr(self, f"_cmd_{name}", None)
        if fn is None:
            log.debug("unhandled TCI command: %s", line)
            return [], []
        try:
            return fn(args)
        except Exception:
            log.exception("error handling %s", line)
            return [], []

    # -- vfo --------------------------------------------------------------

    def _cmd_vfo(self, args):
        if len(args) >= 3:                      # SET
            try:
                trx, chan, hz = int(args[0]), int(args[1]), int(args[2])
            except ValueError:
                return [], []
            if trx != 0 or chan not in (0, 1) or hz < 0:
                return [], []
            cmd = "FA" if chan == 0 else "FB"
            # A frequency that crosses a band edge defers command handling
            # for up to 500 ms, so allow a longer read-back window.
            self.cat.send(f"{cmd}{hz:011d}")
            time.sleep(0.12)
            r = self.cat.ask(cmd, timeout=1.0)
            accepted = hz
            if r and r.startswith(cmd) and len(r) >= 14:
                try:
                    accepted = int(r[2:13])
                except ValueError:
                    pass
            if chan == 0:
                self.state.vfo_a = accepted
            else:
                self.state.vfo_b = accepted
            # Broadcast what the radio accepted, not what was asked for.
            # It is NOT snapped to an amateur band -- the K3 is general
            # coverage, and 8.050 reads back as 8.050 (measured; see
            # _cmd_band for why that matters).
            return [], [f"vfo:0,{chan},{accepted}"]
        # GET
        chan = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0
        hz = self.state.vfo_a if chan == 0 else self.state.vfo_b
        return [f"vfo:0,{chan},{hz}"], []

    # -- band (the bridge's own command) -----------------------------------

    def _read_fa(self, timeout: float = 1.0) -> int | None:
        r = self.cat.ask("FA", timeout=timeout)
        if r and r.startswith("FA") and len(r) >= 14:
            try:
                return int(r[2:13])
            except ValueError:
                pass
        return None

    def _cmd_band(self, args):
        """band:0,<metres> -- change band the way the radio's BAND key does,
        then repair the band's memory if it recalled somewhere off-band.

        Holds the command lock for 0.5-0.8 s -- the BN settle, plus the FA
        fix-up when one is needed -- so every client's commands, PTT
        included, wait behind it. A band press is a deliberate, occasional
        act; that is an acceptable price.
        """
        if len(args) < 2 or args[1] not in BANDS:
            return [], []
        if self.state.transmitting:
            # Changing band under key moves the transmitter into whatever
            # the antenna and tuner are not set up for.
            log.warning("band change to %sm refused while transmitting", args[1])
            return [], []
        bn, lo, hi, default = BANDS[args[1]]
        self.cat.send(f"BN{bn:02d}")
        time.sleep(BAND_SETTLE_S)
        hz = self._read_fa()
        if hz is None or not lo <= hz <= hi:
            log.info("%sm memory recalled %s -- off-band, setting %d",
                     args[1], hz, default)
            self.cat.send(f"FA{default:011d}")
            time.sleep(0.12)
            hz = self._read_fa()
        if hz is not None:
            self.state.vfo_a = hz
        # The band memory carries its own mode, and filters are per mode,
        # so both may have moved -- same as _cmd_modulation.
        self.refresh_mode()
        self.refresh_filter()
        s = self.state
        return [], [f"vfo:0,0,{s.vfo_a}", f"modulation:0,{s.mode}",
                    f"rx_filter_band:0,{s.filter_lo},{s.filter_hi}"]

    # -- modulation -------------------------------------------------------

    def _cmd_modulation(self, args):
        if len(args) >= 2:                      # SET
            want = MOD_ALIASES.get(args[1].lower(), args[1].lower())
            if want not in TCI_TO_K3:
                log.info("unsupported modulation %r ignored", args[1])
                return [], []
            dt, md = TCI_TO_K3[want]
            # DT first: setting it can move MD between 6 and 9.
            if dt:
                self.cat.set_verified(dt, "DT", dt + ";")
            self.cat.set_verified(md, "MD", md + ";")
            self.refresh_mode()
            # Entering a digital mode: silence the transmit monitor.
            #
            # MIC+LIN must stay ON -- it is the enable for LINE IN, not a
            # "sum the mic in" switch; with it OFF nothing reaches the
            # modulator at all (measured). So the microphone is unavoidably
            # live in the TX path, and if the monitor is up it feeds the
            # speaker, which feeds the mic, which feeds the transmitter.
            # That howls. Dropping the monitor breaks the loop and costs
            # nothing in a mode nobody listens to themselves in.
            if self.state.mode in ("digu", "digl") and self._read_mon() > 0:
                log.info("digital mode: muting the TX monitor to prevent "
                         "acoustic feedback through the mic")
                self.cat.set_verified("ML000", "ML", "ML000;")
            # Filters are stored per mode, so the passband just changed too.
            self.refresh_filter()
            s = self.state
            return [], [f"modulation:0,{s.mode}",
                        f"rx_filter_band:0,{s.filter_lo},{s.filter_hi}"]
        return [f"modulation:0,{self.state.mode}"], []

    # -- trx (PTT) --------------------------------------------------------

    def _await_tq(self, want: bool, timeout: float = 1.5) -> bool:
        """Poll TQ until the radio reaches the requested state.

        A single read 100 ms after the command is not enough: the T/R
        transition takes longer than that, and reading too early reports the
        OLD state, which then sticks in the cache.
        """
        target = "TQ1;" if want else "TQ0;"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.cat.ask("TQ") == target:
                return True
            time.sleep(0.08)
        return False

    def _key_on(self) -> bool:
        """Key the transmitter, and confirm the radio agrees that it is.

        In line mode the fallback matters more than the line does: a radio
        whose RS232 menu is not set to PTT ignores RTS completely, and the
        failure is silent -- the bridge would report transmitting, the
        client would send audio, and nothing would go out. So an unconfirmed
        line drops back to `TX;` rather than being trusted.
        """
        if self.ptt_line:
            self.cat.set_ptt_line(True)
            if self._await_tq(True):
                self._keyed_at = time.monotonic()
                return True
            log.warning("RTS did not key the radio -- is its RS232 menu set "
                        "to RTS=PTT? falling back to TX;")
            self.cat.set_ptt_line(False)
        self.cat.send("TX")
        ok = self._await_tq(True)
        if ok:
            self._keyed_at = time.monotonic()
        return ok

    def _key_off(self) -> None:
        """Unkey by BOTH routes, every time, whatever keyed it.

        Dropping an unused line costs nothing and `RX;` into a radio already
        receiving costs nothing, while getting this wrong costs a transmitter
        left running. There is no state worth consulting here.
        """
        self.cat.set_ptt_line(False)
        self.cat.send("RX")

    def _cmd_trx(self, args):
        if len(args) >= 2:                      # SET
            # Strict bool: only literal true/false key the transmitter.
            if args[1] not in ("true", "false"):
                return [], []
            want = args[1] == "true"
            if want:
                # TX; is IGNORED in FSK-D and PSK-D -- it would silently do
                # nothing. Refuse rather than pretend we keyed.
                md = self.cat.ask("MD")
                if md in ("MD6;", "MD9;"):
                    dt = self.cat.ask("DT")
                    if dt in ("DT2;", "DT3;"):
                        log.warning("refusing PTT: %s is FSK-D/PSK-D", dt)
                        return ["trx:0,false"], []
                # Arm the watchdog BEFORE keying, so a failure between the
                # send and the confirm is still covered.
                # TCI's optional third arg names the audio source. "dax" or
                # "tci" explicitly request TX audio; with no source, infer it
                # from the mode, since the digital modes are only ever keyed
                # by something that intends to modulate.
                src = args[2].lower() if len(args) > 2 else ""
                self.ptt_wants_audio = (
                    src in ("dax", "tci")
                    or (src == "" and self.state.mode in ("digu", "digl")))
                self._ptt_deadline = time.monotonic() + PTT_WATCHDOG_S
                self._ptt_owner = self._current_client
                ok = self._key_on()
                if not ok:
                    log.warning("the radio did not key")
                    self._ptt_deadline = None
                    self._ptt_owner = None
                    self.ptt_wants_audio = False
                self.state.transmitting = ok
            else:
                # An unkey from a client that does not hold PTT is allowed
                # -- it is a useful emergency stop from anywhere -- but it
                # is worth shouting about, because it is also how one
                # client accidentally cuts another's transmission short.
                # A web UI once did exactly that on a stray mouse-leave,
                # and finding it meant reading peer port numbers.
                if (self._ptt_owner is not None
                        and self._current_client is not None
                        and self._current_client is not self._ptt_owner):
                    log.warning("unkey from a client that does NOT hold PTT "
                                "-- cutting short another client's "
                                "transmission")
                self._key_off()
                if not self._await_tq(False):
                    # Retry once, then leave the watchdog ARMED. Clearing the
                    # deadline on an unconfirmed unkey would disable the only
                    # thing that can rescue a stuck transmitter.
                    log.warning("unkey unconfirmed -- retrying")
                    self._key_off()
                    if not self._await_tq(False, timeout=2.0):
                        log.error("RADIO STILL KEYED after two RX; commands "
                                  "-- leaving watchdog armed")
                        self.state.transmitting = True
                        return [], ["trx:0,true"]
                self._ptt_deadline = None
                self._ptt_owner = None
                self.ptt_wants_audio = False
                self.state.transmitting = False
            return [], [f"trx:0,{bool_str(self.state.transmitting)}"]
        return [f"trx:0,{bool_str(self.state.transmitting)}"], []

    # -- split ------------------------------------------------------------

    def _cmd_split_enable(self, args):
        # WSJT-X sends "split_enable:false" with NO trx index. Requiring
        # two arguments silently ignored it, so the radio kept whatever
        # split state it had while the client believed it had cleared it.
        if len(args) == 1 and args[0] in ("true", "false"):
            args = ["0", args[0]]
        if len(args) >= 2 and args[1] in ("true", "false"):
            # FT1 enables split (TX on B); FR0 is the documented cancel.
            self.cat.send("FT1" if args[1] == "true" else "FR0")
            time.sleep(0.15)
            self.refresh_if()
            return [], [f"split_enable:0,{bool_str(self.state.split)}"]
        return [f"split_enable:0,{bool_str(self.state.split)}"], []

    # -- RIT / XIT --------------------------------------------------------
    #
    # These were read-only until now: `refresh_if` has always parsed the
    # enables and the offset out of the `IF` response and `state_messages`
    # has always broadcast them, but nothing could SET them, so a client's
    # RIT knob moved its own display and nothing else.
    #
    # Every one of them re-reads `IF` and broadcasts what the radio ACCEPTED
    # rather than what was asked for (global rule 2). That matters more here
    # than elsewhere: `RT`/`XT` are documented as disabled in QRQ CW mode, so
    # a SET that is simply ignored is a normal outcome, not an error.
    #
    # WHEN THE READ-BACK DOES NOT HAPPEN, `refresh_if` returns silently and
    # leaves the cached state alone -- a `?;` from a busy or transmitting
    # radio (global rule 3) does that. The broadcast is then the PRE-SET
    # value, which is the least-wrong answer available: it tells clients what
    # the radio last actually reported instead of confirming a change that
    # may not have happened.

    def _cmd_rit_enable(self, args):
        if len(args) >= 2 and args[1] in ("true", "false"):
            self.cat.send("RT1" if args[1] == "true" else "RT0")
            time.sleep(0.15)
            self.refresh_if()
            return [], [f"rit_enable:0,{bool_str(self.state.rit_on)}"]
        return [f"rit_enable:0,{bool_str(self.state.rit_on)}"], []

    def _cmd_xit_enable(self, args):
        if len(args) >= 2 and args[1] in ("true", "false"):
            self.cat.send("XT1" if args[1] == "true" else "XT0")
            time.sleep(0.15)
            self.refresh_if()
            return [], [f"xit_enable:0,{bool_str(self.state.xit_on)}"]
        return [f"xit_enable:0,{bool_str(self.state.xit_on)}"], []

    def _offset_notifications(self) -> list[str]:
        """Both offsets, always, because the K3 has only one register.

        TCI models RIT and XIT as independent values; `RO` is shared. So a
        client that sets one and is told only about that one would show the
        other as unchanged when the radio had in fact moved it. Echoing both
        keeps every client's two readouts agreeing with the single register
        they actually describe.
        """
        return [f"rit_offset:0,{self.state.rit_offset}",
                f"xit_offset:0,{self.state.rit_offset}"]

    def _set_offset(self, args):
        if len(args) >= 2 and args[1] != "":
            try:
                hz = int(float(args[1]))
            except ValueError:
                return [], []
            self.cat.send(ro_command(hz))
            time.sleep(0.15)
            self.refresh_if()
            return [], self._offset_notifications()
        return [f"rit_offset:0,{self.state.rit_offset}"], []

    def _cmd_rit_offset(self, args):
        return self._set_offset(args)

    def _cmd_xit_offset(self, args):
        # Same register as RIT -- see _offset_notifications. An operator who
        # sets the two differently will see them snap together, which is the
        # radio being honest rather than the bridge losing a value.
        return self._set_offset(args)

    def _cmd_if(self, args):
        """TCI's combined RIT verb: `if:<trx>,<channel>,<offset>`.

        A non-zero offset implies RIT on, and zero implies RIT off -- clients
        clear the offset by sending 0, and leaving the radio enabled at zero
        offset would leave RIT lit on the front panel with nothing to show
        for it.
        """
        if len(args) >= 3 and args[2] != "":
            try:
                hz = int(float(args[2]))
            except ValueError:
                return [], []
            self.cat.send(ro_command(hz))
            self.cat.send("RT1" if hz != 0 else "RT0")
            time.sleep(0.15)
            self.refresh_if()
            return [], (self._offset_notifications()
                        + [f"rit_enable:0,{bool_str(self.state.rit_on)}"])
        return [f"if:0,0,{self.state.rit_offset}"], []

    # -- misc -------------------------------------------------------------

    def _cmd_start(self, args):
        return [], []

    def _cmd_stop(self, args):
        return [], []

    def _cmd_rx_enable(self, args):
        return ["rx_enable:0,true"], []

    def _cmd_tx_enable(self, args):
        # TCI defines TX_ENABLE as server->client state only.
        return [], []

    def _cmd_vfo_limits(self, args):
        return [f"vfo_limits:{VFO_LIMITS[0]},{VFO_LIMITS[1]}"], []

    def _cmd_if_limits(self, args):
        return [f"if_limits:{IF_LIMITS[0]},{IF_LIMITS[1]}"], []

    # -- transmit monitor -------------------------------------------------

    # ML is 000-060 and applies to the CURRENT mode -- CW sidetone, voice or
    # data are stored separately, so a level set in CW does not carry into
    # SSB. That is the radio's behaviour, not something to paper over.
    ML_MAX = 60

    def _cmd_mon_volume(self, args):
        """TCI mon_volume 0-100 -> ML. Other TCI servers put the value in
        args[0] for this command rather than args[1], so accept either."""
        if args and args[0] != "":
            try:
                pct = int(float(args[0] if len(args) == 1 else args[-1]))
            except ValueError:
                return [], []
            pct = max(0, min(100, pct))
            ml = round(pct * self.ML_MAX / 100)
            self.cat.set_verified(f"ML{ml:03d}", "ML", f"ML{ml:03d};")
            self.state.mon_level = pct
            return [], [f"mon_volume:{pct}"]
        return [f"mon_volume:{self._read_mon()}"], []

    def _read_mon(self) -> int:
        r = self.cat.ask("ML")
        if r and r.startswith("ML") and len(r) >= 6:
            try:
                return round(int(r[2:5]) * 100 / self.ML_MAX)
            except ValueError:
                pass
        return 0

    def _cmd_mon_enable(self, args):
        """No on/off command exists -- ML000 is off. Cache the level so it
        can be restored rather than lost."""
        if len(args) >= 2 or (args and args[0] in ("true", "false")):
            on = ("true" in [a.lower() for a in args])
            if on:
                lvl = self.state.mon_level or 30
                self._cmd_mon_volume([str(lvl)])
            else:
                self.state.mon_level = self._read_mon()
                self.cat.set_verified("ML000", "ML", "ML000;")
            return [], [f"mon_enable:{bool_str(on)}"]
        return [f"mon_enable:{bool_str(self._read_mon() > 0)}"], []

    # -- mic gain ---------------------------------------------------------

    # The K3 accepts MG000-060, but above roughly MG034 the mic-path noise
    # floor alone opens the ALC -- measured on the bench. On a remote station
    # that means transmitting shack noise between overs, so the usable range
    # is capped well below what the command allows. Full drive is available
    # by MG005 with the codec mixer at 100%, so nothing is lost.
    MG_MAX = 30

    def _cmd_mic_level(self, args):
        """TCI mic_level 0-100 -> MG, capped. Global: no trx field in the
        spec form, though a legacy trx-prefixed form exists, so take the
        value from the last argument either way."""
        if args and args[0] != "":
            try:
                pct = int(float(args[-1]))
            except ValueError:
                return [], []
            pct = max(0, min(100, pct))
            mg = round(pct * self.MG_MAX / 100)
            self.cat.set_verified(f"MG{mg:03d}", "MG", f"MG{mg:03d};")
            return [], [f"mic_level:{pct}"]
        r = self.cat.ask("MG")
        mg = int(r[2:5]) if r and r.startswith("MG") and len(r) >= 6 else 0
        return [f"mic_level:{round(mg * 100 / self.MG_MAX)}"], []

    def _cmd_tx_gain(self, args):
        return self._cmd_mic_level(args)

    # -- power / tune -----------------------------------------------------

    def _cmd_drive(self, args):
        """TCI drive 0-100 -> PC watts, 1:1.

        Deliberately not scaled to the KPA3A's 110 W ceiling: a client's
        "100%" should mean 100 W, not 110. Power is capped per band and on
        transverter bands, so read back what the radio accepted.
        """
        if len(args) >= 2 and args[1] != "":
            try:
                pct = int(float(args[1]))
            except ValueError:
                return [], []
            pct = max(0, min(100, pct))
            self.cat.send(f"PC{pct:03d}")
            time.sleep(0.15)
            return [], [f"drive:0,{self._read_pc(pct)}"]
        return [f"drive:0,{self._read_pc()}"], []

    def _read_mic(self) -> int:
        r = self.cat.ask("MG")
        if r and r.startswith("MG") and len(r) >= 6:
            try:
                return round(int(r[2:5]) * 100 / self.MG_MAX)
            except ValueError:
                pass
        return 0

    def _read_pc(self, fallback: int = 0) -> int:
        r = self.cat.ask("PC")
        if r and r.startswith("PC") and len(r) >= 6:
            try:
                return int(r[2:5])
            except ValueError:
                pass
        return fallback

    def _cmd_tune_drive(self, args):
        return self._cmd_drive(args)

    # Power settings at which TUNE starts the internal calibration instead.
    TUNE_BAD_W = (5, 50)

    def _cmd_tune(self, args):
        """TUNE = hold XMIT (SWH16). Emits a carrier at the current power,
        which is how the ATU is asked to tune."""
        if len(args) >= 2:
            on = args[1].lower() == "true"
            # Exactly 5 W or 50 W makes TUNE start the K3's internal power
            # calibration rather than a carrier. Checked against the radio's
            # own PC, not the client's idea of it: a front-panel change or
            # another client may have moved it. The UI says why; any other
            # client just sees the tune not start.
            if on:
                pc = self._read_pc(-1)
                if pc in self.TUNE_BAD_W:
                    log.warning("TUNE refused at exactly %d W -- it starts "
                                "the K3's internal calibration", pc)
                    return [], [f"tune:0,{bool_str(self.state.transmitting)}"]
            self.cat.send("SWH16" if on else "RX")
            time.sleep(0.2)
            tq = self.cat.ask("TQ")
            # TUNE radiates, so it IS transmitting, and the state has to say
            # so. It did not: for the 2.5 s of an ATU tune the bridge
            # believed it was receiving, so the S-meter and decoded-text
            # loops -- both gated on this flag -- went on polling a radio
            # with the carrier up. That is the exact condition the reference
            # warns about and that dropped this radio out of TX on the
            # bench, and what the meter reads mid-carrier is not a signal.
            #
            # It also puts TUNE under the same safety net as PTT: force_rx
            # on the last client leaving tests this flag, so a client that
            # dies mid-tune no longer leaves the radio holding a carrier.
            self.state.transmitting = (tq == "TQ1;")
            return [], [f"tune:0,{bool_str(self.state.transmitting)}",
                        f"trx:0,{bool_str(self.state.transmitting)}"]
        return [f"tune:0,{bool_str(self.state.transmitting)}"], []

    # -- CW keying --------------------------------------------------------

    # KY takes at most 24 characters of text per command. The 'W' (wait)
    # form defers any following commands until the message has been sent,
    # which matters because a speed change queued behind a message must not
    # overtake it.
    CW_MAX = 24

    def _cw_chunks(self, text: str) -> list[str]:
        """Split text into KY payloads, each at most CW_MAX characters.

        Returns them ready to write, SPACES INCLUDED, so that joining the
        payloads reproduces the text exactly. Two bugs live in that seam,
        and carrying the space here is what closes both.

        THE SPACE IS PART OF THE BUDGET. A chunk that is followed by another
        carries the space that keeps the words apart, and appending it to a
        chunk of exactly CW_MAX made a 25-character payload -- one past the
        documented limit, with the radio left to decide what to do about the
        overrun. So a chunk that will carry a space is built one character
        shorter. Building EVERY chunk shorter would split a 24-character
        message that fits perfectly well in one command, so a text within
        the limit is always a single chunk.

        A SPLIT INSIDE A WORD CARRIES NO SPACE. A word too long to fit any
        chunk has to be cut somewhere, and the old code appended the
        separator there too -- putting a space in the middle of the word and
        sending it as two. The separator belongs at word boundaries only.
        """
        if len(text) <= self.CW_MAX:
            return [text] if text else []
        budget = self.CW_MAX - 1            # room for the trailing space
        out, cur = [], ""
        for word in text.split(" "):
            # A word longer than a whole chunk: cut it, and do not let a
            # separator into the cut.
            while len(word) > self.CW_MAX:
                if cur:
                    out.append(cur + " ")
                    cur = ""
                out.append(word[:self.CW_MAX])
                word = word[self.CW_MAX:]
            piece = (cur + " " + word) if cur else word
            if len(piece) <= budget:
                cur = piece
            elif cur:
                out.append(cur + " ")
                cur = word
            else:
                cur = word
        if cur:
            out.append(cur)
        return out

    def _cw_send(self, text: str) -> bool:
        """Key the transmitter and hand the text to the sending worker.

        RETURNS AS SOON AS THE TEXT IS QUEUED, which is the whole point. It
        used to write every chunk here, pacing each against the radio's
        buffer -- and a chunk is fourteen seconds of sending at 20 WPM, all
        of it spent inside one command handler. The connection handler reads
        a client's next message only when the previous one returns, so a
        client could not interrupt its OWN transmission: a stop sent five
        seconds into a message was not even read for another nine. Measured,
        not theorised.
        """
        if self.state.mode not in ("cwl", "cwu"):
            log.warning("cw_msg ignored: mode is %s, not CW", self.state.mode)
            return False

        # KEY THE TRANSMITTER, RATHER THAN LEAVING IT TO VOX. `TX;` puts it
        # up before any text goes out and `RX;` behind the last chunk brings
        # it down, which is what makes the T/R transition deterministic
        # instead of a side effect of the first character arriving. The `W`
        # form is what makes the trailing `RX;` correct: it defers following
        # commands until the message has been sent, so the unkey lands after
        # the last element rather than cutting it off.
        #
        # VOX stays as the fallback, not the plan. Without either, the K3
        # takes KY text into its buffer and never transmits -- no `?;`, no
        # error, nothing on the air -- so if `TX;` does not take, the old
        # path runs rather than sending text into silence.
        vx = self.cat.ask("VX")
        # The speed is read HERE, before any text is queued. Reading it
        # afterwards asks a radio that is already deferring commands until
        # the message has been sent, so it timed out every time and the
        # estimate silently fell back to its default.
        wpm = self._cw_wpm()
        text = text.replace("\n", " ")
        chunks = self._cw_chunks(text)
        if not chunks:
            return False

        with self._cw_lock:
            already_sending = bool(self._cw_queue)
            # New text cancels a stop: the operator asking for more is not
            # asking for the last request to stay cancelled.
            self._cw_abort.clear()
            self._cw_queue.extend(chunks)
            self._cw_wpm_now = wpm

        if not already_sending and not self.state.transmitting:
            keyed = False
            self.cat.send("TX")
            if self._await_tq(True):
                keyed = True
                self.state.transmitting = True
                self._ptt_owner = self._current_client
            else:
                log.warning("TX; did not take for CW -- falling back to VOX")
                if vx == "VX0;":
                    log.info("enabling CW VOX (VX1) -- required for KY "
                             "keying")
                    self.cat.set_verified("VX1", "VX", "VX1;")
            self._cw_keyed = keyed

        with self._cw_lock:
            if self._cw_thread is None or not self._cw_thread.is_alive():
                self._cw_thread = threading.Thread(
                    target=self._cw_worker, name="cw", daemon=True)
                self._cw_thread.start()
        return True

    def cw_stop(self) -> None:
        """Abandon whatever has not been written yet, and unkey.

        WHAT A STOP CAN AND CANNOT DO. Text already inside the radio is
        gone: `RX;` queues behind the `KY` buffer like every other command
        the `W` form defers, so it ends the transmission rather than
        interrupting it -- measured on the air, a stop three seconds into a
        nine-second message changed the finishing time by under a second.
        The deferral is not a bug to route around: it is the same mechanism
        that makes an ordinary message unkey at exactly the right moment.

        So a stop cuts at the next chunk boundary. On a long macro that is
        most of it -- 24 characters is the most the radio can be holding
        that we cannot take back.
        """
        with self._cw_lock:
            dropped = len(self._cw_queue)
            self._cw_queue.clear()
            self._cw_abort.set()
        if dropped:
            log.info("cw: stop -- %d chunk(s) abandoned unsent", dropped)
        self._key_off()
        # The `RX;` that stop just sent is deferred behind whatever the
        # radio is still playing, exactly like the one at the end of an
        # ordinary message -- so it can go missing the same way and needs
        # the same backstop. Sized to what the radio can still be holding:
        # a stop abandons everything not yet written, so that is bounded by
        # the last chunk or two rather than by the whole message.
        if self.state.transmitting:
            self._ptt_deadline = time.monotonic() + cw_seconds(
                "x" * (2 * self.CW_MAX), self._cw_wpm_now)

    def _cw_worker(self) -> None:
        """Write queued chunks, pacing each against the radio's buffer.

        NO BRIDGE LOCK. The long wait in here is for the radio to finish
        sending what it already has, and holding the lock that serialises
        every client's commands across that is what made the stop button
        useless. Each CAT transaction is atomic inside K3Cat, which is the
        same footing the S-meter and decoded-text polls already run on.
        """
        try:
            while True:
                with self._cw_lock:
                    if self._cw_abort.is_set() or not self._cw_queue:
                        break
                    chunk = self._cw_queue.pop(0)
                    wpm = self._cw_wpm_now
                    remaining = len(self._cw_queue)
                if not self._cw_wait_for_room(chunk, wpm):
                    break                      # stopped while waiting
                out = "KYW" + chunk      # the chunk already carries its space
                # DEBUG, not INFO. This was the instrumentation for the
                # dropped-first-character report: what the bridge actually
                # wrote, which nothing else records once a message is on the
                # air and gone. It answered that question -- the text left
                # here intact every time -- so it goes quiet rather than
                # away. If the fault returns, `-v` brings it straight back.
                log.debug("cw: write %r (%d left)", out, remaining)
                self.cat.send(out)
        except Exception:
            log.exception("CW worker failed")
        finally:
            self._cw_finish()

    def _cw_wait_for_room(self, chunk: str, wpm: int) -> bool:
        """Wait until the radio's buffer has room. False if stopped first.

        Only `KY0;` is room. Testing for `KY1;` instead reads every OTHER
        answer as a clear buffer -- including the ones that are not answers
        at all: a timeout, or the `?;` of a radio too busy to say. Those are
        exactly when the buffer is most likely to be full, and writing into
        it is how the middle of a message goes missing.

        An unanswered poll must not stop the message either. The `W` form
        defers following commands until what is already queued has been
        sent, so a poll can legitimately go unanswered for as long as the
        radio takes to key it -- which is why the bound comes from the same
        arithmetic the watchdog uses rather than from a flat number, and why
        an unknown answer eventually writes anyway and says so instead of
        discarding text the operator asked to send.
        """
        start = time.monotonic()
        deadline = start + cw_seconds(chunk, wpm)
        ky = self.cat.ask("KY", timeout=0.3, quiet=True)
        while ky != "KY0;" and time.monotonic() < deadline:
            if self._cw_abort.wait(0.1):
                return False
            ky = self.cat.ask("KY", timeout=0.3, quiet=True)
        if ky != "KY0;":
            log.warning("CW buffer not confirmed clear after %.1fs (last "
                        "answer %r) -- writing anyway",
                        time.monotonic() - start, ky)
        return not self._cw_abort.is_set()

    def _cw_finish(self) -> None:
        """End of the queue: unkey, and leave a backstop in case it is lost.

        A bare CAT send rather than `_key_off()`: being held behind the
        message is the entire point here, and a line drops the instant it is
        told to. A stop has already unkeyed by both routes, so it skips this.
        """
        if self._cw_abort.is_set() or not self._cw_keyed:
            return
        self.cat.send("RX")
        # If that `RX;` is lost the radio sits in transmit with nothing to
        # bring it back, so the watchdog is armed with an upper bound on
        # what can still be inside the radio. Generous on purpose: firing
        # early truncates the transmission it is meant to protect.
        self._ptt_deadline = time.monotonic() + cw_seconds(
            "x" * (2 * self.CW_MAX), self._cw_wpm_now)

    def _cw_wpm(self, default: int = 20) -> int:
        r = self.cat.ask("KS")
        if r and r.startswith("KS") and len(r) >= 6:
            try:
                return max(8, min(50, int(r[2:5])))
            except ValueError:
                pass
        return default

    def _cmd_cw_msg(self, args):
        # Args were split on commas, but commas are legal inside CW text,
        # so put them back.
        text = ",".join(args).strip()
        if not text:
            return [], []
        self._cw_send(text)
        # SAY THAT THE RADIO IS TRANSMITTING, because now it is: the message
        # is keyed by `TX;` rather than by VOX, and every loop that must not
        # poll a transmitting radio -- the S-meter, the decoded text -- is
        # gated on this state. It used to send CW with the state still
        # reading receive, so those loops polled straight through the
        # transmission. The matching `false` comes from the reconcile sweep
        # once the radio is back and answering.
        return [], [f"trx:0,{bool_str(self.state.transmitting)}"]

    def _cmd_cw_macros(self, args):
        return self._cmd_cw_msg(args)

    def _cmd_cw_macros_stop(self, args):
        self.cw_stop()
        return [], []

    def _cw_speed(self, args, name):
        # 1-arg-SET quirk, as in other TCI servers: the value is in args[0]
        # and a bare query has no args at all.
        if args and args[0] != "":
            try:
                wpm = int(float(args[0]))
            except ValueError:
                return [], []
            wpm = max(8, min(50, wpm))     # K3 range is 008-050
            self.cat.set_verified(f"KS{wpm:03d}", "KS", f"KS{wpm:03d};")
            return [], [f"{name}:{wpm}"]
        r = self.cat.ask("KS")
        wpm = int(r[2:5]) if r and r.startswith("KS") and len(r) >= 6 else 20
        return [f"{name}:{wpm}"], []

    def _cmd_cw_keyer_speed(self, args):
        return self._cw_speed(args, "cw_keyer_speed")

    def _cmd_cw_macros_speed(self, args):
        return self._cw_speed(args, "cw_macros_speed")

    def _cmd_rx_filter_band(self, args):
        if len(args) >= 3:                      # SET
            try:
                lo, hi = int(args[1]), int(args[2])
            except ValueError:
                return [], []
            if hi <= lo:
                return [], []
            bw = max(0, min(9999, round((hi - lo) / 10)))
            self.cat.send(f"BW{bw:04d}")
            time.sleep(0.12)

            # IS is an absolute AF centre. Only set it where the TCI band is
            # genuinely offset from the carrier -- i.e. SSB and the DATA
            # modes. In CW, AM and FM the band straddles the carrier, so
            # (lo+hi)/2 is ~0 and writing that to IS would drag the passband
            # to DC. In CW the nominal centre is the PITCH, which the client
            # knows nothing about.
            centre = abs(lo + hi) // 2
            if self.state.mode in ("usb", "lsb", "digu", "digl") and centre:
                # Note the literal space: the format is "IS*nnnn;".
                self.cat.send(f"IS {min(9999, centre):04d}")
                time.sleep(0.12)

            # BW is quantised hard by the installed filters, so report back
            # what the radio accepted, never what was asked for.
            self.refresh_filter()
            s = self.state
            return [], [f"rx_filter_band:0,{s.filter_lo},{s.filter_hi}"]
        s = self.state
        return [f"rx_filter_band:0,{s.filter_lo},{s.filter_hi}"], []

    def _cmd_rx_sensors_enable(self, args):
        """WSJT-X sends "rx_sensors_enable:false,500" on connect. We do not
        stream rx_channel_sensors, so just acknowledge -- an unanswered
        command can leave a client waiting."""
        on = bool(args) and args[0].lower() == "true"
        return [f"rx_sensors_enable:{bool_str(on)}"], []

    def _cmd_tx_sensors_enable(self, args):
        on = bool(args) and args[0].lower() == "true"
        return [f"tx_sensors_enable:{bool_str(on)}"], []

    def _cmd_tx_profiles_ex(self, args):
        # TCI Remote queries this right after connecting. The K3 has no TX
        # profile concept, so answer with an empty list rather than staying
        # silent -- an unanswered query leaves the client waiting.
        return ["tx_profiles_ex:"], []

    def _cmd_tx_profile_ex(self, args):
        return ["tx_profile_ex:"], []

    def _cmd_dds(self, args):
        # No panadapter; report the VFO so clients have a sane centre.
        return [f"dds:0,{self.state.vfo_a}"], []

    # ---------- transmit metering ----------

    def refresh_tm(self) -> None:
        """Which quantity the bargraph is reporting.

        BG returns bars whose meaning depends on the METER setting: 00-12
        for RF power under TM0, 00-07 for ALC under TM1. Reporting one as
        the other would be worse than reporting nothing, so read it rather
        than assume. TM is K3/K3S only.
        """
        r = self.cat.ask("TM")
        if r and r.startswith("TM") and len(r) >= 4 and r[2].isdigit():
            self.tm_mode = int(r[2])

    def read_tx_meters(self) -> tuple[int | None, int | None, float | None]:
        """(alc, fwd_bars, swr) during transmit.

        The reference warns against polling faster than ~100 ms and against
        polling during transmit at all unless necessary -- this is the one
        place it is necessary, so it runs at 5 Hz and only while keyed.
        """
        alc = fwd = swr = None
        r = self.cat.ask("BG")
        if r and r.startswith("BG") and len(r) >= 5:
            try:
                bars, flag = int(r[2:4]), r[4]
            except ValueError:
                bars = flag = None
            # An 'R' reading is the S-meter, not transmit metering.
            if flag == "T" and bars is not None:
                if self.tm_mode == 1:
                    alc = bars                 # 00-07
                else:
                    fwd = bars                 # 00-12
        w = self.cat.ask("SW")
        if w and w.startswith("SW") and len(w) >= 6:
            try:
                swr = int(w[2:5]) / 10.0       # tenths, 1.0-99.9
            except ValueError:
                pass
        return alc, fwd, swr

    # ---------- audio gain ----------

    def audio_gain(self) -> float:
        """Linear multiplier for the RX audio frames."""
        if self.state.muted or self.state.volume_db <= -60:
            return 0.0
        return float(10.0 ** (self.state.volume_db / 20.0))

    def _cmd_volume(self, args):
        # Global master volume: no trx field in the spec form. A legacy
        # trx-prefixed form exists, so take the value from the last arg.
        if args and args[0] != "":
            try:
                val = float(args[-1])
            except ValueError:
                return [], []
            # Legacy clients send percent (>=1); the spec sends dB (<=0).
            if val >= 1.0:
                pct = min(val, 100.0)
                self.state.volume_db = (
                    -60 if pct <= 0 else
                    max(-60, min(0, round(20 * math.log10(pct / 100.0)))))
            else:
                self.state.volume_db = max(-60, min(0, int(round(val))))
            return [], [f"volume:{self.state.volume_db}"]
        return [f"volume:{self.state.volume_db}"], []

    # -- AGC --------------------------------------------------------------
    # GT002 fast, GT004 slow. Without K22 the radio has no AGC-off, so a
    # TCI "off" is refused rather than mapped to something it is not
    # (command map, "Unmappable in v1"); "normal" and "med" mean fast.
    AGC_TO_GT = {"fast": "GT002", "normal": "GT002", "med": "GT002",
                 "slow": "GT004"}

    def _parse_gt(self, r) -> str | None:
        if r and r.startswith("GT") and len(r) >= 5:
            return {"002": "fast", "004": "slow"}.get(r[2:5])
        return None

    def refresh_agc(self) -> None:
        agc = self._parse_gt(self.cat.ask("GT"))
        if agc:
            self.state.agc = agc

    def _cmd_agc_mode(self, args):
        if len(args) >= 2:                      # SET
            gt = self.AGC_TO_GT.get(args[1].lower())
            if gt is None:
                log.info("unsupported agc_mode %r ignored", args[1])
                return [], []
            self.cat.set_verified(gt, "GT", gt + ";")
            self.refresh_agc()
            return [], [f"agc_mode:0,{self.state.agc}"]
        return [f"agc_mode:0,{self.state.agc}"], []

    # -- preamp, attenuator, noise blanker -----------------------------------
    # The bridge's own messages except rx_nb_enable, which is TCI's. All
    # three are GET/SET commands that read back as plain ASCII, and the radio
    # reports all three on a band change -- where they can differ, since
    # they are stored per band / per RX ANT -- so on_cat_event keeps them
    # current without polling.

    def _parse_frontend(self, r) -> bool:
        """Apply a PA, RA, NB or NL reply to the state. True if it was one."""
        s = self.state
        if not r:
            return False
        try:
            if r.startswith("PA") and len(r) >= 3 and r[2] in "01":
                s.preamp = r[2] == "1"
            elif r.startswith("RA") and len(r) >= 4 and r[2:4].isdigit():
                s.att = int(r[2:4]) != 0
            elif r.startswith("NB") and len(r) >= 3 and r[2] in "01":
                s.nb = r[2] == "1"
            elif r.startswith("NL") and len(r) >= 6 and r[2:6].isdigit():
                s.nb_levels = (int(r[2:4]), int(r[4:6]))
            else:
                return False
        except ValueError:
            return False
        return True

    def refresh_rx_frontend(self) -> None:
        for cmd in ("PA", "RA", "NB", "NL"):
            self._parse_frontend(self.cat.ask(cmd))

    def rx_frontend_notifications(self) -> list[str]:
        s = self.state
        # nb_levels because NB1 with both levels at 00 blanks nothing: the
        # button would light and do nothing, so the page says why.
        return [f"preamp:0,{bool_str(s.preamp)}",
                f"attenuator:0,{bool_str(s.att)}",
                f"rx_nb_enable:0,{bool_str(s.nb)}",
                f"nb_levels:0,{s.nb_levels[0]},{s.nb_levels[1]}"]

    def _set_frontend(self, args, on_cmd, off_cmd, query):
        if len(args) >= 2 and args[1].lower() in ("true", "false"):
            cmd = on_cmd if args[1].lower() == "true" else off_cmd
            self.cat.set_verified(cmd, query, cmd + ";")
            self.refresh_rx_frontend()
            return [], self.rx_frontend_notifications()
        return self.rx_frontend_notifications(), []

    def _cmd_preamp(self, args):
        return self._set_frontend(args, "PA1", "PA0", "PA")

    def _cmd_attenuator(self, args):
        # RA01, not RA10: this K3 has one 10 dB pad and reads RA05/10/15
        # back as RA01 (command map, "Verified on hardware").
        return self._set_frontend(args, "RA01", "RA00", "RA")

    def _cmd_rx_nb_enable(self, args):
        return self._set_frontend(args, "NB1", "NB0", "NB")

    # -- NR and notch -----------------------------------------------------
    # The K3 has no NR or notch command, only the front-panel switches
    # (SWT34 = NR, SWT32 = NTCH, programmer's reference Table 7). Their
    # state is only in DS's icon-flash byte, which the reader frames by
    # length for exactly this (K3Cat.ask_display). So a press is still a
    # press -- the radio decides what NTCH cycles to -- but what it landed
    # on is read back and reported.
    #
    # AI2 does not report either switch, so a press at the radio reaches
    # clients through the server's reconcile sweep, within about 3 s.

    # Icon-flash byte, K31 (programmer's reference, DS).
    DS_NR, DS_NTCH, DS_MAN_NOTCH = 0x04, 0x02, 0x01

    def refresh_display(self) -> None:
        r = self.cat.ask_display(quiet=self.state.transmitting)
        if not r or not r.startswith("DS") or len(r) < 13:
            log.debug("refresh_display: no usable DS reply (%r)", r)
            return
        f = ord(r[11])
        if not f & 0x80:
            # Bit 7 is always set in both icon bytes; a clear one means the
            # frame is not what we think it is. Leave the state alone.
            log.debug("refresh_display: icon byte %#x lacks bit 7", f)
            return
        s = self.state
        s.nr = bool(f & self.DS_NR)
        s.notch = ("manual" if f & self.DS_MAN_NOTCH else
                   "auto" if f & self.DS_NTCH else "off")

    def display_notifications(self) -> list[str]:
        s = self.state
        # rx_nr_enable is TCI's; notch is the bridge's own, because TCI's
        # rx_anf_enable has no way to say "manual".
        return [f"rx_nr_enable:0,{bool_str(s.nr)}", f"notch:0,{s.notch}"]

    def _tap(self, code):
        if self.state.transmitting:
            return [], []
        self.cat.send(f"SWT{code}")
        time.sleep(0.25)      # switch emulation wants a gap before the next
        self.refresh_display()
        return [], self.display_notifications()

    def _cmd_nr_tap(self, args):
        return self._tap(34)

    def _cmd_notch_tap(self, args):
        return self._tap(32)

    # -- squelch and VFO lock -----------------------------------------------
    # SQ is 000-029 and 000 is open; there is no separate on/off. TCI has
    # both, so the level is kept here while squelch is off and written back
    # when it comes on. On this radio SQ acts on the main receiver only if
    # CONFIG:SQ MAIN is numeric (programmer's reference, SQ).

    SQ_MAX = 29

    def _sq_to_tci(self, n: int) -> int:
        return round(n * 100 / self.SQ_MAX)

    def _tci_to_sq(self, v: int) -> int:
        return max(0, min(self.SQ_MAX, round(v * self.SQ_MAX / 100)))

    def _parse_sql_lock(self, r) -> bool:
        s = self.state
        if r and r.startswith("SQ") and len(r) >= 5 and r[2:5].isdigit():
            n = int(r[2:5])
            s.sql_on = n > 0
            if n > 0:
                s.sql_level = self._sq_to_tci(n)
            return True
        if r and r.startswith("LK") and len(r) >= 3 and r[2] in "01":
            s.lock = r[2] == "1"
            return True
        return False

    def refresh_sql_lock(self) -> None:
        for cmd in ("SQ", "LK"):
            self._parse_sql_lock(self.cat.ask(cmd))

    def sql_lock_notifications(self) -> list[str]:
        s = self.state
        return [f"sql_enable:0,{bool_str(s.sql_on)}",
                f"sql_level:0,{s.sql_level}",
                f"lock:0,{bool_str(s.lock)}"]

    def _write_sq(self, n: int):
        cmd = f"SQ{n:03d}"
        self.cat.set_verified(cmd, "SQ", cmd + ";")
        self.refresh_sql_lock()
        return [], self.sql_lock_notifications()

    def _cmd_sql_enable(self, args):
        if len(args) >= 2 and args[1].lower() in ("true", "false"):
            on = args[1].lower() == "true"
            return self._write_sq(self._tci_to_sq(self.state.sql_level)
                                  if on else 0)
        return self.sql_lock_notifications(), []

    def _cmd_sql_level(self, args):
        if len(args) >= 2:
            try:
                v = max(0, min(100, int(float(args[1]))))
            except ValueError:
                return [], []
            self.state.sql_level = v
            if self.state.sql_on:
                return self._write_sq(self._tci_to_sq(v))
            return [], self.sql_lock_notifications()
        return self.sql_lock_notifications(), []

    def _cmd_lock(self, args):
        if len(args) >= 2 and args[1].lower() in ("true", "false"):
            cmd = "LK1" if args[1].lower() == "true" else "LK0"
            self.cat.set_verified(cmd, "LK", cmd + ";")
            self.refresh_sql_lock()
            return [], self.sql_lock_notifications()
        return self.sql_lock_notifications(), []

    def _cmd_mute(self, args):
        if len(args) >= 2:
            self.state.muted = args[1].lower() == "true"
            return [], [f"mute:0,{bool_str(self.state.muted)}"]
        return [f"mute:0,{bool_str(self.state.muted)}"], []

    def _cmd_rx_mute(self, args):
        return self._cmd_mute(args)

    # ---------- metering ----------

    def read_text(self) -> str | None:
        """Characters decoded since the last call: `""` if none, None on a
        reply we could not use.

        `TB` returns the K3's decoded CW/RTTY/PSK text. Three things about
        it shape everything above:

        READING IS DESTRUCTIVE. The radio clears its RX count as it answers,
        so whatever this returns is the only time anyone sees it. Nothing
        may call this except the one poll loop -- a second caller would eat
        characters the first will never know existed. (The radio's own VFO B
        display is driven separately and is not consumed by `TB`.)

        THE BUFFER HOLDS 40 CHARACTERS, and the reference is explicit that
        the application must "poll with TB; often enough to prevent loss of
        incoming text". 40 characters is about 12 s of 40 WPM CW, so the
        poll interval has a lot of headroom -- but it is a real deadline,
        not a quality setting.

        AN EMPTY REPLY IS AMBIGUOUS. `TB000;` is what a radio with text
        decode switched off returns, and it is equally what a radio with
        text decode on and nothing to hear returns. There is no way to tell
        them apart from CAT, so this does not try; callers report the
        distinction as unknown rather than guessing. Text decode is enabled
        at the front panel (hold TEXT DEC) and has no CAT command.
        """
        r = self.cat.ask_text()
        if not r or not r.startswith("TB") or len(r) < 6:
            return None
        try:
            n = int(r[3:5])
        except ValueError:
            return None
        # Trust the count, not the terminator -- that is the whole point of
        # the counted read in k3cat.ask_text. Text shorter than the count
        # means a truncated reply; treat it as unusable rather than
        # publishing a fragment.
        body = r[5:-1]
        if len(body) < n:
            log.debug("TB short: %d chars declared, %d present (%r)",
                      n, len(body), r)
            return None
        return body[:n]

    def read_smeter(self) -> int | None:
        """S-meter in dBm for `rx_smeter`.

        SMH is preferred: ~1 dB resolution against SM's 5-6 dB.

        SMH is CALIBRATED against a signal generator, with an Elecraft P3 as
        the level reference (tools/smcal2-retest-{slow,fast}.csv, 2026-09-19).
        Two straight segments meeting at SMH 55, fitted to both AGC settings
        together: 0.81 dB rms, 2.05 dB worst, from -115 to -20 dBm. The
        reference's anchors were well off on this radio. S9 (-73 dBm) reads
        SMH 37, not 40, and the slope changes at 55, not 40. The old
        conversion read 3-8 dB low across the whole working range.

        What the calibration covers: 14.1 MHz, CW, preamp off, attenuator
        off, RF GAIN at max. Other bands were not measured. The meter reads
        the AGC line, so reduced RF GAIN pins it (see the command map), and
        the preamp or attenuator moves it by an amount not measured here.
        Below about SMH 4 the meter is reading the receiver's own noise.
        Above SMH 96 (-20 dBm) the upper line is extrapolated, and the
        radio's overload relay pulls in at -10 dBm.

        The SM fallback is still the reference's uncalibrated curve. The
        same runs fit it poorly with two lines (2-3 dB rms), and it is only
        used when SMH fails.
        """
        dbm = self._read_meter_dbm()
        if dbm is None:
            return None
        # THE PREAMP AND ATTENUATOR ARE IN FRONT OF THE METER, so with the
        # pad in the radio reads a signal 10 dB low, and with the preamp on
        # it reads high. Both are taken back out, so the reading stays a
        # level at the antenna whatever the front end is doing.
        #
        # Both are 10 dB by assumption, not measurement. The pad is the
        # reference's nominal figure; the attempts to measure it on WWV were
        # swamped by fading (command map, item 7). The preamp is taken as
        # 10 dB on the operator's decision -- no generator run covers it.
        s = self.state
        return (dbm + (self.ATT_DB if s.att else 0)
                    - (self.PRE_DB if s.preamp else 0))

    ATT_DB = 10
    PRE_DB = 10

    def _read_meter_dbm(self) -> int | None:
        """The calibrated conversion, at the receiver input."""
        r = self.cat.ask("SMH")
        if r and r.startswith("SMH") and len(r) >= 7:
            n = self._meter_count(r, r[3:6], self.SMH_MAX)
            if n is None:
                return None
            # measured: 1.222 dB/count up to SMH 55 (-51.5 dBm), 0.78 above
            if n <= 55:
                return int(round(-118.71 + 1.222 * n))
            return int(round(-51.5 + 0.78 * (n - 55)))
        r = self.cat.ask("SM")
        if r and r.startswith("SM") and len(r) >= 7:
            n = self._meter_count(r, r[2:6], self.SM_MAX)
            if n is None:
                return None
            # K31 scale: 0000-0021, S9=9, then 5 dB per step above it
            if n <= 9:
                return int(round(-73 - 6 * (9 - n)))
            return int(round(-73 + 5 * (n - 9)))
        return None

    # Documented full-scale counts: SMH 0-140, and SM 0-21 under K31 (its
    # range changes with K2x while the field stays four digits, so a reading
    # above 21 is also how a lost K31 would show itself).
    SMH_MAX, SM_MAX = 140, 21

    def _meter_count(self, reply: str, digits: str, top: int) -> int | None:
        """Parse an S-meter count, or None if it is not a reading.

        RANGE-CHECKED, because nothing downstream can tell a wrong number
        from a strong signal. Both curves are unbounded above -- SMH 999
        converts to +853 dBm -- and the UI clamps its bar at S9+60, so ANY
        over-range count paints exactly the same full-scale meter as a real
        S9+60 signal. A count out of range means the reply was not what we
        think it was: a corrupted digit at 38400 baud, a reply framed
        against the wrong command, or K31 lost (see SM_MAX). None of those
        should be shown to the operator as a signal.

        The raw reply is logged because this is the only record of it. A
        pinned meter is over in a fifth of a second and leaves nothing
        behind; a journal line naming the reply is what makes the next one
        diagnosable.
        """
        try:
            n = int(digits)
        except ValueError:
            log.warning("unparsable S-meter reply %r", reply)
            return None
        if not 0 <= n <= top:
            log.warning("S-meter count %d out of range 0-%d in %r "
                        "-- discarded", n, top, reply)
            return None
        return n

    # ---------- background duties ----------

    def _clear_deadline_if_receiving(self) -> None:
        """A radio seen in receive has no stuck transmitter to rescue.

        The CW path arms the watchdog against its queued `RX;` going
        missing, and nothing used to disarm it when that `RX;` worked: the
        radio unkeyed at the end of the message and the watchdog fired
        anyway, sixteen seconds later, forcing an RX into a radio already
        receiving and logging it as an expiry. Harmless in itself, but a
        deadline left lying around is one that can fire into a LATER
        transmission -- one started at the front panel, which sets no
        deadline of its own to overwrite it.

        Called from wherever the radio's own transmit state is read.
        """
        if self.state.transmitting:
            return
        # A snapshot read while we were keying can report the state from
        # just before it, so a moment's grace after keying keeps an IF that
        # was already in flight from disarming a transmission that has only
        # just started.
        if time.monotonic() - self._keyed_at < 2.0:
            return
        self._ptt_deadline = None
        # AND THE OWNER, which used to be cleared only alongside a deadline.
        # A CW stop leaves no deadline to clear, so the owner outlived the
        # transmission: disconnecting minutes later logged "PTT owner
        # disconnected while keyed -- unkeying" at a radio that had been
        # receiving the whole time, and sent it a pointless RX. Nobody holds
        # PTT on a radio that is not transmitting.
        self._ptt_owner = None

    def ptt_watchdog(self) -> str | None:
        """Force RX if a client keyed us and then went away. This is the one
        place where a dropped connection leaves the radio in a physically
        bad state, so it does not rely on the client sending PTT-off."""
        if self._ptt_deadline and time.monotonic() > self._ptt_deadline:
            log.warning("PTT watchdog expired -- forcing RX")
            # cw_stop, not _key_off: if a CW send is still queueing chunks
            # it has to be abandoned too, or the worker keys the radio
            # straight back up behind the watchdog that just stopped it.
            self.cw_stop()
            self._ptt_deadline = None
            self._ptt_owner = None
            self.state.transmitting = False
            return "trx:0,false"
        return None

    def release_client(self, client) -> str | None:
        """A client went away. If it was holding PTT, unkey now."""
        with self._lock:
            if client is not None and client is self._ptt_owner:
                log.warning("PTT owner disconnected while keyed -- unkeying")
                self.cw_stop()
                self._await_tq(False)
                self._ptt_deadline = None
                self._ptt_owner = None
                self.ptt_wants_audio = False
                self.state.transmitting = False
                return "trx:0,false"
        return None

    def force_rx(self) -> None:
        if self._ptt_deadline is not None or self.state.transmitting:
            log.warning("forcing RX")
            self.cw_stop()
            self._ptt_deadline = None
            self.state.transmitting = False

    def on_cat_event(self, msg: str) -> list[str]:
        """Unsolicited AI2 message -> TCI notifications for all clients."""
        out: list[str] = []
        s = self.state
        if msg.startswith("FA") and len(msg) >= 14:
            try:
                s.vfo_a = int(msg[2:13]); out.append(f"vfo:0,0,{s.vfo_a}")
            except ValueError:
                pass
        elif msg.startswith("FB") and len(msg) >= 14:
            try:
                s.vfo_b = int(msg[2:13]); out.append(f"vfo:0,1,{s.vfo_b}")
            except ValueError:
                pass
        elif msg[:2] in ("SQ", "LK") and self._parse_sql_lock(msg):
            # Not known to be reported by AI2; handled in case they are.
            out += self.sql_lock_notifications()
        elif msg[:2] in ("PA", "RA", "NB", "NL") and self._parse_frontend(msg):
            # Front-panel presses and the burst a band change reports.
            out += self.rx_frontend_notifications()
        elif msg.startswith("GT") and self._parse_gt(msg):
            # The AGC key on the front panel, reported by AI2.
            s.agc = self._parse_gt(msg)
            out.append(f"agc_mode:0,{s.agc}")
        elif msg.startswith("MD") and len(msg) >= 4 and msg[2] in K3_TO_TCI:
            s.mode = K3_TO_TCI[msg[2]]
            out.append(f"modulation:0,{s.mode}")
        # Same length trap as refresh_if, and it survived the fix there: this
        # radio's IF is 37 characters, so `len(msg) >= 38` rejected every
        # unsolicited one and this branch had never run. Silent, because a
        # band change auto-reports FA and MD alongside IF and those branches
        # do work -- so frequency and mode tracked, and only split and the
        # TX flag sat still until the 3 s reconcile swept them up. Ask
        # whether the fields being read are present, exactly as refresh_if
        # now does; every one is at IF_LAST_FIELD or below.
        elif msg.startswith("IF") and len(msg) > self.IF_LAST_FIELD:
            before = vars(s).copy()
            self._parse_if_str(msg)
            if vars(s) != before:
                out += self.if_notifications()
        return out

    def if_notifications(self) -> list[str]:
        """Everything one IF reply carries. RIT/XIT are in it because the
        radio's own RIT and XIT keys have no other way to reach a client."""
        s = self.state
        return [f"vfo:0,0,{s.vfo_a}", f"modulation:0,{s.mode}",
                f"split_enable:0,{bool_str(s.split)}",
                f"trx:0,{bool_str(s.transmitting)}",
                f"rit_enable:0,{bool_str(s.rit_on)}",
                f"xit_enable:0,{bool_str(s.xit_on)}",
                *self._offset_notifications()]

    def _parse_if_str(self, r: str) -> None:
        s = self.state
        try:
            s.vfo_a = int(r[2:13])
            sign = -1 if r[18] == "-" else 1
            s.rit_offset = sign * int(r[19:23])
            s.rit_on, s.xit_on = r[23] == "1", r[24] == "1"
            s.transmitting = r[28] == "1"
            s.mode = K3_TO_TCI.get(r[29], s.mode)
            s.split = r[32] == "1"
            self._clear_deadline_if_receiving()
        except (ValueError, IndexError):
            pass
