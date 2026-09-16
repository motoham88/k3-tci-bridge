# Bench tools

The scripts that established the findings in
[`../k3-tci-command-map.md`](../k3-tci-command-map.md). They talk to the
radio **directly over the serial port**, so:

> **Stop the bridge first** — `sudo systemctl stop k3-tci`. Linux does not
> exclusively lock tty devices, so both would open `/dev/k3cat` and
> interleave garbled traffic.

Anything that keys the transmitter asserts TX TEST first and aborts if it is
not set, so nothing here radiates.

## What each one is for

| Script | Answers |
|---|---|
| `k3probe2.py` | Is the radio talking at all? Sweeps modem-control lines and listens passively. Start here on new hardware. |
| `k3state.py` | Full state sweep — every parameter the bridge needs, plus a field-by-field `IF` decode. |
| `k3ratest.py` | K3 or K3S? Probes the attenuator: `RA05/10/15` collapsing to `RA01` means a K3 RF board. |
| `wwvtest2.py` | **Sideband polarity.** Measures FFT energy at the predicted frequency against a WWV carrier, with USB/LSB as controls. |
| `txfinal.py` | **Does USB audio reach the modulator?** Silence-vs-tone at matched gain. |
| `miclin2.py` | Does LINE IN work with `MIC+LIN` OFF? (No — it is the enable for LINE IN.) |
| `montest.py` | Does the transmit monitor reach LINE OUT? (No — +0.3 dB from MON 0 to 100.) |
| `rxlevel.py` | Which control sets the USB RX level — `AG` or `LIN OUT`? (`AG` does nothing.) |
| `calibrate.py` | `LIN OUT` sweep against a real signal, picking a level with headroom. |
| `smcal.py` | S-meter *slope* from the 10 dB attenuator. **Failed against WWV** — see below. |
| `smcal2.py` | S-meter *absolute* calibration against a signal generator. Sweeps known levels, fits the curve, writes the raw pairs. |
| `txdiag.py` | Why will the radio not key? Checks `TX INH`, `IC` status bits, and every keying path. |
| `swrdiag.py` | Distinguishes a stale `SW` reading from a real mismatch, and ATU-inline from bypassed. |
| `txtest_off.py` | Reports and toggles TX TEST, and prints the pre-transmit state. |

## The one that failed, and why it is still here

`smcal.py` could not calibrate the S-meter. Two runs against the radio's
10 dB attenuator gave irreconcilable answers because the WWV reference faded
more than the step being measured. It is kept because the *method* is right
and only the source was wrong — rerun it against a stable carrier and its
number should hold.

But it only ever gives half the answer, which is worth being precise about.
The attenuator yields the **slope** — dB per count — and that figure is
independent of any generator's absolute accuracy, because 10 dB is 10 dB
whatever the source's level error. It can never give the **anchor**, where
the curve sits in dBm, because the radio has no idea what is arriving at its
antenna jack.

`smcal2.py` is the other half: known level in, count out, across the range.
That is what tests the three things the conversion in `bridge/tci.py`
currently assumes rather than knows — that S9 is really SMH 40 on *this*
radio, that the piecewise break at 40 is real, and what the `SM` scale does
below S9, where the reference gives no anchor at all.

Run both. They answer different questions, and the slope from `smcal.py` is
a free check on the slope `smcal2.py` fits.

### The 6 dB trap in a generator that reads microvolts

The CE-4000 at this station reads **microvolts**, so `smcal2.py` defaults to
`--units uv` and converts: `dBm = 20*log10(uV) - 106.99`, which puts 50 uV
at -73.01 dBm. That is the check worth remembering, because S9 is *defined*
as -73 dBm and 50 uV into 50 ohms is exactly that.

The trap is that a generator calibrated in microvolts states either the
**open-circuit** voltage (EMF) or the voltage across a **matched load**
(PD), and EMF is twice PD — 6.02 dB — because the source's own 50 ohms forms
a divider with the load. Read the wrong one and every point in the run is
off by 6.02 dB in the same direction.

That is the worst possible shape for an error here. A uniform offset does
not scatter the points, so the fit stays clean, the residuals stay small,
and the whole 6 dB is absorbed silently into the intercept. The *slope*
survives it — which means `smcal.py` would agree, and agreement would look
like confirmation. The **anchor** is what is wrong, and the anchor is the
only reason to involve a generator at all.

Settle it from the CE-4000's manual before the run, and pass `--emf` if it
states open-circuit volts. This cannot be recovered afterwards from the data
alone: a run that is uniformly 6 dB out is indistinguishable from a radio
whose S9 sits 6 counts away.

**Settled on this station's CE-4000: it is PD.** Measured, not assumed. With
the generator at 50 uV the attached P3 reads -73 dBm; at 0.126 uV it reads
-125 dBm. PD predicts exactly those. EMF would predict -79 and -131. Two
points 52 dB apart, both agreeing within 0.01 dB, and two independent
instruments do not drift into agreement that close. `--emf` stays off for
this generator.

That also retires the worry about the CE-4000's unknown level accuracy: the
P3 tracks it across 52 dB, which bounds the generator's error far better
than a specification sheet would.

## The pattern worth copying

Every one of these runs its controls first and aborts if the controls fail.
That is not ceremony. Three separate results in this project looked
conclusive and were wrong:

- A sideband test measuring WWV's modulation tones rather than its carrier,
  because it searched for the strongest peak instead of energy at the
  predicted frequency.
- A "TX audio works" result where the ALC was responding to something that
  was not the test tone — silence deflected the meter identically.
- An S-meter calibration that was measuring propagation, not the attenuator.

Each was caught by a control, and none would have been caught without one.

## Opening the port

Use `k3serial.open_k3(PORT)`, never `serial.Serial(...)` directly. A port
comes up with DTR and RTS asserted unless it is told otherwise, and the K3
can be told to read DTR as KEY and RTS as PTT (its RS232 menu) — so opening
it the default way is a key-down at a radio configured for that. It happened
here, and stopping it took unplugging the USB lead.

`k3probe2.py` is the exception: its sweep asserts both lines on purpose, to
find out what they do. Turn on TX TEST before running it.
