# K3 → TCI Bridge (Raspberry Pi) — Design Notes

Status: design/scoping phase, no bridge code written yet. Hardware is on the
bench and characterized.

Companion documents:

- **`k3-tci-command-map.md`** — the byte-exact TCI ↔ K3 CAT mapping, global
  rules, and what's been verified against the radio.
- **`k3-tci-capability-eval.md`** — what this station can support, with
  measured performance numbers, open risks, and recommended next steps.

## Goal

Build a Raspberry Pi–based backend for an Elecraft K3 (USB serial CAT + USB
soundcard audio) that speaks the **TCI (Transceiver Control Interface)**
protocol, so existing TCI-capable SDR clients can do remote CAT control and
audio pass-through — extending the K3's usable life as a remote/networked
station without buying newer hardware.

Reference hardware: Elecraft **K3** (not a K3S — see below), connected via
USB. It enumerates as a TUSB2036 hub carrying an FT232 serial UART (CAT)
and a PCM2901 audio codec, so CAT and audio do arrive on one cable.

The radio identifies as a K3, not a K3S, on two independent tests:
`OM;` returns `OM APX-D---V---;` with no `R` (K3S RF board), and the
attenuator only honors the legacy `RA00`/`RA01` form — `RA05`, `RA10` and
`RA15` all collapse to `RA01`. That second test is RF-board behavior, not
firmware reporting, so it's decisive. Installed options: KAT3A ATU, KPA3A
PA, KXV3, KDVR3, KSYN3A. **No KRX3A sub receiver.**

Bench setup: Raspberry Pi 3B (`shack-rpi`, 192.168.1.198), Debian 13
trixie, aarch64, 905 MB RAM. CAT on `/dev/ttyUSB0` at 38400 — address it as
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL047393-if00-port0` so it
can't move. Codec is ALSA card 2 (`hw:2,0`), `S16_LE` stereo, 48 kHz
native.
A P3 panadapter is also attached but — see below — is **not** usable as an
IQ/spectrum data source for this project.

## Why TCI

- Open, documented WebSocket protocol (originally Expert Electronics /
  ExpertSDR2, adopted by Thetis/OpenHPSDR and others).
- Full protocol reference obtained and read in detail (text command channel +
  binary IQ/audio frames). Known-good test client: **TCI Remote** app by
  ON7OFF (Android/web), which has a public, verified protocol reference doc.
- QK4 (Elecraft K4 remote app) was ruled out — it's built around the K4's own
  native network protocol, no generic external-server hook.
- AetherSDR was ruled out as a *client* target for now — its architecture is
  built around radio-specific backends (SmartSDR protocol, experimental
  Hermes-Lite2, networked Icom CI-V) baked into the app, and its TCI feature
  runs the other direction (AetherSDR is a TCI *server*, not a client that can
  point at an external server). Possible future avenue: contribute a K3
  backend to AetherSDR's IRadioBackend abstraction directly, or ask upstream
  whether a TCI-client mode is feasible.

## Protocol summary (from TCI Protocol Reference, verified against TCI Remote v3.5)

- Transport: plain WebSocket (RFC 6455), default port 50001, client-initiated.
- Text commands: `command_name:param1,param2;` UTF-8, lowercase, semicolon
  terminated (except bare `ready`/`start`/`stop`). Multiple commands can be
  batched in one frame, separated by semicolons.
- Server must push a full init block ending in `ready` before the client sends
  anything: `protocol`, `device`, `receive_only`, `trx_count`,
  `channels_count`, `vfo_limits`, `if_limits`, `modulations_list`,
  `iq_samplerate`, `audio_samplerate`, `tx_profiles_ex`, then `ready`.
- Most stateful commands have a query form (command + trx index, no value) —
  must be implemented or client UI shows stale values.
- Server is a stateful broker: any state change from any source (client or
  hardware) must be echoed to **all** connected clients.
- PTT (`trx:`) requires a server-side watchdog (60–120s) to force RX if the
  client connection drops — do not rely on the client sending PTT-off.
- Binary frames: 64-byte header (flags, sample_rate, 3x header fields,
  sample_count, frame_type, 40B padding) + payload.
  - `frame_type 0` = IQ (float32 I/Q pairs, sample_rate must be ≥ 96000 Hz,
    192000 recommended)
  - `frame_type 1` = RX audio (float32 stereo interleaved, 48000 Hz)
  - `frame_type 2` = TX audio (client → server, same format)
- Mode strings client expects: LSB, USB, DSB, CWL, CWU, NFM, AM, SAM, DIGL,
  DIGU (fixed set in TCI Remote's UI — must echo back exact same spelling).

## K3 CAT mapping

Built out in full against the K3S/K3/KX3/KX2 Programmer's Reference (Rev.
G5) — see **`k3-tci-command-map.md`** for the complete table: every TCI
command, its K3S CAT equivalent, the read-back GET, unit conversions, and
the traps.

Decisions that came out of building it, and that shape the prototype:

- **Run `K31;` + `K20;`.** K3 extended mode widens `SM` from 0000-0015 to
  0000-0021 and puts the DATA sub-mode in the `IF` response. `K22` was
  considered (it would allow `agc_mode:off`) and rejected — it changes the
  format of `PC`, `NB`, `KY` and `GT` for one value.
- **Always use `BW`, never `FW`** — `FW`'s semantics shift with the
  meta-command mode. Note the reader must still *parse* `FW`: the radio
  auto-reports it on every band change regardless.
- **Read back after every clamping SET.** `BW`, `IS`, `PC` and `RA` all
  silently alter values they can't honor, so broadcast the accepted value,
  not the requested one.
- **`AI2;` for state echo, plus a slow reconcile poll.** AI2 covers most
  front-panel events but the reference admits *"only a subset of controls
  generate responses"* — a 2-5 s reconcile sweep covers the rest. Our own
  SETs echo back as AI2 responses and must not be re-broadcast as fresh
  hardware events.
- **`trx_count:1`.** VFO A always receives on a K3; A/B map to TCI channels
  0 and 1 of a single trx. Sub RX (KRX3A) becomes trx 1 later, gated on
  `OM;` reporting `S`.
- **`SMH;` for the S-meter**, not `SM;` — ~1 dB resolution vs 5-6 dB. Poll
  at 200 ms, and suppress during TX (`SM` returns 0000 in transmit anyway).
- **Skip `DS` and `IC`.** Both return bytes ≥ 0x80; skipping them keeps the
  serial reader line-oriented on `;`.

Known gaps, recorded in the map's "Unmappable in v1" section: DSB and
AM-Sync (no settable K3 equivalent), `agc_mode:off`, true forward power
(`PO` is KX3/KX2 only — `tx_sensors` carries the `PC` setpoint instead),
ALC (reading it hijacks the front-panel meter), and independent RIT/XIT
offsets (the K3 has one shared `RO` register).

Six items need hardware verification before the mapping can be trusted —
listed at the end of the map. The important two are the **CWU/CWL → MD3/MD7
and DIGU/DIGL → MD6/MD9 sideband polarities**: the K3 reference never states
which sideband `MD3` or `MD6` sits on, and getting either backwards is
silently wrong — inverted sideband, no error.

## Panadapter / IQ status — RULED OUT for v1

Investigated whether the attached P3 could supply the IQ stream TCI wants.
**It cannot.** Findings from the P3 Programmer's Reference (Rev A7):

- The P3 does its own internal FFT/down-conversion from the K3's IF output —
  it does not expose raw IQ or even computed spectrum bin data over its
  RS232/USB command interface.
- The only display-data command is `#BMP` — a full bitmap screenshot upload
  (131,638 bytes + checksum, no semicolon terminator).
- The P3's PC-facing RS232 link runs at a max of 38400 baud, so a single
  `#BMP` grab takes on the order of 30+ seconds — completely unusable for a
  live waterfall/panadapter feed.

Real options for IQ/spectrum later, if wanted:
1. Independent IF tap off the K3's IF output into a dedicated 192kHz-capable
   USB sound card, computed independently of the P3. **This station has the
   first half already**: the KXV3 provides a buffered 8.215 MHz IF output.
   It still needs a quadrature down-converter (LP-PAN2 or equivalent) to
   reach baseband I/Q — the IF alone is not what TCI wants — and
   realistically a Pi 4/5, since 192 kHz stereo plus the audio codec plus
   USB-attached Ethernet is more than a 3B's single USB controller should
   carry. See `k3-tci-capability-eval.md` for the analysis, including why
   the RX-ANT-into-an-RTL-SDR alternative is a non-starter on a 3B.
2. Skip panadapter/IQ entirely — most TCI clients degrade gracefully to "no
   spectrum" if `iq_start`/`iq_stop` are simply left unimplemented.

## v1 scope (locked in)

- CAT control (VFO, mode, filters, split, RIT/XIT, PTT) via TCI text commands
  mapped to K3 serial CAT.
- RX/TX audio streaming via TCI binary audio frames (float32 stereo, 48kHz),
  sourced from/sent to the K3's USB audio codec.
- S-meter and basic TX telemetry (`rx_smeter`, `tx_sensors`) via polling.
- No IQ streaming, no panadapter, no `#BMP` polling.
- Test client: originally TCI Remote (ON7OFF), whose published protocol
  reference is what the mapping was verified against. Not adopted in the end
  — its browser build is built around the author's Compactor tunnel, and the
  project ships its own web UI instead. The protocol findings it produced
  remain valid and are cited where used.

## Suggested stack

- Python + `websockets` for the control channel (CAT-rate traffic is low
  bandwidth/low frequency, no latency concerns).
- `pyserial` for K3 CAT over USB.
- `sounddevice`/ALSA + `numpy` for audio capture/playback. **No resampling
  is needed** — the PCM2901 runs 48 kHz `S16_LE` stereo natively, which is
  TCI's audio rate exactly, so the conversion is int16→float32 (a scale by
  1/32768) and nothing more. This materially lowers the risk that Python is
  too slow for the audio path.
- RX audio arrives **mono on the left channel** (the right carries the sub
  receiver, which isn't installed — measured at −85 dBFS). The bridge must
  duplicate left into both channels of the TCI stereo frame.
- ~~Consider moving audio framing to a tighter loop / different language~~
  **Measured and retired.** The full RX pipeline costs 1.6% of one core with
  ±0.5 ms jitter and zero underruns over 30 s — roughly 60× headroom. The
  real latency risk in this path is ALSA period sizing, not the language:
  a mismatched period injected 85 ms while CPU stayed near zero. Match the
  period to the 2048-sample TCI frame. See `k3-tci-capability-eval.md`.

## Open questions / next steps

- [x] Build full K3 CAT ↔ TCI command mapping table — done, see
      `k3-tci-command-map.md`.
- [ ] Verify the six hardware-dependent items at the end of the command map
      (sideband polarities first).
- [ ] Decide PTT watchdog timeout value.
- [ ] Prototype server, get init handshake + `vfo`/`modulation`/`trx` working
      against TCI Remote app.
- [ ] Once core bridge is stable, revisit AetherSDR: ask upstream (GitHub
      discussions/issues) whether a TCI-client mode or generic serial-CAT
      backend is feasible, using the same gh-cli-via-Claude-Code workflow
      used for the mini-pan feature request.
