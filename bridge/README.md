# K3 → TCI bridge

Working CAT control **and bidirectional audio streaming**. See
`../k3-tci-command-map.md` for the mapping this implements and
`../k3-tci-capability-eval.md` for the measured performance envelope.

## Layout

| File | Role |
|---|---|
| `ui.html` | Web UI, served by the bridge itself on the same port |
| `k3cat.py` | Serial transport. Reader thread, request/response routing, set-verify-retry, `?;` handling, AI2 echo-loop guard |
| `tci.py` | Protocol layer. Mode mapping, init burst, TCI command handlers, PTT ownership and watchdog |
| `server.py` | asyncio WebSocket server, client set, broadcast, reconcile sweep |
| `tcitest.py` | Single-client exercise: handshake, queries, sets, PTT, batching |
| `multiclient.py` | Two clients — verifies broadcast-to-all and PTT release on disconnect |
| `audio.py` | TCI binary frames, ALSA capture/playback, software volume |
| `audiotest.py` | RX stream validation + TX audio ingest |
| `voltest.py` | Software `volume` / `mute` verification |
| `cwtest.py` | CW keying: speed, mode guard, chunking |
| `chronotest.py` | TX_CHRONO pacing (start, rate, stop) |
| `wsjtxmon.py` | Watches WSJT-X transmissions, checks FT8 slot timing |
| `soak.py` | Long-run audio soak (hours), gap and drift detection |
| `filtertest.py` | `rx_filter_band` across all mode classes |
| `audiobench.py` | RX audio pipeline benchmark (used for the capability eval) |

## Web UI

Open **http://\<pi\>:50001/** — the bridge serves the UI over HTTP on the
same port it serves WebSocket on. Same origin for page and socket, which
removes the mixed-content problem entirely: a browser refuses a `ws://`
socket from an `https://` page, and there is no second server to run.

`process_request` checks for an `Upgrade: websocket` header and hands those
requests to the WebSocket handler; everything else gets the page.

Works on a phone on the same LAN. Frequency is tuned by clicking or
scrolling the upper/lower half of any digit.

**Known limitation — no microphone TX from the browser.** `getUserMedia`
requires a secure context, and `http://` on a LAN address is not one
(`localhost` would be, a LAN IP is not). PTT keys the transmitter, so it is
usable for tune and CW, but voice from the browser needs the UI served over
HTTPS with the socket upgraded to `wss://`. The phone app does not have this
restriction.

**iPhone: audio is silent with the ring/silent switch set to silent.** iOS
puts a bare `AudioContext` in the *ambient* audio session category, and
ambient is the category that switch governs — so on a muted phone the socket
connects, every control works, frames arrive and get scheduled, and nothing
comes out. Media elements ignore the switch; Web Audio does not. Affects
every iOS browser, since they are all WebKit. The UI now sets
`navigator.audioSession.type = "playback"` (Safari 16.4+) on audio start,
which is the category streaming apps use and is not muted by the switch. On
older iOS the switch still wins — flip it off silent.

## Running

Normal way — as a service:

```
sudo systemctl start k3-tci        # journalctl -u k3-tci -f  for logs
sudo systemctl stop k3-tci
```

The unit is **enabled at boot**. Note that while running it holds the serial
port, and Linux does not exclusively lock tty devices — so **stop the
service before running any ad-hoc script that opens `/dev/k3cat`**, or the
two will interleave garbled traffic. The test scripts below talk to the
bridge over WebSocket rather than the serial port, so they are safe to run
against a live service.

By hand, for development:

```
cd ~/k3bridge && ./venv/bin/python server.py -v
```

Listens on `ws://0.0.0.0:50001`. Uses `/dev/k3cat`, a udev symlink keyed to
the adapter's serial number, so it survives re-enumeration.

The venv is created with `--system-site-packages` so numpy/pyserial come
from Debian and only `websockets` is installed into it — Debian 13 is
PEP 668 managed, and this avoids needing root.

To start it detached over SSH, `setsid --fork` is required; plain
`nohup ... &` leaves the SSH channel open and the command appears to hang:

```
ssh kx3h@shack-rpi 'cd ~/k3bridge && setsid --fork ./venv/bin/python server.py > server.log 2>&1 < /dev/null'
```

## Measured

| | |
|---|---|
| Audio frame rate | 23.43/s over 60 s, zero dropped to the client |
| Frame | 16448 bytes = 64 header + 16384 float32 stereo |
| Inter-frame jitter | p50 42.88 ms, p95 43.27, max 43.80 |
| Server CPU, streaming | 14.8% of one core (~3.7% of the box) |
| CAT round-trip | 15.6 ms p50 (`latency_timer=1`) |

## Implemented

- Init handshake: 24 messages, settings → `ready` → `start`
- `vfo` (A/B, get and set, broadcasts the **accepted** frequency)
- `modulation` (get/set, full mode map)
- `trx` (PTT, with confirm-and-retry)
- `split_enable`, `dds`, `rx_enable`, `vfo_limits`, `if_limits`
- Broadcast of every state change to all clients
- AI2 unsolicited events → TCI notifications, with echo-loop suppression
- 3 s reconcile sweep for the state AI2 does not report
- PTT ownership: the keying client's disconnect unkeys immediately
- 90 s PTT watchdog as the backstop
- RX audio streaming: `audio_start` / `audio_stop`, float32 stereo at 48 kHz
- TX audio ingest (float32 and int16), continuous primed playback stream
- `volume` / `mute` in software, `rx_smeter` at 5 Hz (suppressed in TX)
- Web UI served on the same port (VFO, modes, filter, S-meter, audio, PTT, split)
- `rx_filter_band` get/set, mode-aware; re-reported on mode change
- CW keying: `cw_msg` / `cw_macros` (chunked to KY's 24-char limit,
  buffer flow-controlled), `cw_keyer_speed`, `cw_macros_stop`
- `drive` (power), `tune`, `mic_level`
- **TX_CHRONO pacing** — the clock that tells WSJT-X-style clients
  when to send TX audio; measured at 46.90/s against a 46.88/s target
- `tx_sensors` carrying the measured level of TX audio actually
  received, so "keyed but sending nothing" is visible rather than silent

## Known limitation: enabling split

`split_enable:0,true` does not take. The command reaches the bridge, the
bridge sends `FT1;`, and the radio's `IF` response still reports split off.
Cause not established — `FT1` may be returning `?;` on a path that does not
check for it, or split may be unavailable in the mode being used.

Reading split works correctly, and clearing it works, so WSJT-X (which only
ever sends `split_enable:false`) is unaffected. Not pursued because nothing
in use here needs split. If you need it, start by checking whether `FT1;`
returns `?;` with the service stopped.

## Not yet implemented

Browser microphone TX (needs HTTPS/WSS), RIT/XIT set, `tx_sensors`
(built but not driven — needed only by clients like WSJT-X that wait to be
asked for TX audio), IQ (deliberately never — no panadapter).

## Six things not to "fix"

1. **`cwl` → `MD3`, `cwu` → `MD7`.** Plain "CW" on this radio is the *lower*
   sideband. Measured against a WWV carrier, not assumed. It looks like a
   typo and is not.
2. **`DT` is set before `MD`.** Norm/reverse is stored per sub-mode pair, so
   setting `DT` can move `MD` between 6 and 9. Setting `MD` first gets
   silently undone.
3. **`IS` is only written in SSB and DATA modes.** In CW, AM and FM the TCI
   passband straddles the carrier, so `(lo+hi)/2` is zero and writing it
   would drag the passband to DC. Guarded twice: by mode, and by refusing a
   computed centre of zero.
4. **`volume` and `mute` are software gain, not `AG`.** The K3's AF GAIN has
   no effect on the USB audio — measured 0.6 dB across AG000..AG250, because
   LINE OUT is a fixed-level tap. The hardware control is `LIN OUT`
   (menu 032), which is a one-time calibration, not a volume slider.
5. **Header `length` is samples, not frames.** Stereo means `frames * 2`.
   Getting it wrong makes clients play at half speed.
6. **`ctx.createBuffer(2, n, 48000)` in the web UI hardcodes 48000 on
   purpose.** That argument is the rate of the *data*, not of the context.
   An `AudioBuffer` carries its own rate and the browser resamples on
   playback when the two differ, which is correct. "Tidying" it to
   `ctx.sampleRate` relabels 48 kHz samples as whatever the hardware runs
   at and plays them at the wrong pitch.

## Two radio settings that fail silently

Both were found the hard way. Neither produces an error of any kind — the
only symptom is that nothing happens.

1. **`MIC+LIN` (menu 015) must be ON** for USB audio to reach the
   transmitter. It is the *enable* for LINE IN — with it OFF nothing
   reaches the modulator at any mic gain. Do not switch it off to keep the
   mic out of the TX path for digital modes: that just silences you. The
   mic is unavoidably live, so the bridge mutes the transmit monitor on
   entering a digital mode instead — otherwise monitor → speaker → mic →
   transmitter → monitor howls.
2. **CW VOX (`VX1`) must be on** for `KY` text to key the radio. Without it
   the K3 buffers the text and never transmits. The bridge now checks and
   enables it automatically before keying, since a remote operator cannot
   reach the front panel.

## A multi-client trap the tests could not catch

The bridge broadcasts state to every client, so `trx:0,true` means *the
radio* is transmitting — not *this client* is transmitting. A web UI that
released PTT on `S.tx` therefore cut short transmissions started by other
clients: a stray `pointerleave` on the PTT button, or a tab switch, sent
`trx:0,false` and dropped WSJT-X mid-transmission from another machine.

Only ever release a transmission you started. `multiclient.py` verifies
that state *reaches* both clients; it cannot verify that an idle client
*refrains from acting* on it, which is where this lived. The server now
logs a warning when an unkey arrives from a client that does not hold PTT —
it is still permitted, since an emergency stop from anywhere is worth
having, but it should never happen silently.

## Two test-harness traps

**Never wait for the socket to go quiet.** `rx_smeter` broadcasts every
200 ms and audio streams continuously, so a "read until silent" loop never
terminates. Collect over a fixed window instead.

**Measure over what you observed, not over the window you intended.** The
chrono clock only starts once PTT is confirmed, about a second after the
request, so counting frames across a fixed window under-reports the rate. A
correct 46.9/s clock measured as 41/s that way.

**Client-side arrival times are not server-side send times.** TCP coalesces
WebSocket frames, so measuring inter-arrival gaps shows bursts (near-zero
gaps followed by long ones) that say nothing about the server's timing. Drain
the socket before a measurement window, count over the window, and let the
server log its own rate. Measuring the chrono clock this way made a
correct 46.90/s clock look like 49.6/s.
