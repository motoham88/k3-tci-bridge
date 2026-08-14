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


def bool_str(v: bool) -> str:
    return "true" if v else "false"


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


class Bridge:
    """Owns the radio state and translates TCI <-> CAT."""

    def __init__(self, cat):
        self.cat = cat
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

    def refresh_if(self) -> None:
        """IF is 38 chars with every field at a fixed offset -- verified on
        the bench. One read gives TX state, mode, split and RIT/XIT."""
        r = self.cat.ask("IF", timeout=0.8)
        if not r or not r.startswith("IF") or len(r) < 38:
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
            f"trx:0,{bool_str(s.transmitting)}",
            f"drive:0,{self._read_pc()}",
            f"mic_level:{self._read_mic()}",
            f"mon_volume:{self._read_mon()}",
            f"volume:{s.volume_db}",
            f"mute:0,{bool_str(s.muted)}",
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
            # Broadcast what the radio accepted, not what was asked for: an
            # out-of-band request snaps to the nearest amateur band.
            return [], [f"vfo:0,{chan},{accepted}"]
        # GET
        chan = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0
        hz = self.state.vfo_a if chan == 0 else self.state.vfo_b
        return [f"vfo:0,{chan},{hz}"], []

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
                self.cat.send("TX")
                ok = self._await_tq(True)
                if not ok:
                    log.warning("TX; did not take")
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
                self.cat.send("RX")
                if not self._await_tq(False):
                    # Retry once, then leave the watchdog ARMED. Clearing the
                    # deadline on an unconfirmed unkey would disable the only
                    # thing that can rescue a stuck transmitter.
                    log.warning("RX; unconfirmed -- retrying")
                    self.cat.send("RX")
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

    def _cmd_tune(self, args):
        """TUNE = hold XMIT (SWH16). Emits a carrier at the current power,
        which is how the ATU is asked to tune."""
        if len(args) >= 2:
            on = args[1].lower() == "true"
            self.cat.send("SWH16" if on else "RX")
            time.sleep(0.2)
            tq = self.cat.ask("TQ")
            return [], [f"tune:0,{bool_str(tq == 'TQ1;')}"]
        return [f"tune:0,{bool_str(self.state.transmitting)}"], []

    # -- CW keying --------------------------------------------------------

    # KY takes at most 24 characters of text per command. The 'W' (wait)
    # form defers any following commands until the message has been sent,
    # which matters because a speed change queued behind a message must not
    # overtake it.
    CW_MAX = 24

    def _cw_send(self, text: str) -> bool:
        """Feed text to the K3's CW buffer, chunked and flow-controlled.

        KY; reports 1 when the buffer is full, so wait rather than
        overrunning it. Only usable in CW (and CW-to-DATA) modes.
        """
        if self.state.mode not in ("cwl", "cwu"):
            log.warning("cw_msg ignored: mode is %s, not CW", self.state.mode)
            return False

        # VOX must be on for KY text to actually key the transmitter. In CW
        # "VOX" means hit-the-key transmit -- without it the K3 accepts the
        # text into its buffer and simply never transmits, with no error
        # anywhere. VX is per-mode, so enabling it here does not touch the
        # voice-mode VOX setting. Enable it rather than just warning: the
        # point of a remote station is that nobody is there to press it.
        vx = self.cat.ask("VX")
        if vx == "VX0;":
            log.info("enabling CW VOX (VX1) -- required for KY keying")
            self.cat.set_verified("VX1", "VX", "VX1;")
        text = text.replace("\n", " ")
        chunks, cur = [], ""
        for word in text.split(" "):
            piece = (cur + " " + word) if cur else word
            if len(piece) <= self.CW_MAX:
                cur = piece
            else:
                if cur:
                    chunks.append(cur)
                # a single word longer than the limit: hard-split it
                while len(word) > self.CW_MAX:
                    chunks.append(word[:self.CW_MAX])
                    word = word[self.CW_MAX:]
                cur = word
        if cur:
            chunks.append(cur)

        for i, chunk in enumerate(chunks):
            waited = 0.0
            while self.cat.ask("KY") == "KY1;" and waited < 20.0:
                time.sleep(0.15)
                waited += 0.15
            if waited >= 20.0:
                log.warning("CW buffer stayed full; dropping the rest")
                return False
            # Trailing space between chunks so words do not run together.
            tail = " " if i < len(chunks) - 1 else ""
            self.cat.send("KYW" + chunk + tail)
        return True

    def _cmd_cw_msg(self, args):
        # Args were split on commas, but commas are legal inside CW text,
        # so put them back.
        text = ",".join(args).strip()
        if text:
            self._cw_send(text)
        return [], []

    def _cmd_cw_macros(self, args):
        return self._cmd_cw_msg(args)

    def _cmd_cw_macros_stop(self, args):
        # RX terminates message play, including a repeating message.
        self.cat.send("RX")
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

    def _cmd_mute(self, args):
        if len(args) >= 2:
            self.state.muted = args[1].lower() == "true"
            return [], [f"mute:0,{bool_str(self.state.muted)}"]
        return [f"mute:0,{bool_str(self.state.muted)}"], []

    def _cmd_rx_mute(self, args):
        return self._cmd_mute(args)

    # ---------- metering ----------

    def read_smeter(self) -> int | None:
        """S-meter in dBm for `rx_smeter`.

        SMH is preferred: ~1 dB resolution against SM's 5-6 dB.

        WARNING: these curves are UNCALIBRATED. They are anchored on the
        reference's documented points (S9 = SM 9 / SMH 40, S9+60 = SM 21 /
        SMH 100), but two attempts to verify them against the radio's 10 dB
        attenuator gave irreconcilable results -- the WWV signal used as a
        source faded more than the step being measured. Calibrating this
        properly needs a signal generator. Until then, treat the output as
        relative rather than absolute; it tracks changes, but the stated dBm
        may be off by a good margin.
        """
        r = self.cat.ask("SMH")
        if r and r.startswith("SMH") and len(r) >= 7:
            try:
                n = int(r[3:6])
            except ValueError:
                return None
            # anchors: S1=5, S9=40, S9+60=100
            if n <= 40:
                return int(round(-121 + (n - 5) * (48 / 35)))
            return int(round(-73 + (n - 40)))
        r = self.cat.ask("SM")
        if r and r.startswith("SM") and len(r) >= 7:
            try:
                n = int(r[2:6])
            except ValueError:
                return None
            # K31 scale: 0000-0021, S9=9, then 5 dB per step above it
            if n <= 9:
                return int(round(-73 - 6 * (9 - n)))
            return int(round(-73 + 5 * (n - 9)))
        return None

    # ---------- background duties ----------

    def ptt_watchdog(self) -> str | None:
        """Force RX if a client keyed us and then went away. This is the one
        place where a dropped connection leaves the radio in a physically
        bad state, so it does not rely on the client sending PTT-off."""
        if self._ptt_deadline and time.monotonic() > self._ptt_deadline:
            log.warning("PTT watchdog expired -- forcing RX")
            self.cat.send("RX")
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
                self.cat.send("RX")
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
            self.cat.send("RX")
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
        elif msg.startswith("MD") and len(msg) >= 4 and msg[2] in K3_TO_TCI:
            s.mode = K3_TO_TCI[msg[2]]
            out.append(f"modulation:0,{s.mode}")
        elif msg.startswith("IF") and len(msg) >= 38:
            before = (s.mode, s.split, s.transmitting, s.vfo_a)
            self._parse_if_str(msg)
            if (s.mode, s.split, s.transmitting, s.vfo_a) != before:
                out += [f"vfo:0,0,{s.vfo_a}", f"modulation:0,{s.mode}",
                        f"split_enable:0,{bool_str(s.split)}",
                        f"trx:0,{bool_str(s.transmitting)}"]
        return out

    def _parse_if_str(self, r: str) -> None:
        s = self.state
        try:
            s.vfo_a = int(r[2:13])
            s.rit_on, s.xit_on = r[23] == "1", r[24] == "1"
            s.transmitting = r[28] == "1"
            s.mode = K3_TO_TCI.get(r[29], s.mode)
            s.split = r[32] == "1"
        except (ValueError, IndexError):
            pass
