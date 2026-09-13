"""
AURA - Offline edge-AI air-quality sentinel
MPU side (Qualcomm Dragonwing, Linux): health-index computation, advisory text,
time-series logging, local web dashboard. Sensor readings arrive from the MCU over
the Router Bridge; the index, the advisory and the alert level go back the same way
to drive the OLED, the status LEDs and the actuator outputs.

Everything here runs on the board. No cloud, no internet.

Threading note: the Bridge delivers sensor callbacks on its own thread, the user
loop runs on the main thread, and the web server answers on yet another. Every
shared buffer below is therefore guarded by `lock`.
"""

import math
import threading
import time
from collections import deque

from arduino.app_utils import App, Bridge
from arduino.app_bricks.dbstorage_sqlstore import SQLStore
from arduino.app_bricks.web_ui import WebUI

# ------------------------------------------------------------------ configuration

AQHI_WINDOW_S = 3600  # the AQHI+ override is defined on the hourly PM2.5 mean
# The published index has to stay on the hourly mean to remain the official AQHI+,
# but an alarm and a purifier relay must follow the air in the room right now. The
# alert path therefore runs on its own short window.
ALERT_WINDOW_S = 300
ALERT_RAISE_HIGH = 7  # live index at which the outputs engage
ALERT_RAISE_VERY_HIGH = 11
ALERT_CLEAR_BELOW = 6  # and the level it must fall under to release: hysteresis
ALARM_MUTE_MINUTES = 15
TREND_RECENT_S = 300  # short window used to detect a developing smoke event
TREND_BASE_S = 1200  # window it is compared against
FORECAST_HORIZON_S = 900  # how far ahead the trend is extrapolated
FORECAST_MIN_SPAN_S = 240  # refuse to extrapolate from a window shorter than this
DEMO_ALERT_S = 30  # how long a simulated alert stays raised
HISTORY_POINTS = 180  # samples kept in memory for the dashboard chart
LOG_LINES = 300
UI_PERIOD_S = 2.0
STATE_REQUEST_PERIOD_S = 120  # ask the MCU to re-announce its state and sensor health

# Time-series logging. One row every 10 s is ~8,600 rows/day, which keeps both the
# database and the eMMC write count modest while staying dense enough to train on.
DB_NAME = "aura.db"
DB_TABLE = "samples"
SAMPLE_PERIOD_S = 10
RETENTION_DAYS = 30
RETENTION_SWEEP_S = 3600
SUMMARY_PERIOD_S = 30  # how often the row counts are re-read from the database
EXPORT_MAX_ROWS = 20000  # cap on one CSV export, about 55 h at 10 s

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

# Alert levels mirrored from the sketch.
ALERT_NONE, ALERT_HIGH, ALERT_VERY_HIGH = 0, 1, 2
ALERT_TEXT = {
    ALERT_HIGH: "LIMIT OUTDOOR EXERTION",
    ALERT_VERY_HIGH: "STAY INDOORS, CLOSE WINDOWS",
}

# Labels written alongside each sample, for supervised training later.
LABEL_CLEAN = "clean"
VALID_LABELS = (LABEL_CLEAN, "smoke", "cooking", "unknown")

# ------------------------------------------------------------------ state

ui = WebUI()
db = SQLStore(DB_NAME)

lock = threading.Lock()  # guards pm25_samples, history and logs

latest = {"sen55": None, "scd41": None}
health = {"raw": None, "at": None}
pm25_samples = deque()  # (timestamp, pm2.5) covering the last AQHI window
history = deque(maxlen=HISTORY_POINTS)
logs = deque(maxlen=LOG_LINES)
index_state = {"value": None, "display": "--", "category": None, "window_s": 0, "plus": None,
               "three_pollutant": None, "pm25_mean": None,
               # Live figures, from the short window, are what drive the alert.
               "live_value": None, "live_display": "--", "live_category": None,
               "live_pm25": None}
advisory_state = {"text": "Warming up sensors", "source": "rules"}
forecast_state = {"pm25": None, "horizon_s": FORECAST_HORIZON_S, "trend": None, "source": "trend"}
dataset_state = {"label": LABEL_CLEAN, "rows": 0, "written": 0, "oldest": None, "newest": None,
                 "by_label": {}, "error": None}
pushed = {"index": None, "label": None, "advisory": None, "alert": None, "state_asked": 0.0,
          "sampled": 0.0, "swept": 0.0, "summarised": 0.0, "live": None}
# Operator-triggered rehearsal of the danger path, so the OLED takeover and the
# actuator outputs can be shown without waiting for genuinely dangerous air.
demo = {"level": ALERT_NONE, "until": 0.0}
# Hush button, like a smoke detector's: silences the buzzer while leaving the
# purifier relay and the on-screen warning in place.
mute = {"until": 0.0, "pushed": None}


def clean(value):
    """Turn NaN into None so the JSON payload stays valid."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return value


def log(line):
    stamped = f"{time.strftime('%H:%M:%S')} {line}"
    with lock:
        logs.append(stamped)
    print(stamped, flush=True)


# ------------------------------------------------------------------ time-series store

def init_database():
    """One row per sample period, with the label that was active when it was taken."""
    try:
        db.create_table(DB_TABLE, {
            "ts": "INTEGER PRIMARY KEY",  # epoch seconds; one row per sample period
            "pm1p0": "REAL",
            "pm2p5": "REAL",
            "pm4p0": "REAL",
            "pm10p0": "REAL",
            "voc": "REAL",
            "nox": "REAL",
            "co2": "INTEGER",
            "temperature": "REAL",
            "humidity": "REAL",
            "aqhi": "INTEGER",
            "label": "TEXT",
        })
        log(f"Time-series store ready: {DB_NAME}")
        log_dataset_summary()
        warm_start_window()
    except Exception as error:  # never let storage take the whole app down
        dataset_state["error"] = str(error)
        log(f"Time-series store unavailable: {error}")


def warm_start_window():
    """
    Refill the rolling window from the database at start-up.

    Without this the hourly mean - and therefore the published AQHI+ - would begin
    again from a single sample after every restart, which on a field node is one
    power cut away. The stored rows are 10 s apart against 2 s live, so the restored
    part carries slightly less weight in the mean; that washes out within the hour
    and is much better than starting blind.
    """
    cutoff = int(time.time()) - AQHI_WINDOW_S
    try:
        rows = db.execute_sql(
            f"SELECT ts, pm2p5 FROM {DB_TABLE} WHERE ts >= {cutoff} AND pm2p5 IS NOT NULL "
            f"ORDER BY ts ASC"
        ) or []
    except Exception as error:
        log(f"Could not restore the rolling window: {error}")
        return
    if not rows:
        return
    with lock:
        for row in rows:
            pm25_samples.append((float(row["ts"]), float(row["pm2p5"])))
            history.append({"t": int(row["ts"]), "pm25": round(float(row["pm2p5"]), 1)})
    log(f"Restored {len(rows)} samples from the store, index continues across the restart")


def refresh_dataset_summary():
    """
    Read the counts back out of the database rather than trusting a counter.

    This is what proves rows are really landing, and it is also the number that
    matters when deciding whether there is enough labelled data to train on.
    """
    try:
        rows = db.execute_sql(
            f"SELECT label, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts "
            f"FROM {DB_TABLE} GROUP BY label ORDER BY n DESC"
        ) or []
        by_label = {(row.get("label") or "unlabelled"): row.get("n") or 0 for row in rows}
        first = [row["first_ts"] for row in rows if row.get("first_ts") is not None]
        last = [row["last_ts"] for row in rows if row.get("last_ts") is not None]
        dataset_state.update(
            rows=sum(by_label.values()),
            by_label=by_label,
            oldest=min(first) if first else None,
            newest=max(last) if last else None,
            error=None,
        )
    except Exception as error:
        dataset_state["error"] = str(error)


def log_dataset_summary():
    refresh_dataset_summary()
    if dataset_state["error"]:
        log(f"Dataset read-back failed: {dataset_state['error']}")
        return
    breakdown = ", ".join(
        f"{name}={count} ({count * SAMPLE_PERIOD_S // 60} min)"
        for name, count in dataset_state["by_label"].items()
    ) or "empty"
    age = ""
    if dataset_state["newest"]:
        age = f", newest row {int(time.time()) - dataset_state['newest']} s old"
    log(f"Dataset: {dataset_state['rows']} rows - {breakdown}{age}")


def store_sample(now):
    sen55 = latest["sen55"] or {}
    scd41 = latest["scd41"] or {}
    if not sen55 and not scd41:
        return
    row = {
        "ts": int(now),
        "pm1p0": sen55.get("pm1p0"),
        "pm2p5": sen55.get("pm2p5"),
        "pm4p0": sen55.get("pm4p0"),
        "pm10p0": sen55.get("pm10p0"),
        "voc": sen55.get("voc"),
        "nox": sen55.get("nox"),
        "co2": scd41.get("co2"),
        "temperature": scd41.get("temperature", sen55.get("temperature")),
        "humidity": scd41.get("humidity", sen55.get("humidity")),
        "aqhi": index_state["value"],
        "label": dataset_state["label"],
    }
    try:
        db.store(DB_TABLE, row)
        dataset_state["written"] += 1
        dataset_state["error"] = None
    except Exception as error:
        dataset_state["error"] = str(error)
        log(f"Sample not stored: {error}")


def sweep_retention(now):
    cutoff = int(now) - RETENTION_DAYS * 86400
    try:
        db.delete(DB_TABLE, condition=f"ts < {cutoff}")
    except Exception as error:
        dataset_state["error"] = str(error)
    log_dataset_summary()


# ------------------------------------------------------------------ health index

def pm25_mean(window_s):
    """Mean PM2.5 over the last window_s seconds, plus the span actually covered."""
    now = time.time()
    with lock:
        if not pm25_samples:
            return None, 0
        oldest = pm25_samples[0][0]
        values = [v for (t, v) in pm25_samples if now - t <= window_s]
    if not values:
        return None, 0
    span = now - max(now - window_s, oldest)
    return sum(values) / len(values), int(span)


def pm25_window(window_s):
    """Raw (timestamp, value) pairs inside a window, copied under the lock."""
    now = time.time()
    with lock:
        return [(t, v) for (t, v) in pm25_samples if now - t <= window_s]


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


def index_from_mean(mean):
    """Published index: the greater of the two terms, as Alberta and B.C. define it."""
    base = aqhi_three_pollutant(mean)
    plus = aqhi_plus(mean)
    value = max(int(round(base)), plus, 1)
    return value, base, plus


def update_index():
    # Live figures first: they react within minutes and drive the alert path.
    live_mean, _ = pm25_mean(ALERT_WINDOW_S)
    if live_mean is None:
        index_state.update(live_value=None, live_display="--", live_category=None,
                           live_pm25=None)
    else:
        live_value, _, _ = index_from_mean(live_mean)
        index_state.update(
            live_value=live_value,
            live_display="10+" if live_value > 10 else str(live_value),
            live_category=category_for(live_value),
            live_pm25=round(live_mean, 1),
        )

    mean, window = pm25_mean(AQHI_WINDOW_S)
    if mean is None:
        index_state.update(value=None, display="--", category=None, window_s=0, plus=None,
                           three_pollutant=None, pm25_mean=None)
        return
    value, base, plus = index_from_mean(mean)
    index_state.update(
        value=value,
        display="10+" if value > 10 else str(value),
        category=category_for(value),
        window_s=window,
        plus=plus,
        three_pollutant=round(base, 2),
        pm25_mean=round(mean, 1),
    )


# ------------------------------------------------------------------ prediction

def update_forecast():
    """
    Short-horizon PM2.5 forecast.

    Today this is an ordinary least-squares slope over the recent window,
    extrapolated FORECAST_HORIZON_S ahead. It exists so the whole chain - forecast
    to advisory to OLED to actuators - is wired and demonstrable.

    TODO(tinyml): replace the fit below with the Edge Impulse regression model's
    output and set source="tinyml". Keep the same three keys (pm25, trend, source)
    and nothing downstream needs to change.
    """
    points = pm25_window(TREND_RECENT_S)
    # Extrapolating a 15-minute horizon from a couple of minutes of data produces
    # nonsense, so the window has to cover a real span before a slope is trusted.
    if len(points) < 6 or (points[-1][0] - points[0][0]) < FORECAST_MIN_SPAN_S:
        forecast_state.update(pm25=None, trend=None, source="trend")
        return
    t0 = points[0][0]
    xs = [t - t0 for (t, _) in points]
    ys = [v for (_, v) in points]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    variance = sum((x - mean_x) ** 2 for x in xs)
    if variance == 0:
        forecast_state.update(pm25=None, trend=None, source="trend")
        return
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / variance
    predicted = max(0.0, mean_y + slope * (xs[-1] - mean_x + FORECAST_HORIZON_S))
    # A linear fit has no idea that aerosols saturate and decay, so cap how far
    # from the present the projection is allowed to land.
    current = ys[-1]
    predicted = min(predicted, max(current * 2.5, current + 60.0))
    per_minute = slope * 60.0
    if per_minute > 0.5:
        trend = "rising"
    elif per_minute < -0.5:
        trend = "falling"
    else:
        trend = "stable"
    forecast_state.update(pm25=round(predicted, 1), trend=trend,
                          per_minute=round(per_minute, 2), source="trend")


# ------------------------------------------------------------------ advisory engine

def build_advisory():
    """
    Deterministic advisory, and the seam where the TinyML classifier plugs in.

    TODO(tinyml): replace the rules below with the Edge Impulse model's output
    (smoke-event classification over a window of PM2.5/VOC/CO2). The contract is
    the same: return (text, source) where source labels who produced the text.
    """
    recent, _ = pm25_mean(TREND_RECENT_S)
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

    # Measured facts outrank a projection, so CO2 is reported before the forecast.
    if co2 is not None and co2 > 1200:
        return f"CO2 {co2} ppm - ventilate the room", "rules"

    if forecast_state["trend"] == "rising" and forecast_state["pm25"] is not None \
            and forecast_state["pm25"] > max(35.0, recent * 1.5):
        return (f"PM2.5 trending to {forecast_state['pm25']:.0f} ug/m3 within 15 min", "rules")

    if base is not None and base_span > 120 and recent < base * 0.7:
        return "Air improving, PM2.5 falling", "rules"

    if value is not None and value <= 3:
        return "Air quality is good", "rules"

    return "Air quality is moderate, stable", "rules"


def next_alert(live_value, current):
    """
    Decide the alert level from the live index, with hysteresis.

    Raising and clearing at the same number would make the outputs chatter around
    the threshold, so the level latches until the air is clearly better. The hourly
    AQHI+ is deliberately not used here: after an event it stays high for a full
    hour, which would leave the alarm sounding long after the room had cleared.
    """
    if live_value is None:
        return ALERT_NONE
    if live_value >= ALERT_RAISE_VERY_HIGH:
        return ALERT_VERY_HIGH
    if live_value >= ALERT_RAISE_HIGH:
        return ALERT_VERY_HIGH if current == ALERT_VERY_HIGH else ALERT_HIGH
    if current != ALERT_NONE and live_value >= ALERT_CLEAR_BELOW:
        return current  # in the hysteresis band: hold whatever is already raised
    return ALERT_NONE


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
        with lock:
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
    with lock:
        history_copy = list(history)
        logs_copy = list(logs)
    return {
        "device": "AURA",
        "index": {**index_state, "advice_general": general, "advice_at_risk": at_risk},
        "advisory": advisory_state,
        "forecast": forecast_state,
        "alert": pushed["alert"] or ALERT_NONE,
        "alert_simulated": time.time() < demo["until"],
        "alarm_muted": time.time() < mute["until"],
        "mute_seconds_left": max(0, int(mute["until"] - time.time())),
        "dataset": dataset_state,
        "sen55": latest["sen55"],
        "scd41": latest["scd41"],
        "health": health if health["raw"] is not None else None,
        "temperature": scd41.get("temperature", sen55.get("temperature")),
        "humidity": scd41.get("humidity", sen55.get("humidity")),
        "history": history_copy,
        "logs": logs_copy,
    }


def clean_fan():
    Bridge.notify("start_fan_cleaning")
    log("Fan cleaning requested from the dashboard")
    return {"ok": True}


def self_test():
    Bridge.notify("self_test_outputs")
    log("Actuator self-test requested from the dashboard")
    return {"ok": True}


def silence_alarm(minutes: int = ALARM_MUTE_MINUTES):
    """Hush the buzzer without releasing the relay or hiding the on-screen warning."""
    minutes = max(1, min(int(minutes), 120))
    mute["until"] = time.time() + minutes * 60
    log(f"Alarm silenced for {minutes} min (relay and display stay active)")
    return {"ok": True, "minutes": minutes}


def unsilence_alarm():
    mute["until"] = 0.0
    log("Alarm un-silenced")
    return {"ok": True}


def simulate_alert(level: int = ALERT_VERY_HIGH):
    """Raise the danger path for DEMO_ALERT_S seconds, then let it fall back."""
    level = ALERT_VERY_HIGH if int(level) >= ALERT_VERY_HIGH else ALERT_HIGH
    demo["level"] = level
    demo["until"] = time.time() + DEMO_ALERT_S
    log(f"Simulated alert level {level} for {DEMO_ALERT_S} s (demo, not a real measurement)")
    return {"ok": True, "level": level, "seconds": DEMO_ALERT_S}


def set_label(label: str = LABEL_CLEAN):
    """
    Tag the rows being written from now on.

    This is what makes supervised training possible: run a real smoke or cooking
    event with the label set, then set it back to "clean".
    """
    label = label if label in VALID_LABELS else "unknown"
    dataset_state["label"] = label
    log(f"Dataset label set to '{label}'")
    log_dataset_summary()  # show what is banked so far each time the label changes
    return {"ok": True, "label": label}


def relabel(label: str = LABEL_CLEAN, minutes: int = 10):
    """
    Retag rows that are already stored.

    You usually notice an event a few minutes after it started, so the rows
    covering its beginning are already on disk with the previous label. This
    rewrites the last `minutes` of them.
    """
    label = label if label in VALID_LABELS else "unknown"
    minutes = max(1, min(int(minutes), 120))
    cutoff = int(time.time()) - minutes * 60
    try:
        db.update(DB_TABLE, {"label": label}, condition=f"ts >= {cutoff}")
    except Exception as error:
        log(f"Relabel failed: {error}")
        return {"ok": False, "error": str(error)}
    log(f"Relabelled the last {minutes} min as '{label}'")
    log_dataset_summary()
    return {"ok": True, "label": label, "minutes": minutes}


def export_csv(balance: int = 0):
    """
    The training set, as CSV text, for upload to Edge Impulse.

    A night of clean air against a few minutes of events is a 50:1 imbalance, which
    biases a classifier towards always answering "clean". With balance=1 the clean
    rows are thinned evenly across the whole span, keeping at most twice the count
    of the largest event class, so every class gets comparable weight. Event rows
    are never dropped.
    """
    columns = ["ts", "pm1p0", "pm2p5", "pm4p0", "pm10p0", "voc", "nox", "co2",
               "temperature", "humidity", "aqhi", "label"]
    try:
        rows = db.execute_sql(
            f"SELECT {', '.join(columns)} FROM {DB_TABLE} ORDER BY ts DESC LIMIT {EXPORT_MAX_ROWS}"
        ) or []
    except Exception as error:
        return {"ok": False, "error": str(error)}
    rows.reverse()  # chronological order for a time-series import

    # Derived feature: the share of the mass that is submicron. Oil droplets from
    # cooking are almost entirely below 1 um (ratio around 0.95) while biomass
    # smoke agglomerates into a slightly broader distribution (around 0.84). The
    # ratio is scale-free, so unlike the raw concentrations it separates the two
    # classes without depending on how intense the event happened to be.
    for row in rows:
        pm1, pm10 = row.get("pm1p0"), row.get("pm10p0")
        row["pm1_pm10_ratio"] = round(pm1 / pm10, 4) if pm1 and pm10 else None
        # Edge Impulse's CSV wizard expects milliseconds, so offer both forms.
        row["t_ms"] = int(row["ts"]) * 1000
    columns.insert(5, "pm1_pm10_ratio")
    columns.insert(1, "t_ms")

    if int(balance):
        events = [r for r in rows if (r.get("label") or LABEL_CLEAN) != LABEL_CLEAN]
        clean_rows = [r for r in rows if (r.get("label") or LABEL_CLEAN) == LABEL_CLEAN]
        budget = max(len(events) * 2, 60)
        if len(clean_rows) > budget:
            # Even stride rather than a head or tail slice, so the retained clean
            # rows still cover every hour of the day that was recorded.
            stride = len(clean_rows) / budget
            clean_rows = [clean_rows[int(i * stride)] for i in range(budget)]
        rows = sorted(events + clean_rows, key=lambda r: r["ts"])
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join("" if row.get(c) is None else str(row.get(c)) for c in columns))
    log(f"Exported {len(rows)} rows as CSV")
    return {"ok": True, "rows": len(rows), "csv": "\n".join(lines)}


# ------------------------------------------------------------------ main loop

def ui_loop():
    """Recompute everything, push it to the MCU, and log one sample to the store."""
    now = time.time()

    # The MCU finished booting before this process attached to the Bridge, so ask
    # it to re-announce which devices came up and what the status register says.
    if now - pushed["state_asked"] > STATE_REQUEST_PERIOD_S:
        pushed["state_asked"] = now
        Bridge.notify("report_state")

    update_index()
    update_forecast()
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

    # Log the live index too, so the difference with the hourly one is visible in the
    # console: the hourly figure is the official AQHI+ and lags by design.
    live = index_state["live_value"]
    if live != pushed["live"]:
        pushed["live"] = live
        if live is not None:
            log(f"Live index {index_state['live_display']} ({index_state['live_category']}) "
                f"from {index_state['live_pm25']} ug/m3 over 5 min "
                f"- hourly AQHI+ is {index_state['display']}")

    # The danger state drives the OLED takeover and the actuator outputs. It follows
    # the live index, not the hourly one, so it releases as soon as the room clears.
    alert = next_alert(index_state["live_value"], pushed["alert"] or ALERT_NONE)
    if now < demo["until"]:
        alert = max(alert, demo["level"])
    if alert != pushed["alert"]:
        pushed["alert"] = alert
        Bridge.notify("set_alert", alert, ALERT_TEXT.get(alert, ""))

    # Mute state is pushed separately: it gates the buzzer only.
    muted = now < mute["until"]
    if muted != mute["pushed"]:
        mute["pushed"] = muted
        Bridge.notify("set_alarm_mute", 1 if muted else 0)

    if text != pushed["advisory"]:
        pushed["advisory"] = text
        Bridge.notify("set_advisory", text)
        log(f"Advisory: {text}")

    if now - pushed["sampled"] >= SAMPLE_PERIOD_S:
        pushed["sampled"] = now
        store_sample(now)

    if now - pushed["summarised"] >= SUMMARY_PERIOD_S:
        pushed["summarised"] = now
        refresh_dataset_summary()

    if now - pushed["swept"] >= RETENTION_SWEEP_S:
        pushed["swept"] = now
        sweep_retention(now)

    time.sleep(UI_PERIOD_S)


Bridge.provide("log", on_log)
Bridge.provide("sen55_data", on_sen55_data)
Bridge.provide("scd41_data", on_scd41_data)
Bridge.provide("sensor_status", on_sensor_status)

# The dashboard probes both paths, so the mount point of the brick does not matter.
ui.expose_api("GET", "/data", get_data)
ui.expose_api("GET", "/api/data", get_data)
ui.expose_api("GET", "/export", export_csv)
ui.expose_api("POST", "/fan-clean", clean_fan)
ui.expose_api("POST", "/self-test", self_test)
ui.expose_api("POST", "/simulate-alert", simulate_alert)
ui.expose_api("POST", "/silence", silence_alarm)
ui.expose_api("POST", "/unsilence", unsilence_alarm)
ui.expose_api("POST", "/label", set_label)
ui.expose_api("POST", "/relabel", relabel)

init_database()
log("AURA started - dashboard on port 7000, all processing on-device")

App.run(user_loop=ui_loop)
