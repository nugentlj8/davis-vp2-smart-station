/*
 * Davis Vantage Pro2  ->  ESP32  ->  MQTT  ->  Home Assistant
 * -------------------------------------------------------------------
 * Reads the console's serial "LOOP" packets, validates each with the
 * Davis CRC-CCITT, parses the values, and publishes to MQTT with Home
 * Assistant auto-discovery.
 *
 * v2 — reliability + headless diagnostics:
 *   - Self-heals: reboots if no good reading for STALE_REBOOT_MS, or if
 *     WiFi/MQTT can't reconnect after many tries.
 *   - Publishes diagnostics to HA (uptime, last error, WiFi RSSI, free
 *     heap, consecutive failures) so you can see problems without a
 *     serial cable.
 *   - Marks itself "offline" (MQTT availability) when stalled, so HA
 *     shows entities as unavailable instead of frozen values.
 *
 * Board: any ESP32 (developed on an ESP32-S3 Dev Module).
 * Library: PubSubClient (Nick O'Leary).
 */

#include <WiFi.h>
#include <PubSubClient.h>

// ======================= USER CONFIG ===============================
const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASS = "YOUR_WIFI_PASSWORD";

const char* MQTT_HOST = "192.168.1.100";  // your Home Assistant IP
const uint16_t MQTT_PORT = 1883;
const char* MQTT_USER = "";               // anonymous broker
const char* MQTT_PASS = "";

const int UART_RX_PIN = 18;   // <- console TXD0
const int UART_TX_PIN = 17;   // -> console RXD0
const uint32_t CONSOLE_BAUD = 19200;

// Reliability tuning
const uint32_t READ_INTERVAL_MS  = 2500;     // poll cadence
const uint32_t STALE_REBOOT_MS   = 180000;   // reboot if no good reading for 3 min
const uint32_t DIAG_INTERVAL_MS  = 30000;    // publish diagnostics every 30 s
const int      MQTT_FAIL_REBOOT  = 20;       // reboot after this many failed MQTT connects
const uint32_t WEEKLY_REBOOT_MS  = 604800000UL; // scheduled reboot every 7 days (insurance)
// ===================================================================

const char* DEVICE_ID   = "davis_vp2";
const char* DEVICE_NAME = "Davis Vantage Pro2";
const char* STATE_TOPIC = "davis_vp2/state";
const char* DIAG_TOPIC  = "davis_vp2/diag";
const char* AVAIL_TOPIC = "davis_vp2/availability";
const char* DISCOVERY_PREFIX = "homeassistant";

HardwareSerial Console(1);
WiFiClient net;
PubSubClient mqtt(net);

uint32_t lastReadMs = 0;
uint32_t lastGoodMs = 0;          // millis of last successful publish
uint32_t lastDiagMs = 0;
uint32_t bootMs = 0;
int      failCount = 0;           // consecutive read failures
int      mqttFails = 0;
char     lastError[48] = "starting";

// ---- Davis CRC-CCITT ----
uint16_t crcTable[256];
void buildCrcTable() {
  for (int i = 0; i < 256; i++) {
    uint16_t crc = i << 8;
    for (int b = 0; b < 8; b++)
      crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : (crc << 1);
    crcTable[i] = crc;
  }
}
uint16_t crcAccum(const uint8_t* data, int len) {
  uint16_t crc = 0;
  for (int i = 0; i < len; i++)
    crc = crcTable[((crc >> 8) ^ data[i]) & 0xFF] ^ (crc << 8);
  return crc;
}

static inline uint16_t u16(const uint8_t* p, int o) { return p[o] | (p[o + 1] << 8); }
static inline int16_t  s16(const uint8_t* p, int o) { return (int16_t)u16(p, o); }

const char* forecastText(uint8_t icon) {
  switch (icon) {
    case 0x08: return "Mostly Clear";
    case 0x06: return "Partially Cloudy";
    case 0x02: return "Mostly Cloudy";
    case 0x03: return "Mostly Cloudy, Rain within 12h";
    case 0x12: return "Mostly Cloudy, Snow within 12h";
    case 0x13: return "Mostly Cloudy, Rain or Snow 12h";
    case 0x07: return "Partly Cloudy, Rain within 12h";
    case 0x16: return "Partly Cloudy, Snow within 12h";
    case 0x17: return "Partly Cloudy, Rain or Snow 12h";
    default:   return "Forecast";
  }
}
const char* barTrendText(int8_t t) {
  switch (t) {
    case -60: return "Falling Rapidly";
    case -20: return "Falling Slowly";
    case 0:   return "Steady";
    case 20:  return "Rising Slowly";
    case 60:  return "Rising Rapidly";
    default:  return "n/a";
  }
}

// ======================= WiFi / MQTT ===============================
void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.mode(WIFI_STA);
  WiFi.setHostname("davis-bridge");   // shows as "davis-bridge" in your router/AP list
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("WiFi connecting");
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    delay(300); Serial.print(".");
  }
  Serial.println(WiFi.status() == WL_CONNECTED ? " connected" : " FAILED");
}

void publishDiscovery(const char* key, const char* name, const char* unit,
                      const char* devClass, const char* stateClass,
                      const char* icon, const char* stateTopic,
                      const char* category) {
  char topic[170];
  snprintf(topic, sizeof(topic), "%s/sensor/%s/%s/config", DISCOVERY_PREFIX, DEVICE_ID, key);
  String p = "{";
  p += "\"name\":\"" + String(name) + "\",";
  p += "\"uniq_id\":\"" + String(DEVICE_ID) + "_" + key + "\",";
  p += "\"stat_t\":\"" + String(stateTopic) + "\",";
  p += "\"avty_t\":\"" + String(AVAIL_TOPIC) + "\",";
  p += "\"exp_aft\":180,";   // sensor goes "unavailable" if no fresh reading in 180s
                             // -> HA draws a gap instead of a flat held-over line
  p += "\"val_tpl\":\"{{ value_json." + String(key) + " }}\",";
  if (unit && unit[0])             p += "\"unit_of_meas\":\"" + String(unit) + "\",";
  if (devClass && devClass[0])     p += "\"dev_cla\":\"" + String(devClass) + "\",";
  if (stateClass && stateClass[0]) p += "\"stat_cla\":\"" + String(stateClass) + "\",";
  if (icon && icon[0])             p += "\"icon\":\"" + String(icon) + "\",";
  if (category && category[0])     p += "\"ent_cat\":\"" + String(category) + "\",";
  p += "\"dev\":{\"ids\":[\"" + String(DEVICE_ID) + "\"],\"name\":\"" + String(DEVICE_NAME) +
       "\",\"mf\":\"Davis Instruments\",\"mdl\":\"Vantage Pro2\"}";
  p += "}";
  mqtt.publish(topic, p.c_str(), true);
}

void publishAllDiscovery() {
  // weather data (state topic)
  publishDiscovery("outside_temp", "Outside Temperature", "\\u00b0F", "temperature", "measurement", "", STATE_TOPIC, "");
  publishDiscovery("inside_temp",  "Inside Temperature",  "\\u00b0F", "temperature", "measurement", "", STATE_TOPIC, "");
  publishDiscovery("outside_hum",  "Outside Humidity",    "%",        "humidity",    "measurement", "", STATE_TOPIC, "");
  publishDiscovery("inside_hum",   "Inside Humidity",     "%",        "humidity",    "measurement", "", STATE_TOPIC, "");
  publishDiscovery("barometer",    "Barometer",           "inHg",     "pressure",    "measurement", "", STATE_TOPIC, "");
  publishDiscovery("bar_trend",    "Barometer Trend",     "",         "",            "",            "mdi:trending-up", STATE_TOPIC, "");
  publishDiscovery("wind_speed",   "Wind Speed",          "mph",      "wind_speed",  "measurement", "", STATE_TOPIC, "");
  publishDiscovery("wind_avg",     "Wind Speed (10-min avg)", "mph",  "wind_speed",  "measurement", "", STATE_TOPIC, "");
  publishDiscovery("wind_dir",     "Wind Direction",      "\\u00b0",  "",            "measurement", "mdi:compass", STATE_TOPIC, "");
  publishDiscovery("rain_rate",    "Rain Rate",           "in/h",     "precipitation_intensity", "measurement", "", STATE_TOPIC, "");
  publishDiscovery("day_rain",     "Rain Today",          "in",       "precipitation", "total_increasing", "", STATE_TOPIC, "");
  publishDiscovery("rain_month",   "Rain This Month",     "in",       "precipitation", "total_increasing", "", STATE_TOPIC, "");
  publishDiscovery("rain_year",    "Rain This Year",      "in",       "precipitation", "total_increasing", "", STATE_TOPIC, "");
  publishDiscovery("storm_rain",   "Storm Rain",          "in",       "precipitation", "measurement", "", STATE_TOPIC, "");
  publishDiscovery("forecast",     "Forecast",            "",         "",            "",            "mdi:weather-partly-cloudy", STATE_TOPIC, "");
  publishDiscovery("forecast_rule", "Forecast Rule",      "",   "",   "",   "mdi:counter",       STATE_TOPIC, "diagnostic");
  publishDiscovery("storm_start",   "Storm Start Date",   "",   "",   "",   "mdi:weather-pouring", STATE_TOPIC, "");
  publishDiscovery("alarms",        "Active Alarms",      "",   "",   "",   "mdi:alarm-light",   STATE_TOPIC, "");
  publishDiscovery("console_batt", "Console Battery",     "V",        "voltage",     "measurement", "", STATE_TOPIC, "");
  publishDiscovery("xmit_batt_low","Transmitter Battery Low", "",     "",            "",            "mdi:battery-alert", STATE_TOPIC, "");
  // diagnostics (diag topic) -> shown under the device's Diagnostic section
  publishDiscovery("uptime_s",   "Bridge Uptime",     "s",   "duration", "measurement", "", DIAG_TOPIC, "diagnostic");
  publishDiscovery("rssi",       "Bridge WiFi Signal","dBm", "signal_strength", "measurement", "", DIAG_TOPIC, "diagnostic");
  publishDiscovery("free_heap",  "Bridge Free Memory","B",   "data_size", "measurement", "", DIAG_TOPIC, "diagnostic");
  publishDiscovery("fails",      "Bridge Read Failures","",  "",         "measurement", "mdi:alert-circle", DIAG_TOPIC, "diagnostic");
  publishDiscovery("last_error", "Bridge Last Status","",    "",         "",            "mdi:information-outline", DIAG_TOPIC, "diagnostic");
  publishDiscovery("bssid",      "Bridge AP (BSSID)", "",   "",         "",            "mdi:access-point",        DIAG_TOPIC, "diagnostic");
  publishDiscovery("ip",         "Bridge IP Address", "",   "",         "",            "mdi:ip-network",          DIAG_TOPIC, "diagnostic");
  publishDiscovery("mac",        "Bridge MAC",        "",   "",         "",            "mdi:lan",                 DIAG_TOPIC, "diagnostic");
  publishDiscovery("channel",    "Bridge WiFi Channel","",  "",         "",            "mdi:wifi",                DIAG_TOPIC, "diagnostic");
}

void ensureMqtt() {
  if (mqtt.connected()) return;
  mqtt.setBufferSize(1024);
  mqtt.setKeepAlive(60);       // tolerate blocking serial reads without dropping
  mqtt.setSocketTimeout(10);
  while (!mqtt.connected()) {
    ensureWifi();
    Serial.print("MQTT connecting...");
    bool ok;
    if (strlen(MQTT_USER) == 0)
      ok = mqtt.connect(DEVICE_ID, NULL, NULL, AVAIL_TOPIC, 0, true, "offline");
    else
      ok = mqtt.connect(DEVICE_ID, MQTT_USER, MQTT_PASS, AVAIL_TOPIC, 0, true, "offline");
    if (ok) {
      Serial.println(" connected");
      mqttFails = 0;
      mqtt.publish(AVAIL_TOPIC, "online", true);
      publishAllDiscovery();
    } else {
      mqttFails++;
      Serial.printf(" failed rc=%d (%d)\n", mqtt.state(), mqttFails);
      if (mqttFails >= MQTT_FAIL_REBOOT) {
        Serial.println("Too many MQTT failures - rebooting.");
        delay(200);
        ESP.restart();
      }
      delay(3000);
    }
  }
}

void publishDiag() {
  String j = "{";
  j += "\"uptime_s\":" + String((millis() - bootMs) / 1000) + ",";
  j += "\"rssi\":" + String(WiFi.RSSI()) + ",";
  j += "\"bssid\":\"" + WiFi.BSSIDstr() + "\",";
  j += "\"ip\":\"" + WiFi.localIP().toString() + "\",";
  j += "\"mac\":\"" + WiFi.macAddress() + "\",";
  j += "\"channel\":" + String(WiFi.channel()) + ",";
  j += "\"free_heap\":" + String(ESP.getFreeHeap()) + ",";
  j += "\"fails\":" + String(failCount) + ",";
  j += "\"last_error\":\"" + String(lastError) + "\"";
  j += "}";
  mqtt.publish(DIAG_TOPIC, j.c_str(), false);
}

// ======================= Console serial ============================
void flushConsole() { while (Console.available()) Console.read(); }

// Fully reset the UART peripheral. Clears a "wedged" serial port (framing/noise
// errors that stop byte delivery) that a soft ESP.restart() does NOT clear --
// this is the software equivalent of unplugging/replugging.
void reinitConsole() {
  Console.end();
  delay(60);
  Console.begin(CONSOLE_BAUD, SERIAL_8N1, UART_RX_PIN, UART_TX_PIN);
  delay(60);
  flushConsole();
  Serial.println("Console UART re-initialized");
}

bool wakeConsole() {
  for (int attempt = 0; attempt < 3; attempt++) {
    flushConsole();
    Console.write('\n');
    uint32_t t0 = millis();
    int got = 0;
    while (millis() - t0 < 1200) {
      if (Console.available()) {
        char c = Console.read();
        if (got == 0 && c == '\n') got = 1;
        else if (got == 1 && c == '\r') return true;
        else got = 0;
      }
    }
  }
  return false;
}

bool readBytes(uint8_t* buf, int n, uint32_t timeoutMs) {
  int idx = 0;
  uint32_t t0 = millis();
  while (idx < n && millis() - t0 < timeoutMs) {
    if (Console.available()) { buf[idx++] = Console.read(); t0 = millis(); }
  }
  return idx == n;
}

bool readOneLoop() {
  flushConsole();
  Console.print("LOOP 1\n");

  uint32_t t0 = millis();
  bool ack = false;
  while (millis() - t0 < 1500) {
    if (Console.available()) { if (Console.read() == 0x06) { ack = true; break; } }
  }
  if (!ack) { strcpy(lastError, "no ACK from console"); return false; }

  uint8_t pkt[99];
  if (!readBytes(pkt, 99, 3000)) { strcpy(lastError, "short/no packet"); return false; }
  if (pkt[0] != 'L' || pkt[1] != 'O' || pkt[2] != 'O') { strcpy(lastError, "bad header"); return false; }
  if (crcAccum(pkt, 99) != 0) { strcpy(lastError, "CRC fail (discarded)"); return false; }

  // Raw 16-bit values kept so we can reject Davis "no data" sentinels (0xFFFF)
  uint16_t baroRaw      = u16(pkt, 7);
  uint16_t rainRateRaw  = u16(pkt, 41);
  uint16_t stormRRaw    = u16(pkt, 46);
  uint16_t dayRainRaw   = u16(pkt, 50);
  uint16_t monthRainRaw = u16(pkt, 52);
  uint16_t yearRainRaw  = u16(pkt, 54);
  int16_t  inTRaw       = s16(pkt, 9);

  float baro   = baroRaw / 1000.0f;
  float inT    = inTRaw / 10.0f;
  int   inH    = pkt[11];
  int16_t outRaw = s16(pkt, 12);
  int   wind   = pkt[14];
  int   windAvg= pkt[15];
  uint16_t wdir = u16(pkt, 16);
  int   outH   = pkt[33];
  float rainRate = rainRateRaw / 100.0f;
  float stormR   = stormRRaw / 100.0f;
  float dayRain  = dayRainRaw / 100.0f;
  float monthRain = monthRainRaw / 100.0f;
  float yearRain  = yearRainRaw / 100.0f;
  int8_t trend   = (int8_t)pkt[3];
  uint8_t xmitBatt = pkt[86];
  uint16_t battRaw = u16(pkt, 87);
  float consoleBatt = battRaw * 300.0f / 512.0f / 100.0f;
  uint8_t fcIcon = pkt[89];
  uint8_t fcRule = pkt[90];
  uint16_t stormRaw = u16(pkt, 48);
  String stormStart;
  if (stormRaw == 0 || stormRaw == 0xFFFF) {
    stormStart = "None";
  } else {
    int sm = (stormRaw >> 12) & 0x0F;
    int sd = (stormRaw >> 7) & 0x1F;
    int sy = (stormRaw & 0x7F) + 2000;
    char b[12]; snprintf(b, sizeof(b), "%04d-%02d-%02d", sy, sm, sd);
    stormStart = String(b);
  }
  String alarms = "";
  auto A = [&](bool c, const char* t){ if (c) { if (alarms.length()) alarms += ", "; alarms += t; } };
  uint8_t a70=pkt[70], a71=pkt[71], a72=pkt[72], a73=pkt[73], a74=pkt[74];
  A(a70&0x01,"Bar Falling"); A(a70&0x02,"Bar Rising"); A(a70&0x04,"Low In Temp");
  A(a70&0x08,"High In Temp"); A(a70&0x10,"Low In Hum"); A(a70&0x20,"High In Hum"); A(a70&0x40,"Time");
  A(a71&0x01,"High Rain Rate"); A(a71&0x02,"Flash Flood"); A(a71&0x04,"24h Rain");
  A(a71&0x08,"Storm Rain"); A(a71&0x10,"Daily ET");
  A(a72&0x01,"Low Temp"); A(a72&0x02,"High Temp"); A(a72&0x04,"High Wind");
  A(a72&0x08,"High 10min Wind"); A(a72&0x10,"Low Dewpoint"); A(a72&0x20,"High Dewpoint");
  A(a72&0x40,"High Heat"); A(a72&0x80,"Low Wind Chill");
  A(a73&0x01,"High THSW"); A(a73&0x02,"High Solar"); A(a73&0x04,"High UV");
  A(a74&0x04,"Low Humidity"); A(a74&0x08,"High Humidity");
  if (alarms.length() == 0) alarms = "None";

  String j = "{";
  if (baroRaw != 0xFFFF && baro > 20.0f && baro < 32.5f)
                        j += "\"barometer\":" + String(baro, 3) + ",";
  j += "\"bar_trend\":\"" + String(barTrendText(trend)) + "\",";
  if (inTRaw != 32767)  j += "\"inside_temp\":" + String(inT, 1) + ",";
  if (inH <= 100)       j += "\"inside_hum\":" + String(inH) + ",";
  if (outRaw != 32767) j += "\"outside_temp\":" + String(outRaw / 10.0f, 1) + ",";
  if (outH <= 100)     j += "\"outside_hum\":" + String(outH) + ",";
  if (wind   != 255) j += "\"wind_speed\":" + String(wind) + ",";   // 0xFF (255) = no data
  if (windAvg != 255) j += "\"wind_avg\":" + String(windAvg) + ",";
  if (wdir >= 1 && wdir <= 360) j += "\"wind_dir\":" + String(wdir) + ",";
  if (rainRateRaw  != 0xFFFF) j += "\"rain_rate\":" + String(rainRate, 2) + ",";
  if (dayRainRaw   != 0xFFFF) j += "\"day_rain\":" + String(dayRain, 2) + ",";
  if (monthRainRaw != 0xFFFF) j += "\"rain_month\":" + String(monthRain, 2) + ",";
  if (yearRainRaw  != 0xFFFF) j += "\"rain_year\":" + String(yearRain, 2) + ",";
  if (stormRRaw    != 0xFFFF) j += "\"storm_rain\":" + String(stormR, 2) + ",";
  j += "\"forecast\":\"" + String(forecastText(fcIcon)) + "\",";
  j += "\"forecast_rule\":" + String(fcRule) + ",";
  j += "\"storm_start\":\"" + stormStart + "\",";
  j += "\"alarms\":\"" + alarms + "\",";
  if (battRaw != 0xFFFF) j += "\"console_batt\":" + String(consoleBatt, 2) + ",";
  j += "\"xmit_batt_low\":\"" + String(xmitBatt ? "ON" : "OFF") + "\"";
  j += "}";

  mqtt.publish(STATE_TOPIC, j.c_str(), false);
  strcpy(lastError, "ok");
  return true;
}

// ======================= Arduino entry points ======================
void setup() {
  Serial.begin(115200);
  delay(300);
  bootMs = millis();
  lastGoodMs = millis();
  buildCrcTable();
  Console.begin(CONSOLE_BAUD, SERIAL_8N1, UART_RX_PIN, UART_TX_PIN);
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  ensureWifi();
  ensureMqtt();
  Serial.println("Davis VP2 bridge v2 ready.");
}

void loop() {
  ensureMqtt();
  mqtt.loop();

  uint32_t now = millis();

  // Poll the console
  if (now - lastReadMs >= READ_INTERVAL_MS) {
    lastReadMs = now;
    bool good = false;
    if (wakeConsole()) {
      good = readOneLoop();
    } else {
      strcpy(lastError, "console did not wake");
    }
    if (good) {
      failCount = 0;
      lastGoodMs = now;
      mqtt.publish(AVAIL_TOPIC, "online", true);
    } else {
      failCount++;
      Serial.println(lastError);
      // Every 5 consecutive failures (~12 s), reset the UART to clear a wedge
      // before falling back to the 3-minute reboot. This usually self-recovers
      // without needing a power cycle.
      if (failCount % 5 == 0) reinitConsole();
    }
  }

  // Diagnostics heartbeat
  if (now - lastDiagMs >= DIAG_INTERVAL_MS) {
    lastDiagMs = now;
    if (mqtt.connected()) publishDiag();
  }

  // Self-heal: if no good reading for too long, go offline and reboot
  if (now - lastGoodMs >= STALE_REBOOT_MS) {
    Serial.println("Data stale too long - rebooting to recover.");
    mqtt.publish(AVAIL_TOPIC, "offline", true);
    delay(300);
    ESP.restart();
  }

  // Weekly scheduled reboot (belt-and-suspenders; clears any long-run cruft)
  if (now - bootMs >= WEEKLY_REBOOT_MS) {
    Serial.println("Weekly scheduled reboot.");
    mqtt.publish(AVAIL_TOPIC, "offline", true);
    delay(300);
    ESP.restart();
  }
}
