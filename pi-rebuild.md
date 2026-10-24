# Pi rebuild — 2026-10-24

The bridge Pi (`shack-rpi`) was reinstalled from scratch. This records what
it took to get the bridge running on it again, so the next rebuild can go
straight through it.

## The new system

| | |
|---|---|
| Address | `192.168.1.129` (DHCP, Wi-Fi; was `.198`). Worth a DHCP reservation |
| OS | Debian 13 (trixie), aarch64, Python 3.13.5 |
| User | `kx3h` |
| Radio | K3, MCU 05.67, FTDI cable serial `AL047393` on `/dev/ttyUSB0` |
| Audio | USB Audio CODEC is ALSA card 2, matching the bridge default `hw:2,0` |

Already present on the image: `python3-numpy`, `python3-serial`,
`alsa-utils`, and `kx3h` in `dialout` and `audio`.

## Recovery files

The three system files the Pi needs are in [`deploy/`](deploy/), copied from
the running Pi:

| File | Installs to |
|---|---|
| `deploy/k3-tci.service` | `/etc/systemd/system/k3-tci.service` |
| `deploy/debug.conf` | `/etc/systemd/system/k3-tci.service.d/debug.conf` |
| `deploy/99-k3-ftdi.rules` | `/etc/udev/rules.d/99-k3-ftdi.rules` |

Once steps 1–3 below are done, this replaces steps 4–6:

```
scp deploy/* kx3h@<pi>:/tmp/
ssh kx3h@<pi> '
  sudo install -m 644 /tmp/99-k3-ftdi.rules /etc/udev/rules.d/ &&
  sudo install -m 644 /tmp/k3-tci.service /etc/systemd/system/ &&
  sudo install -D -m 644 /tmp/debug.conf /etc/systemd/system/k3-tci.service.d/debug.conf &&
  sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=tty --action=add &&
  amixer -c 2 sset PCM 100% && sudo alsactl store &&
  sudo systemctl daemon-reload && sudo systemctl enable --now k3-tci'
```

## Steps

### 1. SSH key

The existing key `~/.ssh/kx3h_rpi` was installed with:

```
ssh-copy-id -i ~/.ssh/kx3h_rpi.pub kx3h@192.168.1.129
```

### 2. Passwordless sudo

A fresh image asks for a password on `sudo`. This has to be run from a real
terminal, because Claude Code's `!` prompt has no tty for the password
prompt:

```
ssh -t -i ~/.ssh/kx3h_rpi kx3h@192.168.1.129 'echo "kx3h ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/010-kx3h-nopasswd; sudo chmod 440 /etc/sudoers.d/010-kx3h-nopasswd'
```

### 3. Bridge files and venv

The Pi is not a git checkout. Copy the files and build the venv:

```
ssh kx3h@192.168.1.129 'mkdir -p ~/k3bridge/tools'
scp bridge/*.py bridge/ui.html bridge/README.md kx3h@192.168.1.129:~/k3bridge/
scp tools/*.py tools/README.md kx3h@192.168.1.129:~/k3bridge/tools/

# on the Pi
cd ~/k3bridge
python3 -m venv --system-site-packages venv
./venv/bin/pip install websockets        # 17.3 at the time
```

Every file was checked against the repo with `md5sum -c`. `tbframetest.py`,
the one test that needs no radio, passed.

### 4. Udev rule: `/dev/k3cat` and the latency timer

`/etc/udev/rules.d/99-k3-ftdi.rules`:

```
SUBSYSTEM=="tty", ATTRS{serial}=="AL047393", SYMLINK+="k3cat", \
  RUN+="/bin/sh -c 'echo 1 > /sys/bus/usb-serial/devices/%k/latency_timer'"
```

```
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=tty --action=add
```

Confirmed: `/dev/k3cat -> ttyUSB0`, `latency_timer` = 1.

### 5. USB audio output level

The CODEC ships at 82% (−23 dB). TX audio needs the headroom:

```
amixer -c 2 sset PCM 100%
sudo alsactl store        # survives reboot
```

### 6. systemd service

The old unit file was never in the repo, so it was rewritten from notes. It is now in `deploy/`.

`/etc/systemd/system/k3-tci.service`:

```ini
[Unit]
Description=K3 to TCI bridge
After=network-online.target sound.target dev-k3cat.device
Wants=network-online.target

[Service]
User=kx3h
WorkingDirectory=/home/kx3h/k3bridge
ExecStart=/home/kx3h/k3bridge/venv/bin/python server.py
# Shutdown forces the radio to receive and disables auto-info; never SIGKILL early
KillSignal=SIGTERM
TimeoutStopSec=20
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/k3-tci.service.d/debug.conf` turns on verbose logging:

```ini
[Service]
ExecStart=
ExecStart=/home/kx3h/k3bridge/venv/bin/python server.py -v
```

```
sudo systemctl daemon-reload
sudo systemctl enable --now k3-tci
```

`--ptt-rts` is not set, so PTT goes over CAT (`TX;`/`RX;`).

## Tests

All run on the Pi against the live service. All are receive-only.

| Test | Result |
|---|---|
| Startup | Radio found (ID017, fw 05.67), VFO A 21.153050 USB. Warned that TX TEST is on, which is intended: listen-only today |
| Web UI | `http://192.168.1.129:50001/` returns 200 from the LAN |
| `tcitest.py` | Handshake, queries, sets, batched commands OK (run without `--ptt`) |
| `rxtest.py` | AGC, PRE, ATT, NB, RIT, XIT and LOCK each flip and read back |
| `filtertest.py` | `rx_filter_band` correct in every mode class; each mode's filter restored |
| `bandtest.py` | Every band button lands in its band; VFO A restored to 21.153050 |
| `audiotest.py` | RX 23.43 frames/s, p50 42.88 ms, about −27 dBFS; TX audio ingest OK with the radio in receive |
| Journal | No warnings during the run |

Not run, because they transmit: `tcitest.py --ptt`, `multiclient.py`,
`cwtest.py`, `cwstoptest.py`, `chronotest.py`.
