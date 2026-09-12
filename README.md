# AURA — Offline Edge-AI Air-Quality Sentinel for Wildfire Smoke

**One Arduino UNO Q. No internet. AURA measures the local air, computes Canada's official smoke-adjusted health index, translates it into clear guidance, and is being extended to forecast short-term trends and drive local actuators — entirely at the edge.**

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

AURA is a self-contained edge node that measures the local air, computes the **AQHI+** (the smoke-adjusted Air Quality Health Index, using the wildfire override adopted in Alberta and British Columbia) **on-device**, turns it into clear guidance, and drives a local display, status LEDs, and a local web dashboard — **fully offline**, on a single **Arduino UNO Q**, deployable where the grid and the network don't reach.

---

## Why the Arduino UNO Q — the dual-brain architecture

AURA is built on the UNO Q's asymmetric, dual-processor design:

- **STM32U585 (Cortex-M33) — real-time control:** reads the I2C sensors without interruption and drives the OLED and the RGB status LEDs (and, next, the actuator outputs).
- **Qualcomm Dragonwing (quad Cortex-A53, Linux) — on-device intelligence:** runs the AQHI+ logic, the advisory engine, the local web dashboard, and — next — the time-series database and the TinyML models.

The two communicate through the **Arduino Router Bridge (RPC)**, so computation or disk I/O on the Linux side never stalls real-time sensing on the MCU. This multimodal, on-device workload is exactly what the UNO Q is designed for.

---

## Key capabilities

- **Comprehensive air sensing** — PM1/2.5/4/10, CO₂, VOC, NOx, temperature, and humidity from a single factory-calibrated Sensirion pair (SEN55 + SCD41) over Qwiic/I2C.
- **On-device AQHI+ computation** — readings are converted to the smoke-adjusted health index, so the device reports an official-grade health risk rather than a raw number.
- **Clear health guidance** — the index is mapped to Environment Canada's advisory messages for the general population and for at-risk groups (children, the elderly, respiratory and cardiac conditions). The risk level is computed **deterministically** from the index.
- **Live sensor-health monitoring** — the SEN55 device status register is polled continuously and decoded: fan failure, laser fault, gas-sensor fault, humidity/temperature fault, fan-speed warning, and auto-cleaning state, surfaced in the logs and on the dashboard, with a manual fan-cleaning command.
- **Self-heating compensation** — a documented temperature offset is programmed into both sensors, which also corrects the relative humidity they derive from it.
- **Local interfaces** — a 1.3" OLED with a product-grade start-up sequence, an ambient RGB status colour, and a responsive web dashboard.
- **Fully offline** — all sensing, computation, and presentation run on-device. No internet, no cloud, no account.

---

## The AQHI+ methodology

Environment Canada's national **AQHI** combines three pollutants:

```
AQHI = (1000 / 10.4) × [ (e^(0.000871·NO₂) − 1) + (e^(0.000537·O₃) − 1) + (e^(0.000487·PM2.5) − 1) ]
```

with NO₂ and O₃ in ppb and PM2.5 in µg/m³. During smoke episodes this formula was found to **under-report** health risk, so Alberta and British Columbia publish an override — the **AQHI+** — computed from hourly PM2.5 alone:

```
AQHI+  = ceil( hourly mean PM2.5 / 10 )
published index = max( AQHI, AQHI+ )        reported as "10+" above 10
```

AURA implements both terms. It measures PM2.5 but not NO₂ or O₃, so its three-pollutant term is a lower bound and the smoke term is what carries the signal — which is precisely the scenario AURA targets. The device is explicit about this in the dashboard rather than hiding it.

Categories follow the national scale: **1–3 low · 4–6 moderate · 7–10 high · 10+ very high.**

---

## Local interfaces

- **Local web dashboard** — served by the UNO Q at `http://<device-ip>:7000` over its own network: the hero AQHI+ figure with its risk category and advisory, the advisor message, all eight measurements with status badges, a PM2.5 history chart, the decoded sensor-health register, and the live log stream. Light and dark themes, phone-first layout.
- **1.3" OLED (128×64)** — start-up animation and greeting, then a permanent screen: power/battery icon, the AQHI+ figure and category, live PM2.5 / CO₂ / VOC / temperature / humidity, and a scrolling message band driven from the Linux side.
- **RGB status LEDs** — an ambient air-quality colour (blue while warming up, green → amber → red as risk rises).

---

## Power

- **Indoor:** USB-C (5 V / 3 A).
- **Off-grid / field:** 7–24 V on `Vin` from a solar panel + MPPT charge controller + LiFePO₄ battery, for deployment in remote communities without power or connectivity. The firmware already renders a battery state on the OLED; the fuel-gauge input is the next hardware step.

---

## Hardware (Bill of Materials)

| Part | Role |
|---|---|
| Arduino UNO Q (4 GB / 32 GB) | Dual-brain compute (STM32U585 + Dragonwing) |
| SparkFun Indoor Air-Quality Combo — SCD41 + SEN55 (Qwiic) | CO₂, PM1/2.5/4/10, VOC, NOx, T, RH |
| SparkFun Qwiic OLED 1.3" (128×64) | Local readout |
| Qwiic cables | Plug-and-play I2C wiring |
| *(next)* Add-on actuator modules (buzzer, relay) | Alarm / fan / purifier |
| *(field)* Solar panel + MPPT + 12 V LiFePO₄ + fuses | Off-grid power |
| Custom 3D-printed enclosure | Field-ready housing, swappable battery |

### Wiring note — the Qwiic bus is `Wire1`

On the UNO Q the Qwiic connector is the board's **secondary** I2C bus: in a sketch it is `Wire1`, not `Wire` (`Wire` is A4/A5). The firmware initialises both and probes `Wire1` first, then falls back to `Wire`, so either wiring works. I2C map: `0x69` SEN55, `0x62` SCD41, `0x3D` OLED (`0x3C` with the address jumper closed).

---

## Software / stack

- **Arduino App Lab** — C++ sketch (MCU) + Python (MPU) + Bricks
- **Bricks:** `arduino:web_ui` (+ `arduino:dbstorage_sqlstore` and the AI bricks next)
- **Sensor drivers:** Sensirion I2C SEN5x & SCD4x, SparkFun Qwiic OLED
- **Health logic:** deterministic AQHI+ (Canadian, with the wildfire-smoke override)
- **Edge AI (in development):** Edge Impulse (TinyML — smoke-event detection, short-horizon prediction)
- **Language model (planned layer):** llama.cpp + a compact GGUF model

---

## Current status

**Implemented and demonstrated**

- [x] Multi-parameter sensing (SEN55 + SCD41) over Qwiic/I2C
- [x] MCU → MPU data flow via the Arduino Router Bridge
- [x] On-device AQHI+ computation with Environment Canada advisory messages
- [x] Live local web dashboard (index, advisor, tiles, PM2.5 chart, logs)
- [x] 1.3" OLED UI with start-up sequence, status screen, and message band
- [x] RGB status LEDs driven from the health index
- [x] SEN55 device-status decoding (fan / laser / gas / RHT faults) + manual fan cleaning
- [x] Sensor self-heating temperature compensation

**In development**

- [ ] SQLite time-series logging (`dbstorage_sqlstore`) and history beyond the in-memory window
- [ ] TinyML calibration, smoke-event detection, and short-horizon prediction (Edge Impulse)
- [ ] On-device language-model phrasing of the guidance, multilingual
- [ ] Battery fuel gauge feeding the OLED power icon
- [ ] Actuator add-on modules (alarm, fan/purifier relay)

---

## Roadmap

- **Camera-based visibility estimation** feeding the index (visibility is an official input to Alberta's smoke messaging) and cross-checking the particulate sensor.
- **Voice interaction** for hands-free, multilingual guidance.
- **Community mesh** — multiple AURA nodes forming a local air-quality map across a community, without internet.
- **Ruggedised, weatherproof solar enclosure** for permanent outdoor deployment.

---

## Impact

AURA targets the **data deserts** the national network cannot reach — rural, remote, and Indigenous communities, which are affected disproportionately by wildfire smoke and by connectivity loss during fires. Its offline, low-cost, community-deployable design aligns with federal environmental-monitoring funding programs, pointing to a viable institutional (B2G) path in addition to consumer use.

---

## Repository structure

This repository *is* the Arduino App Lab application, so the top folders are the ones App Lab requires:

```
app.yaml            — app metadata and the Bricks it depends on
python/main.py      — MPU (Linux): AQHI+, advisory engine, dashboard API
sketch/sketch.ino   — MCU (STM32U585): sensors, OLED, status LEDs, Bridge
sketch/sketch.yaml  — pinned Arduino libraries for the sketch
assets/index.html   — the local web dashboard
docs/               — market study, AQHI+ references, build log
hardware/           — schematic, BOM, 3D enclosure (STL)
edge-ai/            — Edge Impulse project and benchmarks
media/              — demonstration photos and captures
```

## Running it

1. Connect the SparkFun SCD41+SEN55 combo and the 1.3" OLED to the UNO Q's Qwiic connector (daisy-chained).
2. Open the app in Arduino App Lab and press ▶. The first start compiles the sketch and flashes the MCU, which takes a few minutes.
3. Watch the console, or open `http://<device-ip>:7000` from any device on the same network.

The app runs on the board: once started it keeps running with App Lab closed and the USB cable unplugged, as long as the UNO Q is powered.

---

*Built on Arduino UNO Q with App Lab · Edge AI · 100% offline · Made in Alberta, Canada.*
