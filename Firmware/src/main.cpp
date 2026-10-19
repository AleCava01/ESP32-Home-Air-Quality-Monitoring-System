#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>   // v7
#include <Adafruit_AHTX0.h>
#include <Adafruit_BMP280.h>
#include <sps30.h>

// ---------- CONFIGURAZIONE ----------
#include "secrets.h"
//const char* WIFI_SSID  = "...";
//const char* WIFI_PASS  = "...";
//const char* SERVER_URL = "http://192.168.1.XXX:8081/api/data";
const char* DEVICE_ID  = "Airometer_ESP32";

#define I2C_SDA 10
#define I2C_SCL 9
const int pinLed = 5;

// ---------- STATO ----------
Adafruit_AHTX0 aht;
Adafruit_BMP280 bmp;

struct Reading {
  float temp_aht = NAN, hum = NAN, temp_bmp = NAN, press = NAN;
  float pm1 = NAN, pm25 = NAN, pm10 = NAN;
} cur;

// Configurazione LED (aggiornata dal server ad ogni invio)
String ledMode   = "tx";        // off | on | tx | threshold | blink
String ledMetric = "pm25";      // temp_aht, hum, temp_bmp, press, pm1, pm25, pm10
String ledCmp    = "gt";        // gt (>) | lt (<)
float  ledThreshold = 25.0;
unsigned long ledBlinkMs = 500;
unsigned long sendIntervalMs = 60000;

unsigned long lastSend = 0, lastWifiTry = 0, txUntil = 0;

// ---------- FUNZIONI ----------
float metricValue(const String& m) {
  if (m == "temp_aht") return cur.temp_aht;
  if (m == "hum")      return cur.hum;
  if (m == "temp_bmp") return cur.temp_bmp;
  if (m == "press")    return cur.press;
  if (m == "pm1")      return cur.pm1;
  if (m == "pm25")     return cur.pm25;
  if (m == "pm10")     return cur.pm10;
  return NAN;
}

void updateLed() {
  static unsigned long lastToggle = 0;
  static bool blinkState = false;

  // WiFi assente: lampeggio rapido, prioritario su qualsiasi modalità
  if (WiFi.status() != WL_CONNECTED) {
    if (millis() - lastToggle >= 200) {       // 200 ms = lampeggio veloce
      lastToggle = millis();
      blinkState = !blinkState;
      digitalWrite(pinLed, blinkState);
    }
    return;
  }

  if (ledMode == "off") {
    digitalWrite(pinLed, LOW);
  } else if (ledMode == "on") {
    digitalWrite(pinLed, HIGH);
  } else if (ledMode == "tx") {
    digitalWrite(pinLed, (long)(txUntil - millis()) > 0 ? HIGH : LOW);
  } else if (ledMode == "threshold") {
    float v = metricValue(ledMetric);
    bool on = !isnan(v) && (ledCmp == "gt" ? v > ledThreshold : v < ledThreshold);
    digitalWrite(pinLed, on ? HIGH : LOW);
  } else if (ledMode == "blink") {
    if (millis() - lastToggle >= ledBlinkMs / 2) {
      lastToggle = millis();
      blinkState = !blinkState;
      digitalWrite(pinLed, blinkState);
    }
  }
}

void connectWifi(unsigned long timeoutMs) {
  WiFi.persistent(false); // Evita di usare configurazioni corrotte salvate in Flash
  WiFi.disconnect(true);
  delay(1000);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.setSleep(false);
  Serial.printf("[WiFi] Connessione a: %s\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeoutMs) {
    delay(250);
    Serial.print(".");
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("\n[WiFi] Connesso! Indirizzo IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\n[WiFi] ERRORE: Impossibile connettersi.");
    Serial.print("Stato WiFi attuale: ");
    Serial.println(WiFi.status()); // Stampa il codice numerico dell'errore
  }
}

// Controllo non bloccante: se il WiFi è caduto, riprova ogni 10 s
void checkWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  if (millis() - lastWifiTry < 10000) return;   // riprova ogni 10 s

  lastWifiTry = millis();
  Serial.println("[WiFi] Connessione persa, riconnessione in corso...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASS);
}

void readSensors() {
  // AHT20
  sensors_event_t humidity, temp_aht;
  aht.getEvent(&humidity, &temp_aht);
  cur.temp_aht = temp_aht.temperature;
  cur.hum = humidity.relative_humidity;

  // BMP280
  cur.temp_bmp = bmp.readTemperature();
  cur.press = bmp.readPressure() / 100.0F;

  // SPS30 (NAN se non pronto/errore)
  cur.pm1 = cur.pm25 = cur.pm10 = NAN;
  uint16_t ready = 0;
  if (sps30_read_data_ready(&ready) >= 0 && ready) {
    struct sps30_measurement m;
    if (sps30_read_measurement(&m) >= 0) {
      cur.pm1 = m.mc_1p0;
      cur.pm25 = m.mc_2p5;
      cur.pm10 = m.mc_10p0;
    }
  }

  Serial.printf("AHT20 %.1fC %.1f%% | BMP280 %.1fC %.1fhPa | PM1 %.1f PM2.5 %.1f PM10 %.1f\n",
                cur.temp_aht, cur.hum, cur.temp_bmp, cur.press, cur.pm1, cur.pm25, cur.pm10);
}

void setF(JsonDocument& d, const char* k, float v) {
  if (isnan(v)) d[k] = nullptr; else d[k] = v;
}

void applyServerConfig(const String& payload) {
  JsonDocument doc;
  if (deserializeJson(doc, payload)) return;
  JsonObject led = doc["led"];
  if (!led.isNull()) {
    ledMode      = led["mode"]      | ledMode;
    ledMetric    = led["metric"]    | ledMetric;
    ledCmp       = led["cmp"]       | ledCmp;
    ledThreshold = led["threshold"] | ledThreshold;
    ledBlinkMs   = led["blink_ms"]  | (unsigned long)ledBlinkMs;
  }
  if (doc["interval"].is<int>()) sendIntervalMs = doc["interval"].as<unsigned long>() * 1000UL;
}

void sendData() {
  if (WiFi.status() != WL_CONNECTED) return;

  JsonDocument doc;
  doc["device"] = DEVICE_ID;
  setF(doc, "temp_aht", cur.temp_aht);
  setF(doc, "hum", cur.hum);
  setF(doc, "temp_bmp", cur.temp_bmp);
  setF(doc, "press", cur.press);
  setF(doc, "pm1", cur.pm1);
  setF(doc, "pm25", cur.pm25);
  setF(doc, "pm10", cur.pm10);
  String body;
  serializeJson(doc, body);

  if (ledMode == "tx") digitalWrite(pinLed, HIGH);   // LED acceso durante l'invio

  WiFiClient client;
  HTTPClient http;
  http.setTimeout(8000);
  if (http.begin(client, SERVER_URL)) {
    http.addHeader("Content-Type", "application/json");
    int code = http.POST(body);
    if (code == 200) {
      applyServerConfig(http.getString());
    } else {
      Serial.printf("[HTTP] Errore: %d\n", code);
    }
    http.end();
  }
  txUntil = millis() + 200;   // mantiene il LED acceso 200 ms dopo l'invio
}

// ---------- SETUP / LOOP ----------
void setup() {
  pinMode(pinLed, OUTPUT);
  digitalWrite(pinLed, HIGH);
  Serial.begin(115200);
  delay(2000);

  Serial.println("\n--- Inizializzazione Sensori I2C ---");
  Wire.begin(I2C_SDA, I2C_SCL);

  if (!aht.begin()) Serial.println("[AHT20] ERRORE: Sensore non trovato!");
  else Serial.println("[AHT20] OK");

  if (!bmp.begin(0x77) && !bmp.begin(0x76)) {
    Serial.println("[BMP280] ERRORE: Sensore non trovato!");
  } else {
    Serial.println("[BMP280] OK");
    bmp.setSampling(Adafruit_BMP280::MODE_NORMAL,
                    Adafruit_BMP280::SAMPLING_X2,
                    Adafruit_BMP280::SAMPLING_X16,
                    Adafruit_BMP280::FILTER_X16,
                    Adafruit_BMP280::STANDBY_MS_500);
  }

  sensirion_i2c_init();
  if (sps30_probe() != 0) Serial.println("[SPS30] ERRORE: Sensore non rilevato!");
  else Serial.println("[SPS30] OK");
  if (sps30_start_measurement() < 0) Serial.println("[SPS30] ERRORE avvio misurazione!");

  Serial.print("[WiFi] Connessione");
  connectWifi(30000);
  Serial.println("-----------------------------------\n");
  lastSend = millis() - sendIntervalMs;   // primo invio immediato
}

void loop() {
  checkWifi();   // riconnessione automatica se il WiFi è caduto

  if (millis() - lastSend >= sendIntervalMs) {
    lastSend = millis();
    readSensors();
    sendData();
  }

  updateLed();   // lampeggia veloce se WiFi assente
  delay(10);
}
