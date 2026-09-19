# K3S ↔ TCI Command Map

Companion to `k3-tci-bridge-design.md`. This is the v1 implementation
reference: every TCI text command the bridge will answer, the K3S CAT
commands behind it, and the traps.

Sources:

- Elecraft **K3S/K3/KX3/KX2 Programmer's Reference, Rev. G5** (Feb 2019).
  Section references below are to that document.
- **TCI protocol v2.0** wire formats, cross-checked against the working
  server implementation in `~/github/AetherSDR/src/core/TciProtocol.cpp`
  and `TciServer.cpp`.

Radio: **an original K3, not a K3S** — established on the bench, see
"Verified on hardware" below. The USB interface (FT232 CAT + PCM2901 codec
behind a TUSB2036 hub) is present and working regardless, so the one-cable
convenience holds; but the RF board is a K3, which changes the `RA` command
format and rules out the K3S stepped attenuator.

---

## Global rules

These govern the whole bridge. Most of the bugs will live here, not in the
table.

### 1. Meta-command mode: `K31;` + `K20;`

Send `K31;` at startup and leave `K20` (the default) alone.

`K31` buys two things worth having: `SM` widens from 0000-0015 to
**0000-0021** (~5-6 dB per step instead of ~10), and the `IF` response's
`d` field carries the live DATA sub-mode.

`K22` was considered and rejected. It would give `GTnnnx;` (AGC on/off, so
`agc_mode:off` could be honored), but it also changes the format of `PC`,
`NB`, `KY` and `GT` — four mode-dependent parsers for one TCI value. Not
worth it. `agc_mode:off` is listed as unmappable below.

**Never send `FW`; use `BW`.** `FW`'s semantics shift under `K2x`/`K3x`,
`BW` is unconditional. Note this protects our *writes* only — see rule 5,
the radio emits `FW` unsolicited regardless.

### 2. Read back after every clamping SET

The K3 silently alters values it can't honor. `BW` is *"quantized and/or
range limited based on the present operating mode"*; `IS` says *"the SET
value may be altered based on the present mode; a subsequent IS GET
reports the value used"*; `PC` caps per band and on transverter bands;
`RA` accepts only 00/05/10/15.

So for every SET in the "read-back" column: issue the SET, issue the GET,
and broadcast **the value the radio accepted** — never the value the client
asked for. This mirrors what AetherSDR's TCI server does with `vfo:`
(`TciServer.cpp:1341` broadcasts the model-accepted Hz, not the request).

**A SET can also be dropped entirely, with no error.** Observed on the
bench: an `MD2;` was silently ignored and the radio stayed in its previous
mode, with no `?;` and no other indication. Mode changes especially need
set-verify-retry, not fire-and-forget — a mode SET that quietly fails leaves
every subsequent filter and sideband decision wrong. Allow ~350 ms after the
SET before reading back; a single retry cleared it every time.

### 3. `?;` can come back from anything

*"If a command cannot respond due to such a condition, the K3 will return
'?;'"* — busy, transmitting, BSET mode, VFO A/B reverse. Every request
needs a timeout **and** a `?;` branch. Never block waiting on a RSP that
will never arrive.

### 4. Startup sequence

Order matters — `OM;` gates two independent decisions.

```
K31;                    enable K3 extended mode
OM;                     detect options (see below)
AI2;                    unsolicited updates on
<initial state sweep>   FA; FB; IF; MD; DT; BW; IS; RT; XT; RO; PC; AG; ...
<emit TCI init block>   protocol, device, receive_only, trx_count, ...
ready;
start;
```

`OM;` on a K3/K3S returns `OM APXSDFfLVR--;` with absent modules replaced
by `-`. Two letters matter to us:

- **`R`** = K3S RF board. *"The presence of 'R' … is the preferred way to
  identify a K3S."* Presence selects the K3S `RA` format (00/05/10/15 dB)
  over the legacy K3 form (00/01).
- **`S`** = KRX3A sub receiver installed. Gates whether `trx_count` can
  ever be 2 (see rule 6).

This radio returns `OM APX-D---V---;` — **no `R`, no `S`**. So: legacy `RA`
format, and no sub receiver. Installed: KAT3A ATU (`A`), KPA3A PA (`P`),
KXV3 transverter/RX-I/O (`X`), KDVR3 voice recorder (`D`), KSYN3A
synthesizer (`V`).

**Do not detect the sub receiver by probing `$` commands.** They answer
whether or not the KRX3A is fitted — this radio returns `MD$2;`,
`SM$0000;`, `AG$000;` with no sub RX installed. `OM;` is the only reliable
test.

### 5. AI2 echo-loop guard, and AI2 is not enough

`AI2;` makes the radio emit `FA`, `FB`, `IF`, `GT`, `MD`, `RA`, `PC` etc.
on front-panel changes. This is how we satisfy TCI's "broadcast every state
change to all clients" requirement without hammering the serial port.

Two caveats:

- **Our own SETs come back as AI2 responses.** Don't re-broadcast them as
  fresh hardware events — track outstanding SETs and swallow the echo.
- **AI2 is incomplete**: *"At present only a subset of controls generate
  responses."* Add a slow reconcile poll (every 2-5 s) over the state AI2
  doesn't reliably push, so client UIs don't silently go stale.

Independently of AI mode, a **band change** auto-reports `IF, FA, FB, FR,
FT, PA, RA, AN, GT, FW, NB`. So the reader must handle `FW` responses even
though we never send `FW`.

### 6. `trx_count:1` — permanent on this radio

VFO A always receives on a K3 (see `FR` below), and TCI models A/B as
channel 0 and channel 1 of a single trx. So one trx, two channels covers
VFO A, VFO B and split.

This was written as a v1 simplification; `OM;` has since settled it as a
fact. No KRX3A is installed, so there is no second receiver to expose. The
sub-RX-as-trx-1 path stays unwritten until the hardware changes.

### 7. Serial reader stays ASCII

`DS` (VFO A text/icons) and `IC` (icon/status) return bytes ≥ 0x80 and must
be read as binary, not text. `IC` is read once at open, before the reader
thread starts (for TX TEST). `IC` stays out of the running reader.

**`DS` is the second exception, framed by length** like `TB` below: a fixed
13 bytes, `DSttttttttaf;`, decoded as Latin-1 so every byte keeps its value.
It is the only source of main-RX NR and notch state (icon-flash byte `f`,
K31: B2 NR, B1 NTCH, B0 manual notch). The path opens only while a `DS` GET
is outstanding, and a frame whose 13th byte is not `;` resyncs on `;`
rather than being parsed.

**`TB` is the one exception, and it is framed by count, not by `;`.** Its
decoded text may legally contain semicolons (see *Text decode* below), so
the reader takes the exact number of characters the reply declares whenever
a `TB` GET is outstanding. That path opens only for a reply we asked for —
`TB` is GET-only, so the radio never sends one unsolicited. Everything else
still stops at the first `;`.

`RV`'s response format is printed in the reference **without** a
terminating semicolon (`RVxNN.NN`) — every other RSP has one. Parse
defensively if you use it.

### 8. Baud rate is a prerequisite, not a setup step

Require `CONFIG:RS232 = 38400` on the radio. Do not send `BR3;` — changing
baud mid-session forces the host to reopen the port, a fragile startup path
for no gain.

At 38400 8N1 (3840 B/s) the CAT link is nowhere near saturated by our
traffic; an `SM;`/`SMH;` poll round trip is ~10 bytes. The constraint is the
radio, not the wire: *"Continuous, fast polling (< 100 ms per poll …) should
be carefully tested to ensure that it isn't affecting radio operation."*

### 9. Timing budget

- Typical response < 10 ms; worst case ~100 ms.
- **Band change: up to 500 ms**, during which *"all command handling is
  deferred."* Any `FA`/`FB` that crosses a band edge must widen the
  response timeout.
- `BN` SET: allow 300 ms before the next command.
- Switch emulation (`SWT`/`SWH`) needs a delay before a dependent command.

---

## Command map

`⚠` marks rows that need **hardware verification before shipping** — see
"Verify on hardware" at the end.

### Frequency and VFO

| TCI | K3S SET | Read back | Conversion | Notes |
|---|---|---|---|---|
| `vfo:0,0,<hz>` | `FA<11 digits>;` | `FA;` | Hz, zero-padded to 11 | Broadcast the accepted value. An out-of-band request is **not** snapped: `FA00008050000` reads back 8050000 (measured) — the radio is general-coverage |
| `vfo:0,1,<hz>` | `FB<11 digits>;` | `FB;` | same | If VFOs are linked (not split), `FA` also moves `FB` |
| `split_enable:0,true` | `FT1;` | `IF;` field `p` | — | `FT1` = TX on VFO B, which enters SPLIT |
| `split_enable:0,false` | `FR0;` | `IF;` field `p` | — | `FR` SET is the documented split cancel; see below |
| `dds:0` (GET) | — | — | echo VFO A Hz | No panadapter; report the VFO so clients don't divide by zero |
| `tx_frequency` (GET) | — | `IF;` field `p` + `FA;`/`FB;` | Hz | VFO B when split, else VFO A |
| `band:0,<metres>` (bridge's own) | `BN<nn>;`, then `FA` if off-band | `FA;`, `MD;`, `BW;`/`IS;` | metres → `BN` 00-10 (160…6) | Recalls the band memory like the BAND key; if it recalled outside the band, sets the band default, which also repairs the memory. Refused while transmitting. Table in `tci.BANDS`; the UI receives it as `band_plan` and makes one button per band |

The Hz digit is ignored unless the radio is in FINE mode (`SWT49`).

**Band memories hold whatever was last tuned, general coverage included.**
The K3 keeps one memory per band and files a frequency under the band whose
range contains it, so a mistyped 8.050 becomes the 40 m memory and the BAND
key recalls it there from then on. Measured by `bandtest.py` on 2026-09-19:
VFO A took 8.050 as asked, and after leaving 40 m and coming back `BN03`
recalled 8.050 — the bridge's `band` command then set 7.050. The same run
checked every `BN` number 00-10 lands inside its band. The `band` command
reads back after `BN` and overwrites an off-band recall with the default.

**On `FR` vs `FT`:** the reference titles `FR` as *"RX VFO Assignment [K2
only] and SPLIT Cancel"*. The "K2 only" qualifies the RX-VFO-assignment
half — *"n is ignored in the K3 case because VFO A is always active for
receive"* — but the split-cancel side effect is real on a K3: *"Any FR SET
cancels SPLIT mode."* Use `FR0;` to clear split and confirm with `IF;`.
(The reference's own CLEANUP macro uses `FT0;` for this. Mild internal
inconsistency; trust `FR0;` and verify.)

Because VFO A always receives, the `IF` response's `v` field reads `0`
permanently on a K3. Don't use it to infer anything.

### Mode

K3 `MD` codes, complete: **1**=LSB, **2**=USB, **3**=CW, **4**=FM,
**5**=AM, **6**=DATA, **7**=CW-REV, **9**=DATA-REV. (8 is undefined.)

`DT` sub-modes: **0**=DATA A, **1**=AFSK A, **2**=FSK D, **3**=PSK D.

| TCI modulation | K3S SET | Read back | Notes |
|---|---|---|---|
| `lsb` | `MD1;` | `MD;` | |
| `usb` | `MD2;` | `MD;` | |
| `cwl` | `MD3;` | `MD;` | **`MD3` (plain "CW") is LOWER sideband** — measured, not assumed |
| `cwu` | `MD7;` | `MD;` | `MD7` ("CW-REV") is upper — measured |
| `nfm` | `MD4;` | `MD;` | K3 FM is narrow; no wide variant |
| `am` | `MD5;` | `MD;` | |
| `digu` | `MD6;` then `DT0;` | `MD;` `DT;` | DATA A, upper — measured |
| `digl` | `MD9;` then `DT0;` | `MD;` `DT;` | DATA A reversed, lower — measured |

**The CW rows are the reverse of the intuitive guess.** An earlier draft of
this table had `cwu`→`MD3` and `cwl`→`MD7`, on the reasonable assumption
that a radio's plain "CW" mode sits on the upper sideband. On this K3 it
does not. Both rows are now measured against a WWV carrier (see below), and
`MD3` and `MD7` are exact mirror images of each other, as they must be.
Don't "fix" this back.

**Sequencing:** `MD` first, then `DT`. The reference warns *"Use DT only
when the transceiver is in DATA mode; otherwise, the returned value may not
be valid"* — so only trust a `DT` read when `MD` ∈ {6, 9}.

**Pin DIGU/DIGL to DATA A (`DT0`), not FSK-D or PSK-D.** This is a hard
constraint from the PTT path, not a preference: `TX;` is *"ignored"* in
FSK-D and PSK-D modes. If the radio is sitting in `DT2`/`DT3`, a
`trx:0,true` does nothing at all — no RF, no error. The PTT handler must
read `DT` and reply `trx:0,false;` rather than pretend it keyed.

**Side effect worth knowing:** norm/reverse is stored per *pair*, not per
sub-mode — DATA A pairs with PSK D, AFSK A with FSK D. So every
`digl`↔`digu` switch also flips PSK D's polarity. Harmless for this bridge,
surprising to an operator who uses PSK D at the radio.

`MD` under `K21`/`K23` rewrites modes 6 and 7 to 1 and 2. We run `K20`, so
this doesn't apply — but it's why the bridge must never send `K21`/`K23`.

### Filters and passband

TCI sends `rx_filter_band:0,<lo>,<hi>` in Hz **relative to the carrier**.
The K3 wants a bandwidth (`BW`, in 10 Hz units) and an **absolute AF center
frequency** (`IS`, in Hz). These are not the same coordinate system, and
the conversion is mode-dependent.

| Mode | Bandwidth | IF shift |
|---|---|---|
| `usb`, `digu` | `BW = (hi-lo)/10` | `IS = (lo+hi)/2` |
| `lsb`, `digl` | `BW = (hi-lo)/10` | `IS = abs(lo+hi)/2` |
| `cwu`, `cwl` | `BW = (hi-lo)/10` | **leave alone** |
| `am` | `BW = (hi-lo)/10` | **leave alone** |
| `nfm` | `BW = (hi-lo)/10` | not applicable — skip |

**The rule is: only touch `IS` when the TCI band is genuinely offset from
the carrier**, which means SSB and the DATA modes and nothing else.

`IS` is *"the AF center frequency (Fc) in Hz"*, and *"the nominal Fc …
varies with mode, and in CW or DATA modes will also vary with PITCH."* In
CW, AM and FM the passband straddles the carrier, so a client sends a
symmetric band (`-250,250` for CW, `-3000,3000` for AM) and `(lo+hi)/2`
comes out as zero. Writing that to `IS` would drag the passband down to DC.

An earlier version of this table had `am` deriving `IS` like SSB. That was
wrong for exactly this reason, and the implementation guards it twice: by
mode, and by refusing a computed centre of zero.

Going the other way (`BW`+`IS` → TCI edges), the same asymmetry applies:
USB is `centre ± half`, LSB is the negative mirror `-(centre ± half)`, and
CW/AM/FM are simply `±half` regardless of what `IS` reads — in CW that
centre is the sidetone pitch, which the client knows nothing about.

**`IS` requires a literal space**: the format is `IS*nnnn;` where `*` is
ASCII 0x20. `IS 9999;` centers the passband; `IS9999;` is a silent failure.

Clamp `BW` to the protocol range 0000-9999 and read back with `BW;`. Expect
the read-back to differ **substantially**, not slightly: 9999 is 99.99 kHz
in 10-Hz units, which no K3 mode accepts, and even a plausible request gets
quantized to the installed crystal filter and DSP steps (ask for 4.0 kHz in
`usb` and roughly 2.8 kHz comes back). Never assume the SET took.

| TCI | K3S SET | Read back | Notes |
|---|---|---|---|
| `rx_filter_band:0,<lo>,<hi>` | `BW<4 digits>;` + `IS <4 digits>;` | `BW;` `IS;` | Per table above |
| `if:0,0,<offset>` (non-zero) | `RO<sign><4 digits>;` + `RT1;` | `IF;` | Non-zero offset implies RIT on, per AetherSDR's handling |
| `if:0,0,0` | `RO 0000;` + `RT0;` | `IF;` | Clients clear the offset by sending 0 — that must also turn RIT **off**, or the radio sits enabled at zero offset |

### RIT / XIT

One shared offset register, range −9999…+9999 Hz. RIT and XIT enables are
independent, and the offset changes even when both are off.

| TCI | K3S SET | Read back | Notes |
|---|---|---|---|
| `rit_enable:0,<bool>` | `RT1;` / `RT0;` | `IF;` field `r` | Disabled in QRQ CW mode |
| `xit_enable:0,<bool>` | `XT1;` / `XT0;` | `IF;` field `x` | Disabled in QRQ CW mode |
| `rit_offset:0,<hz>` | `RO<s><nnnn>;` | `IF;` fields 19-23 | `s` is `+`, `-`, or space (= `+`); `nnnn` is 4 zero-padded digits |
| `xit_offset:0,<hz>` | `RO<s><nnnn>;` | `IF;` fields 19-23 | **Same register as RIT** — see below |

The K3 has *one* offset shared by RIT and XIT; TCI models them as two
independent values. There is no way to make both true. **Implemented:**
`rit_offset` and `xit_offset` both drive `RO`, and the bridge echoes BOTH
back on every offset change — and carries `xit_offset` in the init burst —
so a client that models the two separately cannot start out of sync or be
told about only the one it set. An operator who sets them differently will
see them snap together, which is the radio being honest rather than the
bridge losing a value.

`RT`/`XT` are documented as disabled in QRQ CW mode, so a SET that is
simply ignored is a normal outcome. Every one of these read-backs
broadcasts what the radio ACCEPTED (global rule 2); when the read-back
itself fails, the pre-SET value goes out rather than a false confirmation.

`RC;` (clear to zero), `RD;`/`RU;` (step down/up by the current VFO step —
1, 10, 20 or 50 Hz) are available but not needed for the TCI mapping.

### PTT and transmit

| TCI | K3S SET | Read back | Notes |
|---|---|---|---|
| `trx:0,true` | `TX;` | `TQ;` | Refuse (reply `trx:0,false;`) if `DT` is 2 or 3 — `TX;` is ignored in FSK-D/PSK-D |
| `trx:0,false` | `RX;` | `TQ;` | Terminates TX in all modes including message play |
| `tune:0,true` | `SWH16;` | `TQ;` | `SWH16` = hold XMIT = TUNE. **Refused at exactly `PC005` or `PC050`** (reply `tune:0,false;`); see below |
| `tune:0,false` | `RX;` | `TQ;` | |
| `drive:0,<0-100>` | `PC<3 digits>;` | `PC;` | Direct percent→watts. 000-110 with KPA3A enabled, 000-012 without |
| `tune_drive:0,<0-100>` | `PC<3 digits>;` | `PC;` | K3 has no separate tune power; set `PC` before `SWH16` |
| `mic_level:<0-100>` | `MG<3 digits>;` | `MG;` | **`MG = round(level * 30/100)`** — cap at 030, not 060. See below |

**TUNE at exactly 5 W or 50 W starts the K3's internal power calibration**
instead of giving a plain carrier. This is the operator's observation of
this radio; the reference does not document it. The bridge reads `PC`
before sending `SWH16` and refuses at 005 or 050. It checks the radio's
value rather than the client's, because the front panel or another client
may have changed it. The UI blocks TUNE at those two settings too, and says
to go up or down 1 W.

`TQ;` is the cheap PTT poll — *"the preferred way to check RX/TX status
since it requires far fewer bytes than an IF response."*

**`TQ1` does not mean RF is out.** It's also returned during pseudo-transmit
— TX TEST, or "pre-armed" for CW via XMIT/PTT — because those states assert
KEY OUT for downstream amplifiers and transverters. Fine for driving the PTT
watchdog; don't report it to the client as confirmed power output.

**Do not poll `BG` or `SW` during transmit.** Reading ALC at 5 Hz — two
commands per 200 ms — made the K3 drop out of transmit after a few seconds.
That is exactly the case the reference warns about: *"Continuous, fast
polling (< 100 ms per poll for bar graph data in transmit mode, for example)
should be carefully tested"* and *"Polling during transmit not be used
unless necessary."* Tried, confirmed harmful, removed. **ALC is therefore
not readable remotely on this radio**; the target on a K3 is around 4-5
bars, read at the front panel. (`TM1` is also required for `BG` to mean ALC
at all — under `TM0` it is RF power on a different scale, and mistaking one
for the other reads as full-scale nonsense.)

**PTT watchdog** (per the design doc's open question): the timer must fire
`RX;` on client disconnect *and* on a stale connection. `TX;`/`RX;` are the
only commands where a dropped WebSocket leaves the radio in a physically
harmful state, so this is the one place to be conservative.

`drive` at 100 maps to `PC100;` (100 W), leaving 101-110 W unreachable. That
is deliberate — a linear 0-100 → 0-110 map would make the client's "100%"
mean 110 W, which is not what an operator expects.

### Metering

**S-meter — use `SMH` (K3-only, high resolution), fall back to `SM`.**

`SMH` returns `SMHnnn;`, 0-140. The reference documents anchors S1 = 5,
S9 = 40, S9+60 = 100, and calls them *"approximate values"*. **On this radio
they are off**, so `bridge/tci.py` uses a measured curve instead:

```
n <= 55:   dBm = -118.71 + 1.222 * n          # 1.222 dB per count
n >  55:   dBm = -51.5 + 0.78 * (n - 55)      # 0.78 dB per count
```

Measured on 2026-09-19 against a Cushman CE-4000 generator, with an
Elecraft P3 as the level reference at every point. Two full sweeps were
run, one with slow AGC and one with fast, both with a 5 s settle, and they
agree within about 2 counts. The two-line fit to both runs is 0.81 dB rms
and 2.05 dB worst from -115 to -20 dBm. S9 (-73 dBm) reads **SMH 37**, and
the slope changes at **SMH 55** (-51.5 dBm), not at 40. The documented
anchors read 3-8 dB low everywhere below -35 dBm. Data and method are in
`tools/README.md`.

Valid for 14.1 MHz, CW, preamp off, attenuator off, RF GAIN at max. Other
bands, the preamp and the attenuator were not measured. The bridge takes
both back out so `rx_smeter` stays a level at the antenna: +10 dB with the
attenuator on (`tci.ATT_DB`, the reference's nominal figure) and -10 dB
with the preamp on (`tci.PRE_DB`, assumed on the operator's decision; no
calibration run is planned). Below about SMH 4
the meter reads the receiver's own noise. Above SMH 96 (-20 dBm) the
upper line is extrapolated, and the radio's overload relay pulls in at
-10 dBm.

`SM` under `K31` returns `SMnnnn;`, 0000-0021, anchors S9 = 9, S9+20 = 13,
S9+40 = 17, S9+60 = 21:

```
n <= 9:    dBm = -73 - 6 * (9 - n)            # 6 dB per S-unit
n >  9:    dBm = -73 + 5 * (n - 9)            # 5 dB per count above S9
```

The `SM` formula is still the reference's anchors, uncalibrated. The same
sweeps fit it poorly with two lines (2-3 dB rms), so it was left alone. The
reference gives **no anchor below S9**, so the 6 dB/S-unit term there is
extrapolation (it puts `SM0000` at −127 dBm). Since `SMH` is primary this
rarely bites, but don't treat the `SM` fallback as calibrated.

**`SM` returns 0000 in transmit mode.** Suppress the S-meter poll while
`TQ1`, which also honors the reference's *"Polling during transmit not be
used unless necessary."*

Poll at **200 ms** in receive (matching AetherSDR's `rx_smeter` tick), which
stays clear of the documented < 100 ms caution.

Note the field width trap: under `K31`, `SM`'s *range* changes to 0000-0021
but the field stays 4 digits. A parser keyed on width will silently read the
wrong scale. Since we always send `K31;` at startup this is settled — but
assert it rather than assume it.

| TCI | K3S GET | Conversion | Notes |
|---|---|---|---|
| `rx_smeter:0,<dbm>` | `SMH;` (fallback `SM;`) | measured curve above (`SM`: piecewise) | 200 ms poll, RX only |
| `tx_sensors:0,...` | `TQ;` `BG;` `SW;` | see below | ≤ 5 Hz poll, TX only |

**`tx_sensors` is partial on a K3S and will stay that way.** TCI wants
`mic, fwd, peak, swr, alc`. Available:

- **SWR** — `SW;` returns `SWnnn;`, tenths, 010-999 (1.0:1 to 99.9:1).
  Works during TX, TUNE and ATU tuning. This one is exact.
- **Forward power** — no true reading. `PO` is KX3/KX2 only. `BG;` gives a
  *bargraph* level (00-12 in PWR mode), not watts. Report the `PC` setpoint
  and mark it as a setpoint, or scale `BG`; either way it is not measured
  forward power.
- **ALC** — `BG` reports ALC only when `TM1` is set, and **`TM` switches the
  front-panel LCD meter too**. Toggling `TM` between polls to collect both
  PWR and ALC visibly thrashes the operator's meter. Pick `TM0;` (RF/SWR),
  report ALC as 0, and say so.
- **Mic level** — no read-back command; echo the `MG` setpoint.

This is also the only place the bridge polls during transmit, so keep it
slow and cite the caution above.

### Receive audio and DSP

| TCI | K3S SET | Read back | Conversion | Notes |
|---|---|---|---|---|
| `volume:<dB>` | **none — software gain** | — | `gain = 10^(dB/20)`, dB ∈ −60…0 | `AG` does **not** work; see below |
| `mute:0,<bool>` | **none — software gain 0** | — | — | Same reason |
| `mon_volume:<0-100>` | `ML<3 digits>;` | `ML;` | `ML = round(v * 60/100)` | 000-060, applies to the current mode |
| `sql_level:0,<0-100>` | `SQ<3 digits>;` | `SQ;` | `SQ = round(v * 29/100)` | 000-029 |
| `sql_enable:0,false` | `SQ000;` | `SQ;` | — | No on/off command; 000 = open. Cache the level. Verified: `sql_level:0,34` + enable wrote `SQ010` and read back 34 |
| `agc_mode:0,slow` | `GT004;` | `GT;` | — | The front-panel AGC key is reported by AI2 as `GT`, and re-broadcast |
| `agc_mode:0,fast` / `med` | `GT002;` | `GT;` | — | K3 has only fast/slow |
| `rx_nb_enable:0,<bool>` | `NB1;` / `NB0;` | `NB;` | — | `NB0` overrides any non-zero `NL`; and `NB1` with `NL0000` blanks nothing, so the bridge also broadcasts `nb_levels` |
| `preamp:0,<bool>` (bridge's own) | `PA1;` / `PA0;` | `PA;` | — | Reported on band change and by AI2 |
| `attenuator:0,<bool>` (bridge's own) | `RA01;` / `RA00;` | `RA;` | — | One 10 dB pad on this K3 |
| `if_shift:0,<Hz>` / `if_shift:0,norm` (bridge's own) | `IS <4 digits>;` / `IS 9999;` | `IS;` | Hz, the AF centre | SHIFT, in the radio's own terms. Broadcast with every `rx_filter_band`, because in CW the TCI edges are always `±half` and cannot show a shift. Measured clamps: CW 100-1050, NORM = PITCH (590 here); USB with 2.4k width 400-2600, NORM 1500, the edges moving with it (400 → -800..1600). AM/FM not measured; the web UI disables SHIFT there |
| `xfil_tap:0` (bridge's own) | `SWT29;` | `XF;`, then `BW;`/`IS;` | `XFn` = FL1-FL5 | Broadcast as `xfil:0,<n>,<Hz>` plus `rx_filter_band`; the width is from `tci.XFIL_WIDTHS` (this station: FL3 2.7k, FL4 500, FL5 250), because CAT cannot report what is fitted. **Each crystal resets the DSP width to suit itself** (measured in CW: FL4 400 Hz → FL5 250 → FL3 2700 → FL4 500, not back to 400). In CW the tap cycles FL4 → FL5 → FL3 only. AI2 reports no `XF`; the reconcile sweep reads it. **The reverse holds too: a `BW` change picks the narrowest crystal at least as wide** (measured in CW: 2700 and 1000 → FL3; 500, 400, 300 → FL4; 250, 100 → FL5), so the web UI's width slider drives the crystal and XFIL is an override |
| `nr_tap:0` / `notch_tap:0` (bridge's own) | `SWT34;` / `SWT32;` | `DS;` byte `f` | — | No NR or notch command exists, so these press the switch; the result is read back and broadcast as `rx_nr_enable` (TCI's) and `notch:0,off\|auto\|manual` (the bridge's own). AI2 reports neither, so front-panel presses arrive via the 3 s reconcile sweep. Refused while transmitting |
| `rx_nb_param:0,0,<0-100>` | `NL<dd><ii>;` | `NL;` | scale to 00-21 each | `dd` = DSP NB level, `ii` = IF NB level |
| `lock:0,<bool>` | `LK1;` / `LK0;` | `LK;` | — | VFO A lock; `LK$` is VFO B. **Locks the knob only: an `FA` SET still moves VFO A with `LK1` set** (measured). The web UI refuses its own tuning while locked; the bridge passes CAT through, as the radio does |

**`AG` does not affect the USB audio at all.** An earlier draft of this table
mapped `volume` to `AG`, on the reasonable assumption that the AF gain
control sets the audio level. It does not. Measured, sweeping `AG` while
recording from the codec:

| `AG` | capture RMS |
|---|---|
| 000 | −62.5 dBFS |
| 050 | −62.3 |
| 120 | −62.3 |
| 200 | −62.1 |
| 250 | −61.9 |

0.6 dB across the entire range — that is drift, not control. The K3's LINE
OUT is a fixed-level tap ahead of the AF gain stage, which is correct
behaviour for a line output and exactly what you want for a bridge.

**The hardware control is `LIN OUT` (menu 032)**, which is `MP`-readable and
has real range:

| `LIN OUT` | capture RMS |
|---|---|
| 000 | −85.9 dBFS (off) |
| 005 | −68.5 |
| 010 | −62.5 ← as found |
| 020 | −55.9 |
| 030 | −52.8 |
| 040 | −50.2 |

But it is the wrong place to implement `volume`: every change needs
`MN032` / `MP` / `MN255`, which is three CAT round-trips and puts the radio
into its menu — visibly, on the front panel — for every movement of a
client's volume slider.

**So `volume` and `mute` are applied in software**, as a float multiply on
the samples before framing. TCI's `volume` is a global master volume, so one
shared gain is correct. Verified: `volume:-20` measured −19.5 dB and
`volume:-40` measured −39.8 dB, and `mute` gives exact digital zero.

`LIN OUT` remains the *calibration*: set it once so that strong signals use
the ADC range without clipping, then leave it. **Now measured**, against
WWV on 10 MHz AM with a real antenna:

| `LIN OUT` | RMS | peak |
|---|---|---|
| 005 | −31.8 dBFS | −20.8 dBFS |
| 010 | −32.2 | −17.0 ← as found |
| 020 | −25.9 | −12.3 ← **chosen** |
| 030 | −19.6 | −9.0 |
| 040 | −15.9 | −4.6 |
| 050 | −16.3 | −4.2 |
| 060 | −16.6 | **−0.0 — clipping** |

Note the signature of clipping above 040: RMS stops rising (−15.9, −16.3,
−16.6 — it even falls) while peak pins at 0 dBFS. That is the ADC running
out of range, not more signal.

**Set to 020**, which gives about 12 dB of peak headroom at a healthy
−25.9 dBFS RMS. The AGC does the levelling, so this holds across signal
strengths; the headroom is there for mode and AGC-setting differences.
The as-found 010 was ~6 dB quieter for no benefit.

### CW

| TCI | K3S SET | Read back | Notes |
|---|---|---|---|
| `cw_keyer_speed:<wpm>,0` | `KS<3 digits>;` | `KS;` | K3 range is 008-050; TCI allows 5-100 — clamp and echo the clamped value |
| `cw_macros_speed:<wpm>,0` | `KS<3 digits>;` | `KS;` | Same register as above |
| `cw_msg:<text>` | `KYW<text>;` | `KY;` | **24 characters max per command** — chunk longer text |
| `cw_macros:<text>` | `KYW<text>;` | `KY;` | Same |
| `cw_macros_stop` | `RX;` | `TQ;` | Ends the transmission; does NOT interrupt it — see below |
| `keyer:0,<bool>` | `TX;` / `RX;` | `TQ;` | Straight key-down has no CAT equivalent; approximate with PTT |

**`VX1` (CW VOX) is a precondition for `KY` keying, and its absence fails
silently.** In CW mode "VOX" means *hit-the-key transmit* — the operator
does not have to assert XMIT or PTT first. With `VX0` the K3 accepts `KY`
text into its buffer and then never transmits: no `?;`, no error, nothing on
the air. Confirmed on the air.

`VX` is stored per mode, so enabling it for CW does not disturb voice VOX.
This sits alongside `MIC+LIN` (TX audio) as the second precondition whose
only symptom is silence.

**The bridge keys with `TX;` rather than relying on VOX**, and drops it with
`RX;` queued behind the last chunk — the `W` form defers following commands
until the message has been sent, so the unkey lands after the last element
instead of cutting it off. That makes the T/R transition deterministic
rather than a side effect of the first character arriving, and it means the
bridge's transmit state is true while CW is going out, which every loop that
must not poll a transmitting radio depends on.

VOX remains the fallback: if `TX;` does not take, the bridge enables `VX1`
and sends the text the old way rather than into silence. Nothing here waits
for the message to finish — this runs under the lock that serialises every
client's commands — so the PTT watchdog is armed with a generous estimate
(PARIS timing, doubled, plus ten seconds) in case the queued `RX;` is lost.

Use the `W` ("wait") form — `KYW<text>;` — not `KY <text>;`. It defers
processing of following commands until the message has been sent, which
matters because we may send `KS` (speed) right behind it.

`KY;` GET returns buffer state: 0 = not full, 1 = full. Poll it before
sending the next chunk. Note that the radio DOES answer `KY;` while it is
playing a message, even though it defers other commands — which is what
makes flow control possible at all.

**`RX;` does not interrupt a message in progress.** Measured: a 22-character
message stopped three seconds in finished at 8.6 s; the same message with no
stop at all finished at 9.7 s. The `W` form defers `RX;` behind the `KY`
buffer exactly as it defers everything else, so the stop ends the
transmission rather than interrupting it. The bridge therefore implements
stop as "abandon the chunks not yet written", which bounds what cannot be
taken back at one chunk — 24 characters.

Prosign mapping the K3 accepts inside `KY` text: `(`=KN, `+`=AR, `=`=BT,
`%`=AS, `*`=SK, `!`=VE. Pass client text through unmodified; these are the
documented escapes if a client wants them.

### Text decode

| TCI | K3S GET | Notes |
|---|---|---|
| `rx_text:0,<text>` * | `TB;` | Decoded CW/RTTY/PSK. **Not a TCI command** — see below |

\* TCI has no message for decoded text, so this one is the bridge's own.
Clients that do not know it ignore it, which is the whole reason for
choosing a message on the existing socket over a second channel.

`TB` response is `TBtrrs;` — `t` is the count of TX characters still to be
sent (0-9, saturating), `rr` the count of RX characters available (00-40),
and `s` exactly `rr` characters of text. The empty reply is `TB000;`.

**Frame it by `rr`, not by the terminator.** Semicolons are legal in decoded
RTTY and PSK, and the reference says so explicitly: the count exists *because*
the text can contain the character everything else uses as a delimiter. A
reader that stops at the first `;` returns a truncated reply and hands the
tail to the unsolicited path, where a fragment like `" DE;"` reads as the
real command `DE`. Covered off-radio by `bridge/tbframetest.py`.

**A `TB` GET that times out has to stay accounted for.** A band change
defers command handling for up to 500 ms (global rule 4), which is inside
the request timeout, so the reply can land while the *next* poll is already
waiting. Delivering it there answers that poll with the previous poll's
text and leaves the reply that really belongs to it to be framed on `;` —
both failures at once. Count the replies still owed rather than tracking a
single "expecting one" flag: keep framing by count so a semicolon still
cannot reach the command stream, and drop everything except the newest.

**Reading is destructive.** The radio clears its RX count as it answers, so
whatever a `TB` GET returns is the only time anyone will see it. Exactly one
poll loop may call it. (The radio's own VFO B display is driven separately
and is not consumed by `TB`.)

**The buffer holds 40 characters** and the reference requires polling "often
enough to prevent loss of incoming text". 40 characters is roughly 12 s of
40 WPM CW, so the interval has headroom — but it is a deadline, not a
quality setting. The bridge polls at 3.3 Hz, dropping to 0.67 Hz after 5 s
with nothing decoded and restoring on the first character.

**Text decode is enabled at the front panel and has no CAT command.** Hold
**TEXT DEC** and select `CW 5-40` with VFO B; a `T` appears under the `CW`
icon. `TT` (text-to-terminal) is *not* a substitute — it enables a raw ASCII
stream that is not `;`-framed at all, and the reference itself recommends
`TB` over it, mandatorily so if a P3 is in the CAT path.

**`TB000;` is ambiguous and cannot be disambiguated.** It is what a radio
with text decode switched off returns, and equally what a radio with text
decode on and a quiet band returns. Nothing in CAT separates them. Report
the ambiguity rather than guessing at it — the web UI says both.

Not polled during transmit, for the same reason as the S-meter. The cost is
that CW you send yourself is read back only after you unkey, and a
transmission longer than 40 characters loses its beginning.

### Init block values

| TCI init command | Value | Rationale |
|---|---|---|
| `protocol` | `ExpertSDR3,1.5` | Matches what AetherSDR advertises and what TCI Remote accepts |
| `device` | `Elecraft K3S` | |
| `receive_only` | `false` | |
| `trx_count` | `1` | Rule 6 |
| `channels_count` | `2` | VFO A = channel 0, VFO B = channel 1. Note the plural spelling — AetherSDR deviates from the spec PDF's singular `CHANNEL_COUNT` deliberately, to match the `ars-ka0s/eesdr-tci` reference implementation |
| `vfo_limits` | `100000,54000000` | K3 covers 500 kHz-30 MHz + 48-54 MHz (100 kHz low end with KSYN3A). TCI wants one contiguous range, so advertise the envelope and let read-back correct out-of-band requests |
| `if_limits` | `-9999,9999` | Tied to the RIT/XIT offset range, since `if:` maps to RIT |
| `modulations_list` | see below | |
| `iq_samplerate` | `48000` | Advertised but unused — no IQ in v1 |
| `audio_samplerate` | `48000` | |
| `tx_profiles_ex` | (per spec) | |

Then `ready;` — **after** every setting, never before. Some clients latch
cached settings on READY. Then `start;`.

**`modulations_list`:** the design doc records TCI Remote's UI set as LSB,
USB, DSB, CWL, CWU, NFM, AM, SAM, DIGL, DIGU. Advertise only what we can
actually honor:

```
modulations_list:lsb,usb,cwl,cwu,nfm,am,digl,digu;
```

DSB and SAM are omitted — see "Unmappable in v1". Advertising a mode whose
control does nothing is worse than having it grayed out.

⚠ The design doc notes TCI Remote's spellings are uppercase and *"must echo
back exact same spelling"*, while TCI on the wire and AetherSDR's
implementation both use lowercase. Keep the inbound parser
case-insensitive and alias-tolerant (accept `cwr` for `cwl`, `cw` for
`cwu`), and settle the outbound spelling against the real client.

---

## Unmappable in v1

Recorded so the gaps are explicit rather than implied by absence.

- **`DSB`** — no K3 equivalent. Omitted from `modulations_list`.
- **`SAM` (AM-Sync)** — the K3 has AM-Sync, but it is only *readable*, via
  `IC` byte `d` bit 3. No documented command sets it. Since v1 skips `IC`
  entirely (rule 7), this is not even observable. Omitted.
- **`agc_mode:off`** — needs `GTnnnx;`, which needs `K22`. Rejected in rule
  1. Echo the current value back rather than silently accepting `off`.
- **True forward power** — `PO` is KX3/KX2 only; `BG` is a bargraph. The
  `tx_sensors` `fwd` field carries the `PC` setpoint, not a measurement.
- **ALC** — requires `TM1`, which hijacks the front-panel meter. Reported
  as 0.
- **Independent RIT and XIT offsets** — one shared `RO` register.
- **`iq_start` / `iq_stop` / IQ frames** — left unimplemented per the design
  doc. Most TCI clients degrade to "no spectrum" gracefully.
- **Sub receiver (KRX3A)** — deferred to trx 1 in a later version. The `$`
  command variants (`MD$`, `BW$`, `AG$`, `SM$`, `PA$`, `RA$`, `NB$`, `NL$`,
  `SQ$`, `RG$`, `BN$`, `LK$`, `XF$`) are all documented and available when
  that happens.

---

## Verified on hardware

Bench session against the radio, via the Pi at 38400 baud. Firmware
`RVM05.67`, radio on 20 m CW at the time.

- **`ID017;`** returned as documented.
- **Modem control lines are NOT irrelevant, and this note used to say they
  were.** All four DTR/RTS combinations worked in that session, and the
  conclusion drawn — that the bridge needn't manage them — was true only
  because the radio had both lines switched off at the time. The K3 can be
  told to read DTR as KEY and RTS as PTT (its RS232 menu), and pyserial
  asserts both lines by default when it opens a port. With DTR=KEY set at
  the radio, starting the bridge is therefore a key-down that holds: a
  carrier on the air from the moment the service starts, which at this
  station took unplugging the USB lead to stop. The bridge now opens the
  port with both lines low, before the port is open, so it is safe whatever
  the radio is set to.

  A caveat the fix does not cover: the tty layer raises DTR/RTS as the
  device is opened, and pyserial lowers them immediately after, so a brief
  assert at open is possible even now. That is a driver-level behaviour, not
  something Python can get in front of. It is the difference between a held
  carrier and a blip; with DTR=KEY intended for use, confirm it with TX TEST
  on before trusting it.

  Driving PTT from RTS is worth considering for one reason: it fails safe.
  If the bridge process dies the line drops and the radio unkeys, where
  `TX;` needs something still alive to send `RX;`. Keying CW from DTR is a
  different matter — that would put CW element timing in Python on the Pi,
  fed by a WebSocket, rather than in the radio's own keyer.
- **The `IF` response decodes at every documented offset — but its LENGTH
  is not fixed.** The bench sample was 38 characters,
  `IF00014030000     -000000 0003000011 ;`, ending with a space before the
  terminator. The same radio in normal operation returns **37**,
  `IF00007020880     +000000 0003000001;`, with no space. Both decode
  identically: freq, mode 3, RIT/XIT flags, RX, split, data sub-mode all sit
  where the layout table says.

  **Do not length-check against 38.** `refresh_if` did, and it therefore
  rejected every reply and returned without updating anything — for months,
  invisibly, because frequency and mode arrive over the AI2 unsolicited
  stream instead. Only the fields with no AI2 path (split, RIT/XIT) were
  affected, and they sat frozen at their startup values while the radio
  moved. Check that the response is long enough for the fields being read
  (all at index 32 or below), not that it matches a remembered total.

  Parse by fixed offset with confidence; do not trust the total length.

  **There were two of these guards, and fixing one left the other.**
  `on_cat_event` checked `len(msg) >= 38` on the *unsolicited* `IF` and so
  had never run at all. That one hid even better than the first: a band
  change auto-reports `FA` and `MD` alongside `IF`, and those branches
  worked, so frequency and mode tracked while split and the TX flag waited
  on the 3 s reconcile to notice. If a third appears, the same rule
  settles it — ask whether the fields being read are present. Both are
  covered by `bridge/tbframetest.py`, which fails on the 37-character form
  and passes on the 38-character one, which is precisely why the bug was
  invisible.
- **`K31;` takes effect and the `IF` `d` field tracks `DT`** (both read 1).
- **`RO` can return a negative zero**: the radio reported `RO-0000;`. The
  sign character is independent of the magnitude, so a parser that keys on
  the sign to decide direction must special-case zero.
- **`IS` returns with the literal space** — `IS 0600;`, as warned.
- **The CW filter rule is right.** In CW with `BW0050;` (500 Hz) the radio
  reported `IS 0600;` — an AF center of 600 Hz, matching the CW PITCH, not
  a carrier-relative zero. This is live confirmation that deriving `IS`
  from TCI's `lo`/`hi` in CW would be wrong.
- **`RA` uses the legacy K3 format on this radio.** Probed directly:
  `RA05`, `RA10` and `RA15` all read back as `RA01;`. The attenuator is a
  single 10 dB pad, not the K3S stepped attenuator. (Original setting was
  restored afterward.)
- **`NL` format confirmed** as `dd`+`ii`: `NL0001;` = DSP NB 00, IF NB 01.

### Filter-panel switches and band select — 2026-09-19

- **`GT`, `PA`, `RA`, `NB`, `RT`, `XT`** each SET and read back in the
  format the bridge parses (`rxtest.py`): `GT002;`/`GT004;`, `PA0;`/`PA1;`,
  `RA00;`/`RA01;`, `NB0;`/`NB1;`.
- **PRE and ATT pressed at the radio** reach the web UI unprompted, through
  AI2 — the operator watched both follow.
- **`SWT34` (NR) and `SWT32` (NTCH)** each tap the switch and cycle it,
  confirmed by ear by the operator. Their state is now read from `DS`
  (see rule 7), checked on the radio: NR tapped twice read on, then off;
  NTCH in CW read **manual**, then off -- CW has no auto notch, so the
  switch goes straight to the manual one.
- **`BN00`-`BN10`** each land inside the band `tci.BANDS` gives them, and an
  off-band `FA` sticks as that band's memory (`bandtest.py`; see the note
  under *Frequency and VFO*).

### TX audio path — confirmed, with two preconditions

USB audio does reach the modulator. Measured in DATA A (`MD6`/`DT0`) in
TX TEST mode, so no RF, comparing **silence against a tone at the same mic
gain** — without that control the measurement is worthless (see below).

| MG | silence | 1500 Hz tone at −3 dBFS |
|---|---|---|
| 005 | 0 0 0 0 0 0 | 5 7 6 6 6 6 |
| 010 | 0 0 0 0 0 0 | 7 6 6 6 6 7 |
| 020 | 0 0 0 0 0 0 | 6 7 6 7 6 6 |
| 030 | 0 0 0 0 0 0 | 6 6 6 6 6 6 |
| 034 | 0 0 0 3 5 5 | 6 7 6 7 5 7 |

ALC bars, 0–7, under `TM1;`. Tone-level response at MG034: −40 dBFS → 0,
−30 → 5, −20 → 5, −12 → 5, −6 → 6, −3 → 7. Monotonic, so this is a real
audio response and not an artifact.

**Precondition 1 — the ALSA playback mixer must be up.** The PCM2901's
`PCM` control ships at 82% (−23 dB), which attenuated a −20 dBFS tone to
about −43 dBFS at the radio. Set it to 100% and calibrate with `MG`
instead:

```
amixer -c 2 sset PCM 100%
```

**Precondition 2 — `MIC+LIN` (menu 015) must be ON.** It is the *enable*
for LINE IN, not a "sum the mic in" switch. Measured with the mixer at 100%
and mic gain in the usable range, in DATA A:

| `MIC+LIN` | silence | −6 dBFS tone |
|---|---|---|
| ON | 0 0 0 0 | 5 5 5 5 |
| OFF | 0 0 0 0 | **0 0 0 0** |

Nothing reaches the modulator with it off, at any mic gain. This is worth
knowing because switching it off is the obvious way to keep the microphone
out of the TX path for digital modes — and it does not work; it just
silences you.

**The microphone is therefore always live in the TX path**, which creates an
acoustic feedback loop if the transmit monitor is up: monitor → speaker →
mic → transmitter → monitor. It howls. Observed on the air. The bridge
mutes the monitor (`ML000`) on entering a digital mode; the alternatives are
to keep `MON` at zero manually or unplug the mic.

**Cap `MG` at 030.** Above roughly MG034 the mic-path noise floor alone
lifts the ALC — the silence column above shows it starting at 034. For a
remote station that means transmitting shack noise between overs. Since
full ALC is reached at MG as low as 005 with the mixer at 100%, the whole
useful control range sits below 030, which is why the `mic_level` row maps
to 0–30 rather than the full 0–60 the command accepts.

### What WSJT-X actually sends

Captured from WSJT-X 3.0.1 over TCI, in order, on connect and on Tune:

```
split_enable:false          <- NO trx index. See below.
audio_start:0
rx_sensors_enable:false,500
tx_sensors_enable:false,500
trx:0,true,tci              <- keys, naming "tci" as the audio source
trx:0,false,tci
```

**`split_enable:false` arrives with no trx index.** A handler that requires
two arguments ignores it silently, and the radio keeps whatever split state
it had while the client believes it has cleared it. Accept both the
`split_enable:<bool>` and `split_enable:<trx>,<bool>` forms.

**It reconnects on its own** about two seconds after a server restart, so
restarting the bridge is not disruptive to it.

**Measured TX audio, one Tune:** 2057 blocks against 2057 chrono requests
over 43.87 s — exact lockstep, 100% coverage, zero dropped, zero clipped,
48 kHz float32. The chrono clock ran at 46.89/s against a 46.88/s target.

Note its output sits at **digital full scale** (peak 0.0 dBFS, rms −3.1),
which leaves no headroom in the float32→int16 conversion. Scale it down at
the source with WSJT-X's own Pwr slider rather than only reducing `MG`:
lowering mic gain fixes the transmitter's drive but leaves the digital
signal clipping-adjacent.

### Sideband polarity — measured, not assumed

Method: WWV radiates a continuous carrier on exact MHz boundaries. Tune the
K3 a known offset off that carrier, capture the receiver's audio from the
codec, and measure FFT energy at the frequency the hypothesis predicts. No
listening required, and no human judgement in the loop. Script:
`~/k3bridge/wwvtest2.py` on the Pi.

Run against WWV 10 MHz. **USB and LSB were run first as controls**, since a
"no tone" result is meaningless if the method itself isn't discriminating:

| Mode | dial 1 kHz low | dial 1 kHz high | verdict |
|---|---|---|---|
| `MD2` USB (control) | 58.7 dB | 10.0 dB | UPPER ✓ as expected |
| `MD1` LSB (control) | 17.3 dB | 55.6 dB | LOWER ✓ as expected |
| `MD6` DATA | 63.7 dB | 9.2 dB | **UPPER** → `digu` |
| `MD9` DATA-REV | 12.9 dB | 55.3 dB | **LOWER** → `digl` |

(dB figures are energy at the predicted 1000 Hz carrier beat, above the
audio-band noise floor. Contrast is 38-54 dB in every row.)

CW was tested differently, since its passband straddles the carrier: with
the dial 200 Hz off, an upper-sideband CW mode beats at pitch+200 and a
lower-sideband one at pitch−200. Pitch was 600 Hz, so 800 vs 400 Hz. Each
mode was tested at both dial offsets, giving two independent votes:

| Mode | dial low: 800 / 400 Hz | dial high: 800 / 400 Hz | votes | verdict |
|---|---|---|---|---|
| `MD3` CW | 68.7 / **111.9** | **115.5** / 60.2 | LOWER, LOWER | **LOWER** → `cwl` |
| `MD7` CW-REV | **116.0** / 58.7 | 61.8 / **114.4** | UPPER, UPPER | **UPPER** → `cwu` |

Two methodological notes, because the first attempt at this got both
wrong and looked plausible doing it:

- **Verify every mode SET by reading it back.** The first run's `MD2;`
  silently didn't take, leaving the radio in AM, and the AM audio looked
  enough like a result to produce a confident, wrong answer.
- **Measure energy at the predicted frequency, not the strongest peak.**
  WWV is amplitude-modulated with 500/600 Hz tones and a 100 Hz time code,
  and those routinely exceed the carrier beat. A global peak search finds
  WWV's modulation and reports it as signal.

Two operational notes from the same session:

- **`PC000;`** — the power setpoint is currently zero. Not a bridge problem,
  but any TX test will produce no output until it's raised.
- **RX audio is effectively mono.** A 3 s capture from the codec gave
  −44.9 dBFS on the left channel (band noise) and −85.1 dBFS on the right —
  the right channel is the sub receiver, which isn't installed. TCI's audio
  frames are stereo interleaved float32, so **the bridge must duplicate the
  left channel into both TCI channels**, or clients will hear audio in one
  ear only.

## Verify on hardware before shipping

Everything below is a coin-flip that fails **silently** — wrong sideband,
no error, no log line. Each needs one test against the real radio.

Sideband polarity for CW and DATA is **settled** — see above. Remaining:

3. ~~**Mode-string case on the wire.**~~ **Settled — case does not matter.**
   Read directly out of TCI Remote v3.5's browser build: its handler does
   `S.mode = args[1].toUpperCase()`, so it normalises whatever spelling it
   receives. And `modulations_list` appears *only* in its known-command
   whitelist with no handler at all — the mode buttons are hardcoded, so
   the advertised list is never consulted. Lowercase output is fine, and
   omitting DSB/SAM from the list does not hide those buttons (they are
   present in the UI regardless; selecting one just won't map).

4. ~~**`AG` volume taper.**~~ **Does not apply.** `AG` does not affect the
   USB audio at all (see *Receive audio and DSP*), so `volume` is software
   gain and there is no taper to verify.

5. ~~**Split.**~~ **Settled 2026-09-19 — `FT1;` enables, `FR0;` clears.**
   It was recorded as "enabling does not take" on 2026-08-13, while
   `refresh_if` was still rejecting every 37-character `IF` reply, so the
   split field could never have read anything but its startup value. With
   that fixed, `split_enable:0,true` reads back split on and `false` reads
   back off. `FR0;` is the cancel the bridge uses and it works; `FT0;` (the
   reference's macro) was not needed.

6. ~~**`rx_filter_band` round-trip in each mode.**~~ **Settled 2026-09-19**
   by `filtertest.py`. USB, LSB, CW and DATA write and read back exactly
   (USB 300..2700 and 300..1800, LSB -2700..-300, CW -350..350 and
   -200..200, DATA 300..2700). CW stays symmetric about the carrier, so
   `IS` is correctly left alone there. AM clamps a 6000 Hz request to 5000.
   A mode change re-broadcasts the passband.

**RF GAIN (`RG`) is inverted, and reducing it PINS the S-meter.**
`RG000`-`RG250`, clamping at 250, and **`RG250` is MAXIMUM gain** — the
opposite of the intuitive guess. Worse, the K3's S-meter reads the AGC line
and manual RF-gain reduction pulls that line up, so backing the gain off
drives the meter *upward* on a dead band. Measured with no signal present
(service monitor in receive, P3 confirming a -140 dBm floor):

| `RG` | `SM` | `SMH` |
|---|---|---|
| 250 | 2 | 11 | true noise floor — max gain |
| 214 | 8 | **40** | |
| 000 | 19 | 91 | meter pinned by gain reduction |

**`RG214` on a dead band reads `SMH 40`, which is exactly the documented S9
count.** A calibration run left at the station's resting RF-gain setting
would have produced a stable, convincing, entirely fictitious S9 at the
noise floor. Any measurement that reads the S-meter must assert `RG250`
first. `RG` was previously undocumented here, appearing only as a label in
`tools/k3state.py`.

**A stale CAT reply can surface as the first read after opening the port.**
Every batch of S-meter reads in the calibration session contained exactly
one outlier, and it was always the *previous* measurement's value — a 91
turning up in the batch after `RG000`, a 37 in the batch after the 50 uV
point. A median over a dozen reads absorbs it and the range-check in
`bridge/tci.py` would reject an out-of-range one, but a tool taking a
single sample will occasionally return the last measurement instead of the
current one.

7. **The S-meter → dBm conversion needed a signal generator. `SMH` now has
   one; `SM` does not.** Both formulas were first anchored on the
   reference's documented points, and neither could be verified without a
   known source.

   Two attempts to verify them against the one precisely known step this
   radio can produce — the 10 dB attenuator, `RA00` vs `RA01` — gave
   irreconcilable answers:

   | | `SM` step | `SMH` step |
   |---|---|---|
   | attempt 1 | 3 counts | 14 counts |
   | attempt 2 | 1 count | 4.5–6 counts |

   Same 10 dB, same radio, minutes apart. The source was WWV on 10 MHz,
   and HF fading moved the signal more than the step being measured.
   Attempt 2 would imply 10 dB per `SM` count, which directly contradicts
   the reference's own anchors (5 dB/count above S9), so it is the
   measurement that is wrong, not the documentation.

   **Do not calibrate against a fading sky-wave signal.** It took a signal
   generator. The `SMH` curve in *Metering* above is the result, measured
   on 2026-09-19 with the P3 as the level reference. It showed the
   documented anchors reading 3-8 dB low. `SM` is still uncalibrated.
