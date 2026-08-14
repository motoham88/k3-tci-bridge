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
| `smcal.py` | S-meter calibration against the 10 dB attenuator. **Failed** — see below. |
| `txdiag.py` | Why will the radio not key? Checks `TX INH`, `IC` status bits, and every keying path. |
| `swrdiag.py` | Distinguishes a stale `SW` reading from a real mismatch, and ATU-inline from bypassed. |
| `txtest_off.py` | Reports and toggles TX TEST, and prints the pre-transmit state. |

## The one that failed, and why it is still here

`smcal.py` could not calibrate the S-meter. Two runs against the radio's
10 dB attenuator gave irreconcilable answers because the WWV reference faded
more than the step being measured. It is kept because the *method* is right
and only the source was wrong — rerun it against a signal generator and it
should work.

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
