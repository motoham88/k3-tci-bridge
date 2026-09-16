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

### What the full 14 MHz sweep does and does not show

`smcal2-14mhz.csv` is the sweep from -100 to -15 dBm, committed verbatim.
Its `entered` column is the script's suggested level at every point, because
Enter was pressed each time, and the file has no `p3_dbm` column. That looks
like an unverified run, but it is not. **The P3 was the reference.** The
CE-4000 struggled to make some levels, so the operator raised its output
until the P3 read each 5 dB step. The level column is therefore P3-measured
from -100 up to at least -25 dBm. The operator recalls the last P3 check as
-25 but is not sure it was not -15, so **-20 and -15 may be unchecked**.
This comes from recollection the morning after, not the file, and the P3
values were not typed in. `smcal2.py` now prompts for them so the file carries the
control itself next time.

Run through `--fit` unedited, it reports a single line with 7 dB rms
residual and declares the break at SMH 40 *"doing work"*. Do not believe
that verdict. The points below explain why.

**The middle agrees with the partial run.** From -85 to -45 dBm every point
falls within about 2 counts of the partial fit (`dBm = -118.64 + 1.2338*n`),
and -85 to -70 on its own refits to `-118.86 + 1.2346*n`. It carries on
across SMH 40 with no visible change of slope up to about SMH 62. So on
this radio the documented break at 40 may not be real.

**The bottom three disagree with the partial run, at the same level.**
-100, -95 and -90 dBm read SMH 30, 31 and 39. In the partial run, -105 read
SMH 11 with the P3 reading -105. Both runs had the level confirmed on the
P3, so the meter gave about 15 dB more for the same input. One difference
between the runs is known: **the partial run used fast AGC (GT002), the full
sweep slow (GT004)**. The meter follows the AGC line, and slow AGC holds a
stronger level for a while after it goes away. Raising the generator to find
each level could leave the meter still holding a stronger level when the
1.5 s settle ended. That is a hypothesis, and it is testable. It fits the
points that went wrong at the bottom, where the generator needed the most
coaxing.

**There is a 14.5-count jump between -40 and -35 dBm**, for 5 dB of input
confirmed on the P3, and above it the points sit about 11 counts above the
line. The same slow-AGC hold is one candidate. It is unexplained.

**The top two or three steps are contaminated.** The operator recalls the
K3's overload protection relay pulling in at the highest levels, around the
last two or three steps (-25, -20, -15 dBm). Once it pulls in, the receiver
no longer sees the generator's full level. That explains -15 dBm reading
*lower* than -20 (86.5 against 94). Those points measure the protection,
not the meter, so leave them out of any fit, whatever the P3 read.

The half-count readings (64.5 at -40, 89.5 at -25, 86.5 at -15) mean the
12 reads straddled two values. The meter was still moving at the same
points that misbehave. At -40 that fits a settle that was too short. At -25
and -15 it fits the relay pulling in during the reads.

**Consequence: `bridge/tci.py` and the S-meter spec in
`k3-tci-command-map.md` stay unchanged.** `smcal2.py` now prompts for the P3
reading at every point, records it as `p3_dbm`, and leaves any point that
disagrees by more than 1.5 dB out of the fit. The next step re-measures the
suspect levels under both AGC settings, with a long settle so the hold has
time to decay:

```
python3 smcal2.py --agc GT004 --settle 5 --levels -100,-95,-90,-40,-35,-20,-15 --out smcal2-retest-slow.csv
python3 smcal2.py --agc GT002 --settle 5 --levels -100,-95,-90,-40,-35,-20,-15 --out smcal2-retest-fast.csv
```

Listen for the protection relay at -20 and -15 dBm, and note which levels
trip it. The script cannot see it, and a tripped point gets left out of the
fit. If it trips, the usable top of the calibration is the highest level
below that.

If the bottom points come down to about SMH 15-23 with the longer settle,
the sweep's outliers were AGC hold and the rest of it stands. If they
repeat under both AGC settings with the P3 agreeing, the meter really does
that. Either way the result is a finding.

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
