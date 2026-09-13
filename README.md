# AURA — Offline Edge-AI Air-Quality Sentinel for Wildfire Smoke

**One Arduino UNO Q. No internet. AURA measures the local air, computes Canada's official smoke-adjusted health index, classifies the aerosol signature with an on-device TinyML model, translates it all into clear guidance, and drives real actuators — entirely at the edge.**

---

## The problem

Wildfire smoke has become a chronic public-health crisis in Canada. Fine particulate matter (PM2.5) penetrates deep into the lungs and bloodstream and is linked to an estimated **1,900–2,500 premature deaths per year** and **~$18–19B/year** in health costs nationally.

Yet the country **measures its own air poorly** where it matters most. The national reference network has roughly **200 stations**, concentrated in cities and near industry — leaving vast **"data deserts"** across rural, remote, and Indigenous communities. These are exactly the places where cellular and internet connectivity fail *first* during major fires.

Existing consumer monitors force a trade-off:

- The **connected** ones (PurpleAir, IQAir) need Wi-Fi/cloud to provide any context.
- The **offline** ones (e.g., Temtop) are analytically mute — they show a raw number (`47 µg/m³`) a non-expert cannot interpret.

**AURA fills the gap between the two: offline *and* intelligent.**

---

## What AURA is

AURA is a self-contained edge-AI node that measures the local air, computes the **AQHI+** (the smoke-adjusted Air Quality Health Index, using the wildfire override adopted in Alberta and British Columbia) **on-device**, runs a **TinyML aerosol classifier** trained on its own logged data, turns everything into clear guidance, and drives an OLED, RGB status LEDs, a web dashboard, and three actuator outputs — **fully offline**, on a single **Arduino UNO Q**, deployable where the grid and the network don't reach.

---

## Why the Arduino UNO Q — the dual-brain architecture

AURA is built on the UNO Q's asymmetric, dual-processor design:

- **STM32U585 (Cortex-M33) — real-time control:** reads the I2C sensors without interruption and drives the OLED, the RGB status LEDs, the actuator outputs, and the physical stand-down button.
- **Qualcomm Dragonwing (quad Cortex-A53, Linux) — on-device intelligence:** runs the AQHI+ logic, the SQLite time-series store, the Edge Impulse classifier (`.eim`), the advisory engine, and the local web dashboard.

The two communicate through the **Arduino Router Bridge (RPC)**, so computation or disk I/O on the Linux side never stalls real-time sensing or the alarm logic on the MCU. This multimodal, on-device workload is exactly what the UNO Q is designed for.

---

## Key capabilities

- **Comprehensive air sensing** — PM1/2.5/4/10, CO₂, VOC, NOx, temperature, and humidity from a single factory-calibrated Sensirion pair (SEN55 + SCD41) over Qwiic/I2C.
- **On-device AQHI+ computation** — two indices are computed and shown side by side: the **official hourly AQHI+** (regulatory, health-dose oriented) and a **5-minute live index** that drives the alarm path with hysteresis, so the alarm follows the room, not the hour.
- **On-device TinyML classification** — an Edge Impulse model (`edge-ai/*.eim`) runs as a local process on the Linux side and classifies the aerosol signature (`clean` / `cooking` / `smoke`) from a 5-minute window of PM channels plus the PM1/PM10 ratio. The advisory engine uses it to tell wildfire-type smoke from harmless cooking aerosols; hard safety warnings stay rule-based and never depend on the model.
- **On-device MLOps loop** — every sample is logged to SQLite with an operator-set label (dashboard buttons + backfill), and the labelled dataset exports as CSV (raw or class-balanced) for retraining in Edge Impulse: **collect → label → export → train → deploy → keep collecting.**
- **Clear health guidance** — the AQHI+ category maps to Environment Canada's advisory messages for the general population and for at-risk groups.
- **Real actuator outputs with human override** — alarm, purifier-relay, and auxiliary lines driven by the MCU, plus a **physical stand-down button** that pauses all outputs for 10 minutes (burnt-toast protection) without ever hiding the on-screen warning.
- **Live sensor-health monitoring** — the SEN55 status register decoded continuously (fan/laser/gas/RHT faults), with a manual fan-cleaning command.
- **Fully offline** — sensing, storage, inference, and presentation all run on-device, and the board can broadcast **its own Wi-Fi access point** when no known network is available.

---

## Hardware map — every port used

### I2C (Qwiic chain on `Wire1`; `Wire` = A4/A5 is probed as fallback)

| Address | Device |
|---|---|
| `0x69` | Sensirion SEN55 — PM1/2.5/4/10, VOC, NOx, T, RH |
| `0x62` | Sensirion SCD41 — CO₂, T, RH |
| `0x3D` | SparkFun Qwiic OLED 1.3" 128×64 (`0x3C` if its jumper is closed) |

> **UNO Q wiring note:** the Qwiic connector is the board's **secondary** I2C bus — `Wire1` in a sketch, not `Wire`. The firmware probes `Wire1` first, then falls back to `Wire`, so either wiring works.

### Digital pins (MCU side, 3.3 V logic)

| Pin | Direction | Role |
|---|---|---|
| `D2` | output, active-HIGH | **Alarm** — buzzer/siren module. Fast 1.7 Hz beeping at very-high risk, one short beep every 5 s at high risk |
| `D3` | output, active-HIGH | **Relay** — fan / air-purifier / HVAC module. Continuously on while an alert is raised |
| `D4` | output, active-HIGH | **Auxiliary** — spare output, on at very-high risk |
| `D5` | input, `INPUT_PULLUP` | **Stand-down push button** to GND. One press pauses all three outputs for 10 min; a second press re-arms immediately |

Drive inductive loads (fans, purifiers) through a proper relay/driver module — never directly from a GPIO. On-board RGB LEDs (active-low) show the ambient risk colour: blue = warming up, green → amber → red.

### Network ports

| Port | Service |
|---|---|
| `7000/tcp` | Web dashboard + JSON API (`/data`, `/export`, `/label`, `/relabel`, `/fan-clean`, `/self-test`, `/simulate-alert`, `/silence`, `/pause…`) |

---

## Connecting to the dashboard

- **Same network:** open `http://<board-ip>:7000` from any phone/PC on the network the board is on (the exact URL is printed in the app console at startup).
- **No network available — AURA broadcasts its own:** the board carries a fallback access-point profile (`AURA-AP`, lowest priority). When none of its known Wi-Fi networks is in range, it automatically starts an access point:
  - **SSID:** `AURA-Sentinel` · **password:** `aura2026`
  - Connect your phone to it, then open **`http://10.42.0.1:7000`**.
  - Because the profile has the lowest autoconnect priority, it never preempts a known network; it is the last resort, which is exactly the wildfire scenario.

---

## The AQHI+ methodology

Environment Canada's national **AQHI** combines three pollutants:

```
AQHI = (1000 / 10.4) × [ (e^(0.000871·NO₂) − 1) + (e^(0.000537·O₃) − 1) + (e^(0.000487·PM2.5) − 1) ]
```

with NO₂ and O₃ in ppb and PM2.5 in µg/m³. During smoke episodes this formula under-reports health risk, so Alberta and British Columbia publish an override — the **AQHI+** — computed from hourly PM2.5 alone:

```
AQHI+  = ceil( hourly mean PM2.5 / 10 )
published index = max( AQHI, AQHI+ )        reported as "10+" above 10
```

AURA implements both terms (its three-pollutant term is a lower bound since it does not measure NO₂/O₃ — the smoke term carries the signal, which is the scenario AURA targets). The rolling hourly window is persisted in SQLite and **survives restarts and power cuts**. Categories: **1–3 low · 4–6 moderate · 7–10 high · 10+ very high.**

**Two time scales by design:** the published index stays on the hourly mean (that is what makes it the official AQHI+ and the right measure of inhaled dose), while the **live 5-minute index** drives the alarm and relay with hysteresis (raise at 7, release below 6) so actuators engage — and release — within minutes of the room actually changing.

---

## The TinyML model — and its honest limitations

- **Features:** 5-minute windows (30 samples at 0.1 Hz) of PM1.0, PM2.5, PM4.0, PM10 and the **PM1/PM10 ratio** — a scale-free feature observed at ~0.95 for cooking-oil aerosols vs ~0.84–0.87 for biomass smoke in our recordings, which separates the classes without depending on event intensity.
- **Deliberately excluded:** the VOC/NOx indices (the SGP41's index is adaptive — it re-baselines on the last 24 h, so the same air can read 100 one evening and 359 the next morning: not a stationary feature), the AQHI (computed *from* PM2.5 — using it would leak the answer), and temperature/humidity (they encode time-of-day, not aerosol physics).
- **Runtime:** the exported Linux AARCH64 `.eim` runs as a local process, spoken to over a Unix socket by a ~150-line stdlib-only runner (`python/eim_runner.py`) — no heavyweight SDK, no cloud.
- **Role in the decision chain:** hard safety warnings (fast PM rise, AQHI ≥ 7, high CO₂) are deterministic and fire regardless of the model. The model refines the *interpretation* — smoke vs cooking vs clean — and its verdict, confidence, and per-class scores are always shown on the dashboard.

**Known limitation (current model):** the training dataset came from a single overnight collection session — ~12.5 h `clean`, ~3.6 h `smoke` (burning paper and bay leaf, including full decay curves), but **only ~32 min of `cooking`**, which produced zero validation windows for that class. The published metrics (100 % accuracy, 24 validation windows) therefore only demonstrate `clean` vs `smoke` separation on a small set, and the `cooking` class must be considered **untested**. The on-device MLOps loop exists precisely to fix this: keep collecting labelled cooking sessions, re-export, retrain, redeploy. Until then the deterministic safety layer guarantees no dangerous condition is missed — at the cost of possible false "smoke" interpretations during intense cooking.

---

## On-device data & MLOps

One row every 10 s in SQLite (`data/aura.db`): all channels + AQHI+ + operator label. Retention 30 days (~260k rows, ~20 MB), hourly sweep. Dashboard tools: per-label row/minute counts, live label switching, **backfill** (retag the last 5/10/20 min when you started an event late), CSV export **raw or class-balanced** (events kept in full, `clean` thinned evenly across the whole span).

---

## Power

- **Indoor:** USB-C (5 V / 3 A).
- **Off-grid / field:** 7–24 V on `Vin` from a solar panel + MPPT charge controller + LiFePO₄ battery. The OLED already renders a battery/power icon; the fuel-gauge input (`set_power_state` Bridge channel) is the next hardware step.

---

## Bill of materials

| Part | Role |
|---|---|
| Arduino UNO Q (4 GB / 32 GB) | Dual-brain compute (STM32U585 + Dragonwing) |
| SparkFun Indoor Air-Quality Combo — SCD41 + SEN55 (Qwiic) | CO₂, PM, VOC, NOx, T, RH |
| SparkFun Qwiic OLED 1.3" (128×64) | Local readout + alert takeover screen |
| Qwiic cables | Plug-and-play I2C |
| Buzzer module, relay module, push button | Alarm / purifier / stand-down on D2–D5 |
| *(field)* Solar panel + MPPT + 12 V LiFePO₄ + fuses | Off-grid power |
| Custom 3D-printed enclosure | Field-ready housing, swappable battery |

---

## Software stack

- **Arduino App Lab** — C++ sketch (MCU) + Python (MPU) + Bricks
- **Bricks:** `arduino:web_ui`, `arduino:dbstorage_sqlstore`
- **Sensor drivers:** Sensirion I2C SEN5x & SCD4x, SparkFun Qwiic OLED (v1.0.13 pinned for Zephyr compatibility)
- **Edge AI:** Edge Impulse (Flatten + classifier), deployed as Linux AARCH64 `.eim`, driven by a dependency-free Python runner
- **Health logic:** deterministic AQHI+ (Canadian, with the wildfire-smoke override)

---

## Current status

**Implemented and demonstrated**

- [x] Multi-parameter sensing (SEN55 + SCD41) over Qwiic/I2C, with self-heating temperature compensation
- [x] MCU ↔ MPU data flow via the Arduino Router Bridge
- [x] On-device AQHI+ (hourly, restart-persistent) + live 5-min index with hysteresis alarm path
- [x] On-device TinyML aerosol classifier (`.eim`) wired into the advisory engine
- [x] SQLite time-series logging with labelling, backfill, balanced CSV export (full MLOps loop)
- [x] Live web dashboard (responsive, two-column on desktop; light/dark)
- [x] 1.3" OLED UI: start-up sequence, status screen, full-screen flashing danger takeover
- [x] RGB status LEDs + three actuator outputs (D2/D3/D4) with real-alert triggering
- [x] Physical stand-down button (D5): 10-min actuator pause, second press re-arms
- [x] Fallback Wi-Fi access point (`AURA-Sentinel`) for network-less deployments
- [x] SEN55 health-register decoding + manual fan cleaning

**In development**

- [ ] Retrain the classifier with more `cooking` data (see limitation above) and a low-intensity smoke session
- [ ] Short-horizon PM2.5 *regression* forecast in Edge Impulse (the current 15-min forecast is a guarded linear trend)
- [ ] Battery fuel gauge feeding the OLED power icon
- [ ] On-device language-model phrasing of the guidance, multilingual

---

## Roadmap

- **Camera-based visibility estimation** feeding the index (visibility is an official input to Alberta's smoke messaging) and cross-checking the particulate sensor.
- **Voice interaction** for hands-free, multilingual guidance.
- **Community mesh** — multiple AURA nodes forming a local air-quality map across a community, without internet.
- **Ruggedised, weatherproof solar enclosure** for permanent outdoor deployment.
- **Validation against reference stations** — correlating AURA's AQHI+ with the nearest government station (AQICN) to quantify instrument-grade accuracy.

---

## Impact

AURA targets the **data deserts** the national network cannot reach — rural, remote, and Indigenous communities, disproportionately affected by wildfire smoke and by connectivity loss during fires. Its offline, low-cost, community-deployable design aligns with federal environmental-monitoring funding programs, pointing to a viable institutional (B2G) path in addition to consumer use.

---

## Repository structure

This repository *is* the Arduino App Lab application:

```
app.yaml              — app metadata, exposed port, Bricks
python/main.py        — MPU: AQHI+, advisory engine, TinyML integration, storage, API
python/eim_runner.py  — dependency-free Edge Impulse .eim runner (Unix-socket JSON)
sketch/sketch.ino     — MCU: sensors, OLED, LEDs, actuators, stand-down button, Bridge
sketch/sketch.yaml    — pinned Arduino libraries
assets/index.html     — the local web dashboard (single file, no build step)
edge-ai/              — trained .eim model + Edge Impulse evaluation metrics
data/aura.db          — on-device time-series store (created at runtime)
```

## Running it

1. Chain the SCD41+SEN55 combo and the OLED on the Qwiic connector; wire the optional buzzer (D2), relay (D3) and stand-down button (D5).
2. Open the app in Arduino App Lab and press ▶ (first start compiles and flashes the sketch — a few minutes).
3. Open `http://<board-ip>:7000` — or, with no network around, join Wi-Fi `AURA-Sentinel` (password `aura2026`) and open `http://10.42.0.1:7000`.

Once started, the app runs on the board: App Lab can be closed and the USB cable unplugged.

---

*Built on Arduino UNO Q with App Lab · Edge AI · 100% offline · Made in Alberta, Canada.*
