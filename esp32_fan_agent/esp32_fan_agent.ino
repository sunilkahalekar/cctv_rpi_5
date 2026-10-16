/*
  ESP32 exhaust-fan agent  —  same network API as pi_fan_agent.py
  ------------------------------------------------------------------
  The camera app talks to this board exactly as it talks to the Raspberry Pi agent.
  config.yaml  fan: controller: dual  +  fan: esp32: url: http://<this board's IP>:8095
  makes the Pi and this ESP32 alternatives (dashboard Fan page: Raspberry Pi | ESP32).

    GET  /status                         -> JSON state (also the app's heartbeat)
    POST /fan   {"state":"on"|"off","reason":"..."}
    Header on every request:  Authorization: Bearer <TOKEN>

  Hardware:  ESP32 DevKit  GPIO 26  ->  SSR-25DA input "3 (+)"
             ESP32 GND               ->  SSR-25DA input "4 (-)"
             10 kΩ from GPIO 26 to GND (keeps the SSR OFF while the ESP32 boots)
  SSR output (terminals 1, 2) in series with the fan's live wire — by an electrician.

  Safety behaviour (lab decision, same as the Pi agent):
    - relay OFF at power-up / reset
    - no contact from the PC for WATCHDOG_MS  ->  COMM_LOSS_ACTION (off)
    - Wi-Fi lost for 60 s                      ->  fan OFF, board restarts and reconnects

  Arduino IDE: Boards Manager "esp32" by Espressif (2.x or 3.x), board "ESP32 Dev Module".
  No extra libraries needed.
*/
#include <WiFi.h>
#include <WebServer.h>

// ===================== settings: change these =====================
// Wi-Fi password and token live in secrets.h (same folder, never uploaded to GitHub).
// First time: copy secrets.h.example to secrets.h and fill it in.
#include "secrets.h"          // defines WIFI_SSID, WIFI_PASSWORD, TOKEN
const char* ALLOW_IP      = "";      // "" = any computer with the token (needed behind a Wi-Fi router / port
                                       // forwarding, where the board sees the router, not the PC)

// Fixed address without IT: the board first asks the Wi-Fi router for an address (DHCP), learns
// the router's network (gateway, mask, DNS), then keeps the SAME network but always uses the host
// number FIXED_HOST, e.g. 192.168.0.200. Choose a number outside the router's DHCP pool (TP-Link
// and most routers hand out .100-.199, so .200 is safe). 0 = keep the DHCP address.
const uint8_t FIXED_HOST = 200;

const int  RELAY_PIN        = 26;        // safe pin: does not toggle during boot
const int  STATUS_LED       = 2;         // blue LED on most ESP32 DevKit boards (-1 = none):
                                         // fast blink = joining Wi-Fi, ON = Wi-Fi OK, short flash = PC talked
const bool ACTIVE_HIGH      = true;      // SSR-25DA: HIGH = ON
const unsigned long WATCHDOG_MS = 120000;  // 0 = no watchdog
const char* COMM_LOSS_ACTION = "off";      // "off", "on" or "hold"
const uint16_t PORT = 8095;
// ==================================================================

WebServer server(PORT);
bool requested = false;
bool commLost = false;
unsigned long lastContact = 0;
unsigned long sinceMs = 0;
unsigned long wifiLostAt = 0;
String lastReason = "agent started";

void led(bool on) {
  if (STATUS_LED >= 0) digitalWrite(STATUS_LED, on ? HIGH : LOW);
}

void setRelay(bool on, const String& reason) {
  digitalWrite(RELAY_PIN, (on == ACTIVE_HIGH) ? HIGH : LOW);
  if (on != requested || reason != lastReason) sinceMs = millis();
  requested = on;
  lastReason = reason;
  Serial.printf("[fan] %s - %s\n", on ? "ON" : "OFF", reason.c_str());
}

bool signalOn() {
  // pin level read back from the output register (proves the control signal, not that the fan turns)
  bool high = digitalRead(RELAY_PIN) == HIGH;
  return high == ACTIVE_HIGH;
}

String jsonEscape(const String& s) {
  String out;
  for (size_t i = 0; i < s.length() && i < 200; i++) {
    char c = s[i];
    if (c == '"' || c == '\\') out += '\'';
    else if ((uint8_t)c < 0x20) out += ' ';
    else out += c;
  }
  return out;
}

void sendJson(int code, const String& body) {
  server.send(code, "application/json", body);
}

void sendStatus() {
  String s = "{";
  s += "\"requested\":" + String(requested ? "true" : "false");
  s += ",\"signal\":" + String(signalOn() ? "true" : "false");
  s += ",\"running\":null,\"ok\":true";
  s += ",\"pin\":" + String(RELAY_PIN);
  s += ",\"active_high\":" + String(ACTIVE_HIGH ? "true" : "false");
  s += ",\"watchdog_seconds\":" + String(WATCHDOG_MS / 1000);
  s += ",\"on_comm_loss\":\"" + String(COMM_LOSS_ACTION) + "\"";
  s += ",\"last_reason\":\"" + jsonEscape(lastReason) + "\"";
  s += ",\"since\":\"uptime " + String(sinceMs / 1000) + " s\"";
  s += ",\"comm_lost\":" + String(commLost ? "true" : "false");
  s += ",\"controller\":\"ESP32\"}";
  sendJson(200, s);
}

bool authorised() {
  if (strlen(ALLOW_IP) && server.client().remoteIP().toString() != ALLOW_IP) {
    sendJson(403, "{\"error\":\"this computer is not allowed\"}");
    return false;
  }
  String expected = String("Bearer ") + TOKEN;
  if (server.header("Authorization") != expected) {
    delay(500);                                   // slows down guessing
    sendJson(401, "{\"error\":\"wrong or missing token\"}");
    return false;
  }
  lastContact = millis();
  led(false); delay(30); led(true);             // short flash: the PC talked to the board
  if (commLost) Serial.println("[fan] PC is back");
  commLost = false;
  return true;
}

// tiny JSON reader for {"state":"on","reason":"..."}
String jsonField(const String& body, const char* key) {
  String k = String("\"") + key + "\"";
  int i = body.indexOf(k);
  if (i < 0) return "";
  i = body.indexOf(':', i + k.length());
  if (i < 0) return "";
  i = body.indexOf('"', i);
  if (i < 0) return "";
  int j = body.indexOf('"', i + 1);
  if (j < 0) return "";
  return body.substring(i + 1, j);
}

void handleStatus() {
  if (authorised()) sendStatus();
}

void handleFan() {
  if (!authorised()) return;
  String body = server.arg("plain");
  String state = jsonField(body, "state");
  if (state != "on" && state != "off") {
    sendJson(400, "{\"error\":\"state must be 'on' or 'off'\"}");
    return;
  }
  String reason = jsonField(body, "reason");
  setRelay(state == "on", reason + " [from " + server.client().remoteIP().toString() + "]");
  sendStatus();
}

bool waitConnected(unsigned long ms) {
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < ms) {
    for (int i = 0; i < 5; i++) { led(i % 2 == 0); delay(100); }   // fast blink while joining
    Serial.print(".");
  }
  led(WiFi.status() == WL_CONNECTED);
  Serial.println();
  return WiFi.status() == WL_CONNECTED;
}

void connectWifi() {
  WiFi.setHostname("fan-esp32");     // before WiFi.mode, as the esp32 core requires
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  Serial.printf("[wifi] MAC %s\n", WiFi.macAddress().c_str());   // valid only after WiFi.mode
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("[wifi] connecting (DHCP)");
  if (!waitConnected(30000)) {
    Serial.println("[wifi] not connected yet, will keep trying (check name / password, 2.4 GHz only)");
    return;
  }
  IPAddress ip = WiFi.localIP(), gw = WiFi.gatewayIP(), mask = WiFi.subnetMask(), dns = WiFi.dnsIP();
  Serial.printf("[wifi] router gave IP %s, gateway %s, mask %s\n", ip.toString().c_str(),
                gw.toString().c_str(), mask.toString().c_str());
  if (FIXED_HOST) {
    IPAddress fixed(ip[0], ip[1], ip[2], FIXED_HOST);       // same network, fixed host number (/24 networks)
    if (fixed != ip) {
      WiFi.disconnect(false, false);
      delay(200);
      WiFi.config(fixed, gw, mask, dns);
      WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
      Serial.printf("[wifi] switching to the fixed address %s", fixed.toString().c_str());
      if (!waitConnected(20000)) {
        Serial.println("[wifi] fixed address failed, back to DHCP");
        WiFi.disconnect(false, false);
        WiFi.config(INADDR_NONE, INADDR_NONE, INADDR_NONE);
        WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
        waitConnected(20000);
      }
    }
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[wifi] connected, IP %s  ->  reach it at http://%s:%u/status\n",
                  WiFi.localIP().toString().c_str(), WiFi.localIP().toString().c_str(), PORT);
  }
}

void setup() {
  // relay OFF before the pin becomes an output: no blip at boot
  digitalWrite(RELAY_PIN, ACTIVE_HIGH ? LOW : HIGH);
  pinMode(RELAY_PIN, OUTPUT);
  if (STATUS_LED >= 0) { pinMode(STATUS_LED, OUTPUT); led(false); }
  Serial.begin(115200);
  delay(200);
  Serial.println("\nESP32 fan agent");
  setRelay(false, "agent started");
  connectWifi();
  const char* headers[] = {"Authorization"};
  server.collectHeaders(headers, 1);
  server.on("/status", HTTP_GET, handleStatus);
  server.on("/fan", HTTP_POST, handleFan);
  server.onNotFound([]() { sendJson(404, "{\"error\":\"not found\"}"); });
  server.begin();
  lastContact = millis();
  Serial.printf("[http] listening on port %u\n", PORT);
}

void loop() {
  server.handleClient();

  // watchdog: the PC asks for the status every few seconds
  if (WATCHDOG_MS && !commLost && millis() - lastContact > WATCHDOG_MS) {
    commLost = true;
    String msg = "no contact from the lab PC for " + String(WATCHDOG_MS / 1000) + " s";
    if (strcmp(COMM_LOSS_ACTION, "hold") == 0) Serial.println("[fan] " + msg + ": holding");
    else setRelay(strcmp(COMM_LOSS_ACTION, "on") == 0, msg + " (comm-loss action: " + COMM_LOSS_ACTION + ")");
  }

  // Wi-Fi loss: fan OFF after 60 s, then restart to reconnect cleanly
  if (WiFi.status() != WL_CONNECTED) {
    led((millis() / 100) % 2);                  // fast blink: Wi-Fi lost
    if (!wifiLostAt) wifiLostAt = millis();
    if (millis() - wifiLostAt > 60000) {
      setRelay(false, "Wi-Fi lost for 60 s");
      delay(100);
      ESP.restart();
    }
  } else {
    if (wifiLostAt) led(true);
    wifiLostAt = 0;
  }
  delay(2);
}
