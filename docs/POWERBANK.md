# Power bank — Viking PN-964PD + Raspberry Pi 5

ROVER2 test1 runs untethered from a **Viking PN-964PD** (27 000 mAh, 145 W max, USB‑A + 2× USB‑C). Many MCU power banks **turn USB output off** when they think nothing is connected or when average load is too low. The Pi must stay powered 24/7 on battery.

Official manual for the same Viking PD family: [PN-962PD user manual](https://www.manualslib.com/manual/2167078/Viking-Pn-962pd.html) (BONA SPES; PN-964PD behaves the same way).

---

## Viking PN-964PD — what the manual says

| Behaviour | Detail |
|-----------|--------|
| **Standby** | Screen off; bank dormant until a load is detected or you use the button |
| **Start output (auto)** | Plug a device into a USB OUT port → output starts, display shows % and voltage |
| **Start output (manual)** | In standby, **double‑tap ON/OFF** → 5 V output on (display on ~40 s, then display dims while output can stay on) |
| **Shut down output** | **40 s after the device is disconnected** from USB, outputs and display switch off |
| **Manual off** | **Double‑tap ON/OFF** while discharging → outputs off |

There is **no documented “always on / low‑current” menu** on this series. The Pi **cannot** disable bank sleep in firmware — only physics (continuous load) or a different power supply.

**Official manual (CZ/EN):** [Google Drive — PN-964PD](https://drive.google.com/file/d/1iyIzOx29Xs-yGQXcDO0tE92wEhev2eDS/view)

---

## How to get “power without sleep” (real answer)

| Method | Always-on? | For test1 robot? |
|--------|------------|------------------|
| **USB keep‑alive dongle** on 2nd port (~50–100 mA 24/7) | Yes | **Best** (hardware) |
| **`rover2-virtual-usb-dongle.service`** (constant light CPU load on Pi) | Often works | **Yes — software substitute** |
| Pi software timer (stress burst every 90 s) | Tricks some banks | Legacy; disabled if virtual dongle on |
| **Pass‑through** (charger into bank IN while Pi on OUT) | Bank stays in charge mode | Only if tethered to wall power |
| Double‑tap ON/OFF after plug | Wakes output; does not stop low‑current sleep | Required once per session |
| Different PSU (bench supply / UPS / “always on” power bank) | Yes | Best if Viking keeps dying |

---

## Recommended setup for the Pi

1. **Cable** — Pi 5 needs a solid **USB‑C** cable (5 V, **≥3 A** sustained; 5 A preferred). Loose connectors can look like “disconnect” → bank off in 40 s.
2. **Port** — Use a **USB‑C OUT** on the Viking (PD). Avoid under‑rated adapters or long thin cables.
3. **Wake the bank** — After plugging the Pi in (or after any replug), **double‑tap ON/OFF** once so output is definitely on (manual mode).
4. **Second port (best hardware fix)** — Plug a small **USB keep‑alive load** (~50–100 mA) into **USB‑A** or the other USB‑C OUT, e.g.:
   - “Power bank keep alive” dongle (resistor load), or  
   - USB LED / dummy load  
   This stops many banks from sleeping when the Pi idles. Cost is a little extra drain; far less than losing the Pi.
5. **While testing** — Keep `rover2-api` running (serial poll + follow). For overnight / idle on battery, enable the optional Pi timer below.

---

## Virtual USB dongle (software — on the Pi)

Emulates a keep‑alive dongle by holding a **small constant CPU load** (~10% of one core by default). Extra power is drawn through the **same USB‑C cable** as the Pi (not the bank’s spare port).

```bash
# On the Pi (or: ssh ambassad0r@192.168.70.11 '...')
sudo systemctl enable --now rover2-virtual-usb-dongle.service
sudo systemctl status rover2-virtual-usb-dongle.service

# Tune load — raise if bank still sleeps, lower if Pi runs hot
sudo nano /etc/default/rover2-virtual-usb-dongle   # VIRTUAL_DONGLE_CPU_LOAD=10

# Wall power / lab — turn off
sudo systemctl disable --now rover2-virtual-usb-dongle.service
```

Installed by `./deploy_pi.sh` (skip with `ROVER2_SKIP_VIRTUAL_DONGLE=1 ./deploy_pi.sh`).

---

## Optional: Pi software keepalive timer (fallback only)

If you **cannot** add a USB dongle yet, you can enable a **short load burst every 90 s** on the Pi (not the same as a true always‑on bank):

```bash
cd ~/Documents/projects/rover2
./scripts/setup_powerbank_keepalive.sh rover-eth
# or after deploy:
ssh rover-eth 'sudo bash /opt/rover2/scripts/setup_powerbank_keepalive.sh'
```

Then on the Pi:

```bash
sudo systemctl enable --now rover2-powerbank-keepalive.timer
```

Disable when back on wall power:

```bash
sudo systemctl disable --now rover2-powerbank-keepalive.timer
```

Also disables systemd sleep targets so the Pi does not suspend.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|----------------|-----|
| Bank off after ~40 s | Unplug / bad cable / port sleep | Reseat cable; **double‑tap ON/OFF**; add USB keep‑alive on 2nd port |
| Bank off after minutes idle | Low average current | Keep‑alive dongle + enable `rover2-powerbank-keepalive.timer` |
| Pi brownouts / reboots | Cable or port not enough amps | Shorter USB‑C cable; use PD USB‑C OUT; don’t share port with heavy loads |
| Display off but Pi still runs | Normal Viking behaviour | Display dims after ~40 s; not necessarily output off |

---

## Do not use on the robot

- **`stress-ng.service`** left enabled 24/7 — competes with Hailo follow and heats the Pi. Use the light **timer** only if needed, or prefer a USB keep‑alive dongle.
