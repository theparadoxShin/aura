/*
 * AURA - Offline edge-AI air-quality sentinel
 * MCU side (STM32U585): real-time sensor acquisition, OLED user interface,
 * RGB status LEDs. Measurements and sensor health are pushed to the Linux side
 * over the Router Bridge; the AQHI+ index and advisory text come back the same way.
 *
 * I2C map (Qwiic chain on Wire1, with a fallback to the A4/A5 bus on Wire):
 *   0x69  SEN55  particulate matter, VOC, NOx, temperature, humidity
 *   0x62  SCD41  CO2, temperature, humidity
 *   0x3D  1.3" OLED 128x64 (0x3C when the address jumper is closed)
 */

#include <Arduino.h>
#include <Arduino_RouterBridge.h>
#include <Wire.h>
#include <SensirionI2CSen5x.h>
#include <SensirionI2cScd4x.h>
#include <SparkFun_Qwiic_OLED.h>
// This library declares its fonts in separate headers; they are not pulled in above.
#include <res/qw_fnt_5x7.h>
#include <res/qw_fnt_8x16.h>
#include <stdio.h>

/*
 * The SparkFun OLED library has one debug code path that writes through stdio.
 * Zephyr's minimal C library does not provide fputs, so the link fails without
 * this definition. Nothing in AURA calls it; it only satisfies the linker.
 */
extern "C" int fputs(const char* string, FILE* stream) {
  (void)string;
  (void)stream;
  return 0;
}

// ---------------------------------------------------------------- configuration

static const uint8_t SCD41_ADDR = 0x62;
static const uint8_t OLED_ADDR_DEFAULT = 0x3D;
static const uint8_t OLED_ADDR_ALT = 0x3C;

/*
 * Temperature compensation.
 *
 * Both sensors sit on one breakout whose SEN55 fan and 5 V boost converter warm
 * the enclosure, so the raw readings run several degrees high. The offset is
 * subtracted inside each sensor, which also corrects the relative humidity it
 * derives from that temperature. The SCD41 ships with a 4 C offset already
 * applied, so its value here is that default plus the extra self-heating.
 * Compare against a reference thermometer and adjust these two numbers.
 */
static const float SEN55_TEMPERATURE_OFFSET_C = 3.0f;
static const float SCD41_TEMPERATURE_OFFSET_C = 5.0f;

/*
 * Actuator outputs. These are plain digital outputs, active HIGH, meant for
 * opto-isolated add-on modules: a buzzer or siren, a relay driving a fan or an
 * air purifier, and one spare line. They are driven by the MCU, not by Linux,
 * so the response stays real-time even if the Linux side is busy.
 */
static const uint8_t PIN_ALARM = 2;
static const uint8_t PIN_RELAY = 3;
static const uint8_t PIN_AUX = 4;

/*
 * Stand-down push button (momentary, wired between D5 and GND, internal pull-up).
 * One press pauses every actuator for STAND_DOWN_MS - the escape hatch when the
 * alarm is going off over a burnt toast. A second press re-arms immediately. The
 * OLED keeps showing the danger while paused: the button silences the outputs,
 * never the information.
 */
static const uint8_t PIN_STAND_DOWN = 5;
static const uint32_t STAND_DOWN_MS = 600000;  // 10 minutes
static const uint32_t DEBOUNCE_MS = 40;

// Alert levels, pushed from Python from the AQHI+ category.
static const int ALERT_NONE = 0;
static const int ALERT_HIGH = 1;       // AQHI+ 7-10
static const int ALERT_VERY_HIGH = 2;  // AQHI+ above 10

// SEN55 device status register bits (SEN5x datasheet, section 6.1.4).
static const uint32_t ST_FAN_ERROR = 1UL << 4;    // fan switched on but 0 RPM: blocked or broken
static const uint32_t ST_LASER_ERROR = 1UL << 5;  // laser current out of range
static const uint32_t ST_RHT_ERROR = 1UL << 6;    // internal humidity/temperature sensor
static const uint32_t ST_GAS_ERROR = 1UL << 7;    // VOC/NOx gas sensor
static const uint32_t ST_FAN_CLEANING = 1UL << 19;
static const uint32_t ST_FAN_SPEED_WARNING = 1UL << 21;

// Scheduler periods, in milliseconds.
static const uint32_t SEN55_PERIOD_MS = 2000;
static const uint32_t SCD41_PERIOD_MS = 2000;  // poll data-ready; the sensor itself updates every 5 s
static const uint32_t STATUS_PERIOD_MS = 10000;
static const uint32_t OLED_PERIOD_MS = 220;
static const uint32_t INIT_RETRY_MS = 10000;
static const uint32_t OUTPUT_PERIOD_MS = 50;   // how often the actuator pattern is refreshed
static const uint32_t SELF_TEST_MS = 10000;    // length of the dashboard self-test

// ---------------------------------------------------------------- device state

SensirionI2CSen5x sen5x;
SensirionI2cScd4x scd4x;
Qwiic1in3OLED oled;

static bool sen55Ready = false;
static bool scd41Ready = false;
static bool oledReady = false;

static uint32_t lastSen55Read = 0;
static uint32_t lastScd41Read = 0;
static uint32_t lastStatusRead = 0;
static uint32_t lastOledDraw = 0;
static uint32_t lastInitRetry = 0;

static uint32_t knownStatus = 0xFFFFFFFF;  // impossible value: forces a first report

// Latest readings, mirrored here so the OLED can redraw between acquisitions.
static float lastPm1p0 = NAN;
static float lastPm2p5 = NAN;
static float lastPm10p0 = NAN;
static float lastVoc = NAN;
static float lastNox = NAN;
static float lastTemperature = NAN;
static float lastHumidity = NAN;
static int lastCo2 = -1;

// User-interface state, driven from the Linux side over the Bridge.
static char advisory[96] = "Starting up";
static int aqhiValue = -1;             // -1 while the index is still warming up
static char aqhiLabel[12] = "--";      // short category shown on the OLED
static bool powerFromUsb = true;       // no fuel gauge yet: assume mains/USB
static int batteryPercent = -1;
static bool batteryCharging = false;
static uint32_t marqueeOffset = 0;

// Critical-alert state. When it is raised the OLED drops everything else.
static int alertLevel = ALERT_NONE;
static char alertMessage[48] = "";
static uint32_t lastOutputUpdate = 0;
static uint32_t selfTestUntil = 0;
static bool alarmMuted = false;  // hush button: silences the buzzer only
static uint32_t standDownUntil = 0;  // physical button: pauses ALL actuators
static bool buttonWasDown = false;
static uint32_t buttonChangedAt = 0;

// ---------------------------------------------------------------- logging helpers

static void logLine(const String& message) {
  Serial.println(message);
  Bridge.notify("log", message);
}

static void logError(const String& what, uint16_t error) {
  char detail[64];
  errorToString(error, detail, sizeof(detail));
  logLine(what + ": " + detail);
}

// Formats a float with one decimal without pulling printf's float support in.
static void formatOneDecimal(char* out, size_t size, float value) {
  if (isnan(value)) {
    snprintf(out, size, "--.-");
    return;
  }
  long scaled = lroundf(value * 10.0f);
  snprintf(out, size, "%ld.%ld", scaled / 10, labs(scaled % 10));
}

// ---------------------------------------------------------------- status LEDs

static void updateStatusLed() {
#if defined(LEDR) && defined(LEDG) && defined(LEDB)
  bool red = false;
  bool green = false;
  bool blue = false;
  if (aqhiValue < 0) {
    blue = true;  // warming up
  } else if (aqhiValue <= 3) {
    green = true;  // low risk
  } else if (aqhiValue <= 6) {
    red = true;  // amber: red + green
    green = true;
  } else {
    red = true;  // high or very high risk
  }
  // The on-board RGB LEDs are active-low.
  digitalWrite(LEDR, red ? LOW : HIGH);
  digitalWrite(LEDG, green ? LOW : HIGH);
  digitalWrite(LEDB, blue ? LOW : HIGH);
#endif
}

// ---------------------------------------------------------------- OLED drawing

static void drawCenteredColor(uint8_t y, const char* text, uint8_t advance, uint8_t colour) {
  uint8_t width = (uint8_t)(strlen(text) * advance);
  uint8_t x = (width >= oled.getWidth()) ? 0 : (uint8_t)((oled.getWidth() - width) / 2);
  oled.text(x, y, text, colour);
}

static void drawCentered(uint8_t y, const char* text, uint8_t advance) {
  drawCenteredColor(y, text, advance, COLOR_WHITE);
}

static void drawPowerIcon(uint8_t x, uint8_t y) {
  oled.rectangle(x, y, 14, 8);
  oled.rectangleFill(x + 14, y + 2, 2, 4);
  if (powerFromUsb || batteryPercent < 0) {
    // Mains/USB: a small bolt instead of a charge level.
    oled.line(x + 9, y + 1, x + 5, y + 4);
    oled.line(x + 5, y + 4, x + 8, y + 4);
    oled.line(x + 8, y + 4, x + 4, y + 7);
  } else {
    uint8_t fill = (uint8_t)((batteryPercent * 12) / 100);
    if (fill > 0) {
      oled.rectangleFill(x + 1, y + 1, fill, 6);
    }
    if (batteryCharging) {
      oled.line(x + 9, y + 1, x + 5, y + 4);
      oled.line(x + 5, y + 4, x + 8, y + 4);
      oled.line(x + 8, y + 4, x + 4, y + 7);
    }
  }
}

// Bottom band: the advisory text, scrolled one character at a time when it is
// too long for the 21-column line.
static void drawAdvisory() {
  const uint8_t columns = 21;
  size_t length = strlen(advisory);
  if (length == 0) {
    return;
  }
  if (length <= columns) {
    drawCentered(55, advisory, 6);
    return;
  }
  size_t total = length + 3;  // gap between the end and the wrapped start
  char window[columns + 1];
  for (uint8_t i = 0; i < columns; i++) {
    size_t index = (marqueeOffset + i) % total;
    window[i] = (index < length) ? advisory[index] : ' ';
  }
  window[columns] = '\0';
  oled.text(0, 55, window);
}

/*
 * Full-screen takeover for a dangerous level. Nothing else is shown: no
 * measurements, no advisory band. At the top level the whole screen inverts
 * twice a second, which is the mono-display equivalent of a red flash.
 */
static void drawAlertScreen(uint32_t now) {
  char buffer[20];
  bool inverted = (alertLevel >= ALERT_VERY_HIGH) && (((now / 600) % 2) == 0);
  uint8_t ink = inverted ? COLOR_BLACK : COLOR_WHITE;

  oled.erase();
  if (inverted) {
    oled.rectangleFill(0, 0, oled.getWidth(), oled.getHeight(), COLOR_WHITE);
  }

  // A self-test with no real danger gets its own screen, never the danger banner.
  if (alertLevel == ALERT_NONE) {
    oled.setFont(QW_FONT_8X16);
    drawCentered(12, "SELF TEST", 8);
    oled.setFont(QW_FONT_5X7);
    drawCentered(36, "ALARM / RELAY / AUX", 6);
    drawCentered(48, "PULSING 10 S", 6);
    oled.display();
    return;
  }

  oled.setFont(QW_FONT_8X16);
  drawCenteredColor(1, "! DANGER !", 8, ink);

  if (aqhiValue > 10) {
    snprintf(buffer, sizeof(buffer), "AQHI 10+");
  } else {
    snprintf(buffer, sizeof(buffer), "AQHI %d", aqhiValue);
  }
  drawCenteredColor(21, buffer, 8, ink);

  oled.setFont(QW_FONT_5X7);
  drawCenteredColor(40, aqhiLabel, 6, ink);
  if (now < standDownUntil) {
    // The danger stays on screen; only the outputs are resting.
    char paused[24];
    snprintf(paused, sizeof(paused), "OUTPUTS PAUSED %lu MIN",
             (unsigned long)((standDownUntil - now) / 60000UL + 1));
    drawCenteredColor(54, paused, 6, ink);
  } else if (alertMessage[0] != '\0') {
    drawCenteredColor(54, alertMessage, 6, ink);
  }

  oled.display();
}

static void drawMainScreen() {
  char buffer[28];
  char value[12];
  char second[12];

  oled.erase();

  // Header: brand and power source.
  oled.setFont(QW_FONT_5X7);
  oled.text(0, 0, "AURA");
  drawPowerIcon(oled.getWidth() - 17, 0);
  oled.line(0, 10, oled.getWidth() - 1, 10);

  // Health index, the headline figure.
  oled.setFont(QW_FONT_8X16);
  if (aqhiValue < 0) {
    snprintf(buffer, sizeof(buffer), "AQHI --");
  } else if (aqhiValue > 10) {
    snprintf(buffer, sizeof(buffer), "AQHI 10+");
  } else {
    snprintf(buffer, sizeof(buffer), "AQHI %d", aqhiValue);
  }
  oled.text(0, 14, buffer);
  oled.setFont(QW_FONT_5X7);
  uint8_t labelWidth = (uint8_t)(strlen(aqhiLabel) * 6);
  if (labelWidth < oled.getWidth()) {
    oled.text(oled.getWidth() - labelWidth, 18, aqhiLabel);
  }

  // Measurements.
  formatOneDecimal(value, sizeof(value), lastPm2p5);
  snprintf(buffer, sizeof(buffer), "PM2.5 %s", value);
  oled.text(0, 32, buffer);
  if (lastCo2 >= 0) {
    snprintf(buffer, sizeof(buffer), "CO2 %d", lastCo2);
  } else {
    snprintf(buffer, sizeof(buffer), "CO2 --");
  }
  oled.text(74, 32, buffer);

  formatOneDecimal(value, sizeof(value), lastTemperature);
  formatOneDecimal(second, sizeof(second), lastHumidity);
  if (isnan(lastVoc)) {
    snprintf(buffer, sizeof(buffer), "VOC --  %sC %s%%", value, second);
  } else {
    snprintf(buffer, sizeof(buffer), "VOC %ld  %sC %s%%", lroundf(lastVoc), value, second);
  }
  oled.text(0, 42, buffer);

  oled.line(0, 52, oled.getWidth() - 1, 52);
  drawAdvisory();

  oled.display();
}

static void drawBootStep(const char* step, uint8_t percent) {
  if (!oledReady) {
    return;
  }
  oled.erase();
  oled.setFont(QW_FONT_8X16);
  drawCentered(6, "AURA", 8);
  oled.setFont(QW_FONT_5X7);
  drawCentered(30, step, 6);
  oled.rectangle(13, 44, 102, 8);
  if (percent > 0) {
    oled.rectangleFill(15, 46, (uint8_t)(98 * percent / 100), 4);
  }
  oled.display();
}

// Short, deliberately calm start-up sequence: a sweep, a greeting, the brand.
static void playSplash() {
  if (!oledReady) {
    return;
  }
  for (uint8_t radius = 4; radius <= 44; radius += 5) {
    oled.erase();
    oled.circle(64, 32, radius);
    oled.display();
    delay(25);
  }
  oled.erase();
  oled.setFont(QW_FONT_8X16);
  drawCentered(24, "WELCOME", 8);
  oled.display();
  delay(650);

  oled.erase();
  oled.setFont(QW_FONT_8X16);
  drawCentered(12, "AURA", 8);
  oled.setFont(QW_FONT_5X7);
  drawCentered(34, "AIR QUALITY SENTINEL", 6);
  drawCentered(46, "EDGE AI - OFFLINE", 6);
  oled.display();
  delay(1100);
}

// ---------------------------------------------------------------- actuator outputs

/*
 * Drives the three output lines from the alert level. Called every 50 ms, so the
 * beep patterns are generated here rather than with blocking delays.
 */
static void updateOutputs(uint32_t now) {
  bool alarm = false;
  bool relay = false;
  bool aux = false;

  // Stand-down beats everything except the wiring self-test: the user asked for
  // quiet, they get quiet - the OLED and the dashboard still show the danger.
  if (now < standDownUntil && now >= selfTestUntil) {
    digitalWrite(PIN_ALARM, LOW);
    digitalWrite(PIN_RELAY, LOW);
    digitalWrite(PIN_AUX, LOW);
    return;
  }

  if (now < selfTestUntil) {
    // Self-test: pulse all three lines together at 2 Hz so wiring is easy to check.
    alarm = relay = aux = (((now / 250) % 2) == 0);
  } else if (alertLevel >= ALERT_VERY_HIGH) {
    relay = true;  // purifier or fan on continuously
    aux = true;
    alarm = (((now / 300) % 2) == 0);  // urgent, fast beeping
  } else if (alertLevel == ALERT_HIGH) {
    relay = true;
    alarm = ((now % 5000) < 200);  // one short reminder beep every 5 s
  }

  // The hush button gates the buzzer alone: the purifier keeps running and the
  // screen keeps warning, so silencing never hides the danger.
  if (alarmMuted && now >= selfTestUntil) {
    alarm = false;
  }

  digitalWrite(PIN_ALARM, alarm ? HIGH : LOW);
  digitalWrite(PIN_RELAY, relay ? HIGH : LOW);
  digitalWrite(PIN_AUX, aux ? HIGH : LOW);
}

// Debounced edge detection for the stand-down button; toggles the pause.
static void pollStandDownButton(uint32_t now) {
  bool down = (digitalRead(PIN_STAND_DOWN) == LOW);
  if (down != buttonWasDown && (now - buttonChangedAt) > DEBOUNCE_MS) {
    buttonChangedAt = now;
    buttonWasDown = down;
    if (down) {  // act on press, not release
      if (now < standDownUntil) {
        standDownUntil = 0;
        logLine("Stand-down button: actuators re-armed");
        Bridge.notify("outputs_paused", 0);
      } else {
        standDownUntil = now + STAND_DOWN_MS;
        logLine("Stand-down button: actuators paused for 10 min");
        Bridge.notify("outputs_paused", (int)(STAND_DOWN_MS / 1000));
      }
    }
  }
}

// ---------------------------------------------------------------- initialisation

static bool initOled() {
  if (oled.begin(Wire1, OLED_ADDR_DEFAULT)) {
    logLine("OLED ready on Qwiic/Wire1 at 0x3D");
    return true;
  }
  if (oled.begin(Wire1, OLED_ADDR_ALT)) {
    logLine("OLED ready on Qwiic/Wire1 at 0x3C");
    return true;
  }
  if (oled.begin(Wire, OLED_ADDR_DEFAULT)) {
    logLine("OLED ready on Wire at 0x3D");
    return true;
  }
  logLine("OLED not found on 0x3D or 0x3C, display disabled");
  return false;
}

static bool initSen55On(TwoWire& bus, const char* busName) {
  sen5x.begin(bus);
  uint16_t error = sen5x.deviceReset();
  if (error) {
    logError(String("SEN55 deviceReset (") + busName + ")", error);
    return false;
  }
  delay(200);
  error = sen5x.setTemperatureOffsetSimple(SEN55_TEMPERATURE_OFFSET_C);
  if (error) {
    logError("SEN55 setTemperatureOffsetSimple", error);  // non-fatal
  }
  error = sen5x.startMeasurement();
  if (error) {
    logError(String("SEN55 startMeasurement (") + busName + ")", error);
    return false;
  }
  char offset[12];
  formatOneDecimal(offset, sizeof(offset), SEN55_TEMPERATURE_OFFSET_C);
  logLine(String("SEN55 ready on ") + busName + ", temperature offset -" + offset + " C");
  return true;
}

static void initSen55() {
  sen55Ready = initSen55On(Wire1, "Qwiic/Wire1") || initSen55On(Wire, "Wire");
  if (sen55Ready) {
    knownStatus = 0xFFFFFFFF;  // report the status register once the sensor is up
  }
}

static bool initScd41On(TwoWire& bus, const char* busName) {
  scd4x.begin(bus, SCD41_ADDR);
  scd4x.wakeUp();
  scd4x.stopPeriodicMeasurement();
  delay(500);
  scd4x.reinit();
  delay(30);
  uint16_t error = scd4x.setTemperatureOffset(SCD41_TEMPERATURE_OFFSET_C);
  if (error) {
    logError("SCD41 setTemperatureOffset", error);  // non-fatal
  }
  error = scd4x.startPeriodicMeasurement();
  if (error) {
    logError(String("SCD41 startPeriodicMeasurement (") + busName + ")", error);
    return false;
  }
  char offset[12];
  formatOneDecimal(offset, sizeof(offset), SCD41_TEMPERATURE_OFFSET_C);
  logLine(String("SCD41 ready on ") + busName + ", temperature offset -" + offset + " C");
  return true;
}

static void initScd41() {
  scd41Ready = initScd41On(Wire1, "Qwiic/Wire1") || initScd41On(Wire, "Wire");
}

// ---------------------------------------------------------------- sensor health

static void reportSensorStatus(bool force) {
  if (!sen55Ready) {
    return;
  }
  uint32_t status = 0;
  uint16_t error = sen5x.readDeviceStatus(status);
  if (error) {
    logError("SEN55 readDeviceStatus", error);
    return;
  }
  if (!force && status == knownStatus) {
    return;
  }
  knownStatus = status;

  bool fanError = (status & ST_FAN_ERROR) != 0;
  bool laserError = (status & ST_LASER_ERROR) != 0;
  bool rhtError = (status & ST_RHT_ERROR) != 0;
  bool gasError = (status & ST_GAS_ERROR) != 0;
  bool fanCleaning = (status & ST_FAN_CLEANING) != 0;
  bool fanSpeedWarning = (status & ST_FAN_SPEED_WARNING) != 0;

  // The raw register travels as a single value; Python decodes the same bits.
  Bridge.notify("sensor_status", (int)status);

  char raw[16];
  snprintf(raw, sizeof(raw), "0x%08lX", (unsigned long)status);
  String line = String("SEN55 status ") + raw;
  if (status == 0) {
    line += " - all clear";
  } else {
    if (fanError) line += " FAN-FAILURE";
    if (laserError) line += " LASER-ERROR";
    if (rhtError) line += " RHT-ERROR";
    if (gasError) line += " GAS-ERROR";
    if (fanSpeedWarning) line += " FAN-SPEED-WARNING";
    if (fanCleaning) line += " fan-cleaning";
  }
  logLine(line);
}

// ---------------------------------------------------------------- Bridge handlers

// Called from Python with the advisory text to show in the OLED message band.
void setAdvisory(String text) {
  text.toCharArray(advisory, sizeof(advisory));
  marqueeOffset = 0;
}

// Called from Python with the computed health index and its short label.
void setHealthIndex(int value, String label) {
  aqhiValue = value;
  label.toCharArray(aqhiLabel, sizeof(aqhiLabel));
  updateStatusLed();
}

/*
 * Raises or clears the danger state. Level 2 (AQHI+ above 10) inverts the whole
 * screen and beeps urgently; level 1 (AQHI+ 7-10) shows the same takeover screen
 * steadily and beeps once every five seconds.
 */
void setAlert(int level, String message) {
  bool changed = (level != alertLevel);
  alertLevel = level;
  message.toCharArray(alertMessage, sizeof(alertMessage));
  if (changed) {
    if (level >= ALERT_VERY_HIGH) {
      logLine(String("ALERT level 2 (very high) raised: ") + alertMessage);
    } else if (level == ALERT_HIGH) {
      logLine(String("ALERT level 1 (high) raised: ") + alertMessage);
    } else {
      logLine("Alert cleared, outputs released");
    }
  }
}

// Silences the buzzer while leaving the relay and the warning screen untouched.
void setAlarmMute(int muted) {
  bool wanted = (muted != 0);
  if (wanted != alarmMuted) {
    alarmMuted = wanted;
    logLine(alarmMuted ? "Buzzer muted, relay and display still active"
                       : "Buzzer un-muted");
  }
}

// Pulses all three output lines for ten seconds so wiring can be verified.
void selfTestOutputs() {
  selfTestUntil = millis() + SELF_TEST_MS;
  logLine("Output self-test started: alarm, relay and aux pulsing for 10 s");
}

// Reserved for the battery monitor: percent, on-USB flag, charging flag.
void setPowerState(int percent, int onUsb, int charging) {
  batteryPercent = percent;
  powerFromUsb = (onUsb != 0);
  batteryCharging = (charging != 0);
}

/*
 * The MCU boots and runs setup() before the Linux side has attached to the
 * Bridge, so the start-up log lines would otherwise be lost. Python calls this
 * once it is ready, and periodically afterwards, to get the current picture.
 */
void reportState() {
  logLine(String("Firmware state: OLED ") + (oledReady ? "ready" : "absent") + ", SEN55 " +
          (sen55Ready ? "ready" : "absent") + ", SCD41 " + (scd41Ready ? "ready" : "absent"));
  reportSensorStatus(true);
}

// Manual maintenance hook: spins the fan at full speed for about 10 seconds.
void startFanCleaning() {
  if (!sen55Ready) {
    logLine("Fan cleaning skipped, SEN55 not ready");
    return;
  }
  uint16_t error = sen5x.startFanCleaning();
  if (error) {
    logError("SEN55 startFanCleaning", error);
  } else {
    logLine("SEN55 fan cleaning started");
  }
}

// ---------------------------------------------------------------- setup and loop

void setup() {
  Serial.begin(115200);
  Bridge.begin();

  Bridge.provide("set_advisory", setAdvisory);
  Bridge.provide("set_health_index", setHealthIndex);
  Bridge.provide("set_power_state", setPowerState);
  Bridge.provide("start_fan_cleaning", startFanCleaning);
  Bridge.provide("report_state", reportState);
  Bridge.provide("set_alert", setAlert);
  Bridge.provide("self_test_outputs", selfTestOutputs);
  Bridge.provide("set_alarm_mute", setAlarmMute);

  pinMode(PIN_ALARM, OUTPUT);
  pinMode(PIN_RELAY, OUTPUT);
  pinMode(PIN_AUX, OUTPUT);
  digitalWrite(PIN_ALARM, LOW);
  digitalWrite(PIN_RELAY, LOW);
  digitalWrite(PIN_AUX, LOW);
  pinMode(PIN_STAND_DOWN, INPUT_PULLUP);

#if defined(LEDR) && defined(LEDG) && defined(LEDB)
  pinMode(LEDR, OUTPUT);
  pinMode(LEDG, OUTPUT);
  pinMode(LEDB, OUTPUT);
#endif
  updateStatusLed();

  Wire.begin();
  Wire1.begin();
  delay(2000);  // give the Linux side time to attach to the Bridge

  oledReady = initOled();
  playSplash();

  drawBootStep("STARTING SENSORS", 15);
  initSen55();
  drawBootStep(sen55Ready ? "SEN55 OK" : "SEN55 NOT FOUND", 55);
  delay(500);

  initScd41();
  drawBootStep(scd41Ready ? "SCD41 OK" : "SCD41 NOT FOUND", 85);
  delay(500);

  reportSensorStatus(true);
  drawBootStep("READY", 100);
  delay(500);

  logLine("AURA firmware ready");
}

void loop() {
  uint32_t now = millis();

  // Retry any sensor that failed to come up, so a late Qwiic connection recovers.
  if ((!sen55Ready || !scd41Ready) && (now - lastInitRetry > INIT_RETRY_MS)) {
    lastInitRetry = now;
    if (!sen55Ready) initSen55();
    if (!scd41Ready) initScd41();
  }

  if (sen55Ready && (now - lastSen55Read >= SEN55_PERIOD_MS)) {
    lastSen55Read = now;
    float pm1p0, pm2p5, pm4p0, pm10p0, humidity, temperature, voc, nox;
    uint16_t error = sen5x.readMeasuredValues(pm1p0, pm2p5, pm4p0, pm10p0, humidity, temperature,
                                              voc, nox);
    if (error) {
      logError("SEN55 readMeasuredValues", error);
    } else {
      lastPm1p0 = pm1p0;
      lastPm2p5 = pm2p5;
      lastPm10p0 = pm10p0;
      lastVoc = voc;
      lastNox = nox;
      if (lastCo2 < 0) {  // the SCD41 is the preferred source once it reports
        lastTemperature = temperature;
        lastHumidity = humidity;
      }
      Bridge.notify("sen55_data", pm1p0, pm2p5, pm4p0, pm10p0, humidity, temperature, voc, nox);
    }
  }

  if (scd41Ready && (now - lastScd41Read >= SCD41_PERIOD_MS)) {
    lastScd41Read = now;
    bool dataReady = false;
    uint16_t error = scd4x.getDataReadyStatus(dataReady);
    if (error) {
      logError("SCD41 getDataReadyStatus", error);
    } else if (dataReady) {
      uint16_t co2 = 0;
      float temperature = 0.0f;
      float humidity = 0.0f;
      error = scd4x.readMeasurement(co2, temperature, humidity);
      if (error) {
        logError("SCD41 readMeasurement", error);
      } else if (co2 != 0) {
        lastCo2 = (int)co2;
        lastTemperature = temperature;
        lastHumidity = humidity;
        Bridge.notify("scd41_data", (int)co2, temperature, humidity);
      }
    }
  }

  if (now - lastStatusRead >= STATUS_PERIOD_MS) {
    lastStatusRead = now;
    reportSensorStatus(false);
  }

  pollStandDownButton(now);

  if (now - lastOutputUpdate >= OUTPUT_PERIOD_MS) {
    lastOutputUpdate = now;
    updateOutputs(now);
  }

  if (oledReady && (now - lastOledDraw >= OLED_PERIOD_MS)) {
    lastOledDraw = now;
    marqueeOffset++;
    // A danger level takes the whole screen; nothing else is drawn.
    if (alertLevel > ALERT_NONE || now < selfTestUntil) {
      drawAlertScreen(now);
    } else {
      drawMainScreen();
    }
  }
}
