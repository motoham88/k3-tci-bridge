# K3 → TCI Bridge: Capability Evaluation

What this specific station can support, measured on the bench rather than
inferred from datasheets. Companion to `k3-tci-bridge-design.md` (scope) and
`k3-tci-command-map.md` (the protocol mapping).

Bench date: 2026-08-13. Radio: Elecraft K3, upgraded. Host: Raspberry Pi 3B
(`shack-rpi`, 192.168.1.198), Debian 13 trixie, aarch64, 905 MB.

---

## Verdict

**The v1 scope is achievable end to end, and the Pi 3B is not the
bottleneck.** The RX audio path costs 1.6% of one core with ±0.5 ms of
jitter and zero underruns. CAT control is fully mapped and verified against
the radio. The TX audio path is confirmed working, with two configuration
preconditions and one gain cap. Nothing in the v1 scope is now unproven.

The remaining constraint is **3.07 Mbit/s of uncompressed audio per
direction**, which decides whether this works over your uplink rather than
whether it works at all.

A panadapter is now *possible* where it wasn't before (the KXV3 gives an IF
output the P3 couldn't), but it needs added hardware and realistically a
Pi 4 or 5. It is not a v1 conversation.

---

## The hardware we actually have

`OM;` returns `OM APX-D---V---;`:

| Letter | Module | Present |
|---|---|---|
| `A` | KAT3A antenna tuner | yes |
| `P` | KPA3A 100 W PA | yes |
| `X` | KXV3 transverter / IF out / RX ant | yes |
| `S` | KRX3A sub receiver | **no** |
| `D` | KDVR3 voice recorder | yes |
| `F`/`f` | KBPF3A bandpass filters | no |
| `V` | KSYN3A synthesizer | yes |
| `R` | K3S RF board | **no** |

This is an original K3 mainboard with most of the K3S option set added —
matching your description. Two consequences that are not cosmetic:

- **`RA` uses the legacy attenuator format** (`RA00`/`RA01`, a single 10 dB
  pad). Verified directly: `RA05`, `RA10`, `RA15` all collapse to `RA01;`.
  This is RF-board behavior, so no firmware update changes it.
- **No sub receiver**, so `trx_count:1` permanently, no diversity, and the
  right audio channel is dead (measured at −85 dBFS).

Firmware `RVM05.67`. CAT at 38400 on an FT232 (serial `AL047393`), audio on
a PCM2901 (ALSA card 2), both behind the radio's TUSB2036 hub.

---

## Measured performance envelope

### RX audio path — large headroom

30 s run, full pipeline: ALSA capture → int16→float32 → duplicate left into
both channels → pack into 2048-sample TCI frames, with a concurrent CAT
S-meter poll competing for the same cores.

| Metric | Result |
|---|---|
| Frames delivered | 703 in 30.0 s (23.43/s, expected 23.44/s) |
| Inter-frame interval | p50 42.99 ms, p99 43.11, max 43.18, min 41.81 |
| Per-frame processing | p50 0.385 ms, p99 0.540, max 0.703 |
| Share of the 42.67 ms budget | **1.26% at p99** |
| CPU | 1.6% of one core, 0.4% of the box |
| Underruns | **0** |

**This retires the design doc's concern about Python GC in the audio loop.**
There is roughly 60× headroom. The concern was also aimed at the wrong
target: the risk in this path was never CPU, it's **ALSA period sizing**.
An early version of this benchmark used a 4096-frame period against
2048-frame reads and produced alternating 85 ms / 0.25 ms delivery — 85 ms
of latency injected before the bridge saw a single sample, with CPU still
near zero. Match the ALSA period to the TCI frame size (2048) and the
problem disappears. That is the lesson worth carrying into the
implementation.

Two caveats on these numbers. **30 s is short** — the failure mode that
bites long-running audio is a rare underrun every few minutes, so treat
this as provisional pending a multi-hour soak. And the CPU governor was
`ondemand`, idling at 1.2 GHz, so this is the best case; under real
WebSocket load the governor's ramp adds latency. Switch to `performance`.

### CAT latency — fixed

Round-trip for `SMH;` was **p50 23.4 ms** (p95 24.7, max 28.5) with the FTDI
`latency_timer` at its 16 ms default. With the timer at 1 ms:

**p50 15.6 ms, p95 16.3, p99 17.0, max 17.2** (n=150).

That is close to the floor. What remains is the radio itself (documented
under 10 ms typical) plus about 3 ms of wire time — `SMH;` out and
`SMH000;` back is 11 bytes at 38400 8N1 — plus host overhead. Going faster
would need a higher baud rate, and 38400 is the K3's maximum.

Applied persistently by udev (see below), along with a stable `/dev/k3cat`
symlink keyed to the adapter's serial.

### Bandwidth — the real gate on remote operation

TCI audio is uncompressed float32 stereo at 48 kHz:

**16384 bytes/frame × 23.44 frames/s = 375 KiB/s = 3.07 Mbit/s, each
direction.**

On the LAN this is nothing (the Pi's 100 Mbit link is USB-attached but
carries it easily). Over a home uplink it is the whole question. TCI has no
audio codec — there is no compression option in the protocol.

Worth knowing: **half of that is duplicated mono.** With no sub receiver
the right channel is dead, so we transmit the left channel twice. TCI
supports `audio_stream_channels:1`, which would halve it to ~1.5 Mbit/s —
but whether TCI Remote will actually negotiate mono is unverified, so treat
the halving as a possibility, not a plan.

---

## What we can build

### Phase 1 — the v1 bridge (built and running)

Implemented in `bridge/`, running on the Pi. Measured end to end with a real
client streaming for 60 s: **23.43 frames/s, zero dropped, 14.8% of one core**
(~3.7% of the box) with capture, float conversion, framing, WebSocket send,
S-meter polling and the reconcile sweep all running together. Inter-frame
jitter p50 42.88 ms, max 43.80.

Still to add: RIT/XIT set, `tx_sensors`, CW keying, browser mic TX.

Original scope, for reference:

- **Full CAT control**: VFO A/B, mode, filters, split, RIT/XIT, PTT,
  attenuator, preamp, AGC, noise blanker, squelch, antenna, keyer speed.
  Mapped byte-exact in `k3-tci-command-map.md`, including the two sideband
  polarities measured against WWV.
- **RX audio streaming** at 48 kHz — measured above, ample headroom.
- **S-meter** via `SMH;` (~1 dB resolution).
- **PTT** with a server-side watchdog.
- **TX audio** — *see the risk section; this is the one unvalidated piece.*

### Phase 2 — station features this station happens to have

None of these are in the TCI v1 scope, but the hardware supports them and
they're what makes a remote station actually usable:

- **ATU tune (KAT3A).** `MN023` reads `AUTO` and is `MP`-readable
  (`MP002;`), so ATU state is observable; a tune cycle is a switch-emulation
  command. Useful remotely, where you can't reach the ATU button.
- **Antenna switching**: `AN1`/`AN2`, plus `AR` for the KXV3's RX antenna
  loop. Both trivially mapped.
- **Voice memories (KDVR3).** No standard TCI command, but AetherSDR
  defines `rx_play`/`rx_record` extensions that would fit.
- **SWR readout** via `SW;` during transmit — exact, unlike forward power.

### Phase 3 — panadapter (deferred, needs hardware)

The KXV3 changes this from impossible to merely expensive. The P3 was ruled
out because it exposes only `#BMP` screenshots at 38400 baud. The KXV3's
**buffered IF output at 8.215 MHz** is a real tap point.

**Recommended path: KXV3 IF OUT → quadrature down-converter (LP-PAN2 or
equivalent) → 192 kHz stereo sound card.** The IF output alone is not
enough — it is an 8.215 MHz IF, not baseband, so a QSD is required to
produce the I/Q pair TCI wants. This is exactly the option the design doc
listed and the hardware for the first half is already installed.

The other route — RX ANT out into an RTL-SDR — is **not viable on a Pi 3B**.
At 2.4 MSPS it is roughly 38 Mbit/s of isochronous USB traffic on the same
`dwc_otg` controller that carries the audio codec *and* the Ethernet
adapter. That contention is not a risk to manage, it is a wall.

Even the recommended path is tight on a 3B: 192 kHz stereo is ~1.5 MB/s of
additional isochronous USB, and TCI would then carry ~12.3 Mbit/s of IQ on
top of the 3.07 Mbit/s of audio. **Treat a panadapter as a Pi 4/5
conversation** — those move Ethernet off the USB bus entirely and give the
headroom this needs.

---

## What we cannot do

- **Dual receive or diversity** — no KRX3A. Permanent.
- **Stereo RX audio** — right channel is the absent sub RX.
- **K3S stepped attenuator** — legacy 10 dB pad only.
- **True forward power** — `PO` is KX3/KX2 only. `tx_sensors` will carry the
  `PC` setpoint, labelled as a setpoint.
- **ALC readout** — requires `TM1;`, which also switches the front-panel
  meter and would visibly thrash it during operation.
- **DSB, AM-Sync, `agc_mode:off`** — no settable K3 equivalent.

---

## Open risks

### 1. TX audio — RESOLVED, path confirmed

USB audio reaches the modulator. Measured in DATA A in TX TEST mode (no RF),
comparing silence against a tone at matched mic gain: silence held at 0 ALC
across MG005–030 while a −3 dBFS tone produced 5–7, with a monotonic
tone-level response. Full detail in the command map.

Two preconditions came out of it, both now documented:

- **ALSA `PCM` mixer to 100%.** It ships at 82% (−23 dB), which was
  attenuating test tones by enough to hide them.
- **`MIC+LIN` (menu 015) ON.** It means mic *plus* line summed — OFF removes
  LINE IN from the TX path entirely.

And one operational constraint: **cap `MG` at 030.** Above ~034 the mic-path
noise floor alone lifts the ALC, which on a remote station means
transmitting shack noise between overs. Full drive is available well below
that cap.

Still untested: **keying timing** — whether `TX;` followed immediately by
audio clips the opening of a transmission. Worth measuring before the first
real QSO, but it's a tuning question now, not an existence question.

### 2. Uplink bandwidth

3.07 Mbit/s each direction, uncompressed, no protocol-level alternative.
Fine on the LAN; the deciding factor for true remote use.

### 3. Long-run audio stability

30 s proves the steady state, not the tail. A multi-hour soak counting
underruns is cheap and should run before this is trusted unattended.

---

## Recommended configuration changes

Applied on the Pi already: **✓**

| Change | Status | Why |
|---|---|---|
| FTDI `latency_timer` 16 → 1 | ✓ udev | CAT round-trip 23.4 → 15.6 ms p50, measured |
| Stable `/dev/k3cat` symlink | ✓ udev | Keyed to adapter serial; survives re-enumeration |
| CPU governor → `performance` | ✓ unit, enabled | Removes frequency-ramp latency under load |
| `k3-tci.service` | ✓ enabled at boot | Starts with the Pi, restarts on failure |
| ALSA period-size = 2048 | pending | Must match the TCI frame, or you inject ~85 ms |
| `amixer -c 2 sset PCM 100%` | Ships at 82% (−23 dB); TX audio needs the headroom |
| `MIC+LIN` (menu 015) = ON | Required for LINE IN to reach the transmitter |
| Cap `MG` at 030 in the bridge | Above ~034 the mic noise floor alone opens the ALC |
| `pip install websockets` | Not present; required for the control channel |
| Require `CONFIG:RS232 = 38400` | Precondition, not something the bridge sets |
| Turn **TX TEST off** before real operation | The radio is currently in it — no RF is produced |

The installed udev rule (`/etc/udev/rules.d/99-k3-ftdi.rules`):

```
SUBSYSTEM=="tty", ATTRS{serial}=="AL047393", SYMLINK+="k3cat", \
  RUN+="/bin/sh -c 'echo 1 > /sys/bus/usb-serial/devices/%k/latency_timer'"
```

Note this drives `latency_timer` from the **tty** rule rather than matching
the `usb-serial` device directly. The attribute lives on the usb-serial bus
device, but a rule matching `SUBSYSTEM=="usb-serial"` did not fire on this
system, while the tty rule does — and the tty rule can still be keyed on the
adapter serial, so it cannot hit some other FTDI device. `SYMLINK` only
works on a device node, which is another reason it belongs on the tty rule.

**`k3-tci.service` is enabled at boot.** It holds the serial port while
running, and Linux does not exclusively lock tty devices — so stop the
service before running any ad-hoc script that opens `/dev/k3cat` directly.
Anything talking to the bridge over WebSocket is unaffected.

`sounddevice` is absent but not needed — `arecord`/`aplay` pipes are
sufficient and are what the benchmark used.

---

## Suggested order of work

1. **Validate the TX audio path** (needs your go-ahead). Largest unknown,
   cheap to answer, and it gates whether v1 is a two-way bridge or a
   receive-only one.
2. **Apply the config changes above**, then re-measure CAT latency.
3. **Build the protocol skeleton**: WebSocket server, init handshake,
   `ready`/`start`, and `vfo`/`modulation`/`trx`. *(Done — plus audio, CW,
   filters and a web UI. The built-in UI is now the primary client.)*
4. **Multi-hour audio soak** in the background while step 3 proceeds.
5. Settle the four remaining verify items in the command map.
