"""
AURA - Offline edge-AI air-quality sentinel
MPU side (Qualcomm Dragonwing, Linux): health-index computation, advisory text,
local web dashboard. Sensor readings arrive from the MCU over the Router Bridge;
the index and the advisory go back the same way to drive the OLED and the LEDs.

Everything here runs on the board. No cloud, no internet.
"""

import math
import time
from collections import deque

from arduino.app_utils import App, Bridge
from arduino.app_bricks.web_ui import WebUI

# ------------------------------------------------------------------ configuration

AQHI_WINDOW_S = 3600  # the AQHI+ override is defined on the hourly PM2.5 mean
TREND_RECENT_S = 300  # short window used to detect a developing smoke event
TREND_BASE_S = 1200  # window it is compared against
HISTORY_POINTS = 180  # samples kept for the dashboard chart
LOG_LINES = 300
UI_PERIOD_S = 2.0
STATE_REQUEST_PERIOD_S = 120  # ask the MCU to re-announce its state and sensor health

# SEN55 device status register bits (SEN5x datasheet, section 6.1.4).
STATUS_BITS = (
    ("fan_error", 4, "Fan failure: fan is on but reads 0 RPM (blocked or broken)"),
    ("laser_error", 5, "Laser current out of range"),
    ("rht_error", 6, "Internal humidity/temperature sensor error"),
    ("gas_error", 7, "VOC/NOx gas sensor error"),
    ("fan_cleaning", 19, "Automatic fan cleaning in progress"),
    ("fan_speed_warning", 21, "Fan speed out of specification"),
)

# Environment Canada's AQHI advisory messages, by risk category.
ADVICE = {
    "Low": (
        "Ideal air quality for outdoor activities.",
        "Enjoy your usual outdoor activities.",
    ),
    "Moderate": (
        "No need to modify your usual outdoor activities unless you experience symptoms.",
        "Consider reducing or rescheduling strenuous activities outdoors if you experience"
        " symptoms.",
    ),
    "High": (
        "Consider reducing or rescheduling strenuous activities outdoors if you experience"
        " symptoms.",
        "Reduce or reschedule strenuous activities outdoors. Children and the elderly should"
        " also take it easy.",
    ),
    "Very high": (
        "Reduce or reschedule strenuous activities outdoors, especially if you experience"
        " symptoms.",
        "Avoid strenuous activities outdoors. Children and the elderly should also avoid"
        " outdoor physical exertion.",
    ),
}

SHORT_LABEL = {"Low": "LOW", "Moderate": "MODERATE", "High": "HIGH", "Very high": "V.HIGH"}

# ------------------------------------------------------------------ state

ui = WebUI()

latest = {"sen55": None, "scd41": None}
health = {"raw": None, "at": None}
pm25_samples = deque()  # (timestamp, pm2.5) covering the last AQHI window
history = deque(maxlen=HISTORY_POINTS)
logs = deque(maxlen=LOG_LINES)
index_state = {"value": None, "display": "--", "category": None, "window_s": 0, "plus": None,
               "three_pollutant": None, "pm25_mean": None}
advisory_state = {"text": "Warming up sensors", "source": "rules"}
pushed = {"index": None, "label": None, "advisory": None, "state_asked": 0.0}


def clean(value):
    """Turn NaN into None so the JSON payload stays valid."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


def log(line):
    stamped = f"{time.strftime('%H:%M:%S')} {line}"
    logs.append(stamped)
    print(stamped, flush=True)


# ------------------------------------------------------------------ health index

def pm25_mean(window_s):
    """Mean PM2.5 over the last window_s seconds, plus the span actually covered."""
    if not pm25_samples:
        return None, 0
    now = time.time()
    values = [v for (t, v) in pm25_samples if now - t <= window_s]
    if not values:
        return None, 0
    span = now - max(now - window_s, pm25_samples[0][0])
    return sum(values) / len(values), int(span)


def aqhi_three_pollutant(pm25, no2_ppb=0.0, o3_ppb=0.0):
    """
    Environment Canada's national AQHI formula.

    AURA measures PM2.5 only, so NO2 and O3 are zero here. That makes this term a
    lower bound on the true three-pollutant AQHI; the smoke override below is what
    carries the signal during a wildfire event, which is the case AURA targets.
    """
    total = (
        (math.exp(0.000871 * no2_ppb) - 1)
        + (math.exp(0.000537 * o3_ppb) - 1)
        + (math.exp(0.000487 * pm25) - 1)
    )
    return (1000.0 / 10.4) * total


def aqhi_plus(pm25_hourly):
    """Wildfire-smoke override used in Alberta and B.C.: ceil(hourly PM2.5 / 10)."""
    return max(1, math.ceil(pm25_hourly / 10.0))


def category_for(value):
    if value <= 3:
        return "Low"
    if value <= 6:
        return "Moderate"
    if value <= 10:
        return "High"
    return "Very high"


def update_index():
    mean, window = pm25_mean(AQHI_WINDOW_S)
    if mean is None:
        index_state.update(value=None, display="--", category=None, window_s=0, plus=None,
                           three_pollutant=None, pm25_mean=None)
        return
    base = aqhi_three_pollutant(mean)
    plus = aqhi_plus(mean)
    # The published index is the greater of the two, as Alberta and B.C. define it.
    value = max(int(round(base)), plus, 1)
    index_state.update(
        value=value,
        display="10+" if value > 10 else str(value),
        category=category_for(value),
        window_s=window,
        plus=plus,
        three_pollutant=round(base, 2),
        pm25_mean=round(mean, 1),
    )


# ------------------------------------------------------------------ advisory engine

def build_advisory():
    """
    Deterministic advisory, and the seam where the TinyML model plugs in.

    TODO(tinyml): replace the rules below with the Edge Impulse model's output
    (smoke-event classification and short-horizon PM2.5 forecast). The contract is
    the same: return (text, source) where source labels who produced the text.
    """
    recent, recent_span = pm25_mean(TREND_RECENT_S)
    base, base_span = pm25_mean(TREND_BASE_S)
    co2 = (latest["scd41"] or {}).get("co2")
    value = index_state["value"]

    if recent is None:
        return "Warming up sensors", "rules"

    # A fast climb matters more than the absolute level: it is the early warning.
    if base is not None and base_span > 120 and recent > base + 5 and recent > base * 1.3:
        return "PM2.5 rising fast - possible smoke event, close windows", "rules"

    if value is not None and value >= 7:
        return f"AQHI+ {index_state['display']} - limit outdoor exertion", "rules"

    if co2 is not None and co2 > 1200:
        return f"CO2 {co2} ppm - ventilate the room", "rules"

    if base is not None and base_span > 120 and recent < base * 0.7:
        return "Air improving, PM2.5 falling", "rules"

    if value is not None and value <= 3:
        return "Air quality is good", "rules"

    return "Air quality is moderate, stable", "rules"


# ------------------------------------------------------------------ Bridge handlers

def on_sen55_data(pm1p0, pm2p5, pm4p0, pm10p0, humidity, temperature, voc_index, nox_index):
    now = time.time()
    latest["sen55"] = {
        "pm1p0": clean(pm1p0),
        "pm2p5": clean(pm2p5),
        "pm4p0": clean(pm4p0),
        "pm10p0": clean(pm10p0),
        "humidity": clean(humidity),
        "temperature": clean(temperature),
        "voc": clean(voc_index),
        "nox": clean(nox_index),
        "at": now,
    }
    if not math.isnan(pm2p5):
        pm25_samples.append((now, pm2p5))
        while pm25_samples and now - pm25_samples[0][0] > AQHI_WINDOW_S:
            pm25_samples.popleft()
        history.append({"t": int(now), "pm25": round(pm2p5, 1)})

    log(
        f"SEN55 | PM1.0: {pm1p0:.1f} ug/m3 | PM2.5: {pm2p5:.1f} ug/m3 | "
        f"PM4.0: {pm4p0:.1f} ug/m3 | PM10: {pm10p0:.1f} ug/m3 | "
        f"Temp: {temperature:.1f} C | Hum: {humidity:.1f} % | "
        f"VOC: {voc_index:.0f} | NOx: {nox_index:.0f}"
    )


def on_scd41_data(co2, temperature, humidity):
    latest["scd41"] = {
        "co2": clean(co2),
        "temperature": clean(temperature),
        "humidity": clean(humidity),
        "at": time.time(),
    }
    log(f"SCD41 | CO2: {co2} ppm | Temp: {temperature:.1f} C | Hum: {humidity:.1f} %")


def on_sensor_status(raw):
    """SEN55 device status register, pushed by the MCU whenever it changes."""
    raw = int(raw) & 0xFFFFFFFF
    flags = {name: bool(raw & (1 << bit)) for (name, bit, _) in STATUS_BITS}
    problems = [text for (name, _, text) in STATUS_BITS if flags[name] and name != "fan_cleaning"]
    health.update(
        raw=raw,
        raw_hex=f"0x{raw:08X}",
        at=time.time(),
        problems=problems,
        ok=not problems,
        **flags,
    )


def on_log(message):
    log(f"[MCU] {message}")


# ------------------------------------------------------------------ HTTP API

def get_data():
    sen55 = latest["sen55"] or {}
    scd41 = latest["scd41"] or {}
    general, at_risk = ADVICE.get(index_state["category"], ("", ""))
    return {
        "device": "AURA",
        "index": {**index_state, "advice_general": general, "advice_at_risk": at_risk},
        "advisory": advisory_state,
        "sen55": latest["sen55"],
        "scd41": latest["scd41"],
        "health": health if health["raw"] is not None else None,
        "temperature": scd41.get("temperature", sen55.get("temperature")),
        "humidity": scd41.get("humidity", sen55.get("humidity")),
        "history": list(history),
        "logs": list(logs),
    }


def clean_fan():
    Bridge.notify("start_fan_cleaning")
    log("Fan cleaning requested from the dashboard")
    return {"ok": True}


# ------------------------------------------------------------------ main loop

def ui_loop():
    """Recompute the index and advisory, then push them to the OLED and the LEDs."""
    # The MCU finished booting before this process attached to the Bridge, so ask
    # it to re-announce which devices came up and what the status register says.
    now = time.time()
    if now - pushed["state_asked"] > STATE_REQUEST_PERIOD_S:
        pushed["state_asked"] = now
        Bridge.notify("report_state")

    update_index()
    text, source = build_advisory()
    advisory_state.update(text=text, source=source)

    value = index_state["value"]
    label = SHORT_LABEL.get(index_state["category"], "--")
    if value != pushed["index"] or label != pushed["label"]:
        pushed["index"] = value
        pushed["label"] = label
        Bridge.notify("set_health_index", -1 if value is None else value, label)
        if value is not None:
            log(f"AQHI+ {index_state['display']} ({index_state['category']}) from "
                f"{index_state['pm25_mean']} ug/m3 mean over {index_state['window_s']} s")

    if text != pushed["advisory"]:
        pushed["advisory"] = text
        Bridge.notify("set_advisory", text)
        log(f"Advisory: {text}")

    time.sleep(UI_PERIOD_S)


Bridge.provide("log", on_log)
Bridge.provide("sen55_data", on_sen55_data)
Bridge.provide("scd41_data", on_scd41_data)
Bridge.provide("sensor_status", on_sensor_status)

# The dashboard probes both paths, so the mount point of the brick does not matter.
ui.expose_api("GET", "/data", get_data)
ui.expose_api("GET", "/api/data", get_data)
ui.expose_api("POST", "/fan-clean", clean_fan)

log("AURA started - dashboard on port 7000, all processing on-device")

App.run(user_loop=ui_loop)
