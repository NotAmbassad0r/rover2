/**
 * ROVER2 basic MegaPi firmware v1.0.0
 *
 * Fixed ports only — no auto-detection scans.
 *   Left drive  → PORT1B
 *   Right drive → PORT2B
 *   Gripper     → PORT4B (DC open/close)
 *   Arm lift    → PORT3B (DC up/down)
 *   Ultrasonic  → PORT_8  (Ultimate 2.0 default; auto-fallback scan PORT_5–8)
 *
 * Commands (newline-terminated JSON):
 *   {"cmd":"drive","l":N,"r":N}
 *   {"cmd":"stop"}
 *   {"cmd":"grip","action":"open"|"close"}
 *   {"cmd":"arm","speed":N}           — continuous −255…255 (0 = stop arm only)
 *   {"cmd":"arm","action":"up"|"down"} — timed pulse (test / nudge)
 *   {"cmd":"sensor_req","sensor":"ultrasonic"}
 *   {"cmd":"ping"}  {"cmd":"version"}
 */

#include <SoftwareSerial.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include "MeMegaPi.h"

// ── Config ───────────────────────────────────────────────────────────────────
#define US_MAX_CM       400   // distanceCm() arg — timeout returns this value (invalid)
#define US_SCAN_MAX_CM  500   // use 500 for port scan (timeout = 500, filtered out)
#define GRIP_RUN_MS     600
#define GRIP_PWM        150
#define ARM_PULSE_MS    800
#define ARM_PWM         150
#define WATCHDOG_MS     5000UL
#define SERIAL_BUF_LEN  160

// ── Hardware objects (only what we use) ──────────────────────────────────────
MeMegaPiDCMotor motorLeft(PORT1B);
MeMegaPiDCMotor motorRight(PORT2B);
MeMegaPiDCMotor motorArm(PORT3B);
MeMegaPiDCMotor motorGripper(PORT4B);
MeUltrasonicSensor us1(PORT_1), us2(PORT_2), us3(PORT_3), us4(PORT_4);
MeUltrasonicSensor us5(PORT_5), us6(PORT_6), us7(PORT_7), us8(PORT_8);
MeUltrasonicSensor* const ultraPorts[8] = {
  &us1, &us2, &us3, &us4, &us5, &us6, &us7, &us8
};
static int8_t ultraIndex = 5;  // default PORT_6 (common Ultimate 2.0 mount)
static bool ultraPortLocked = false;
static uint8_t ultraMissCount = 0;

// ── State ────────────────────────────────────────────────────────────────────
static char serialBuf[SERIAL_BUF_LEN];
static uint8_t serialLen = 0;
static unsigned long lastSerialMs = 0;
static bool watchdogFired = false;

static bool gripActive = false;
static unsigned long gripStopMs = 0;
static int gripPwm = 0;

static bool armPulseActive = false;
static unsigned long armPulseStopMs = 0;
static int armPwm = 0;

// ── JSON helpers (minimal, no String allocations in hot path) ────────────────
static int jsonInt(const char* json, const char* key) {
  char search[24];
  snprintf(search, sizeof(search), "\"%s\":", key);
  const char* p = strstr(json, search);
  if (!p) return 0;
  p += strlen(search);
  while (*p == ' ') p++;
  return atoi(p);
}

static void jsonStr(const char* json, const char* key, char* out, size_t outLen) {
  out[0] = '\0';
  char search[24];
  snprintf(search, sizeof(search), "\"%s\":\"", key);
  const char* start = strstr(json, search);
  if (!start) return;
  start += strlen(search);
  const char* end = strchr(start, '"');
  if (!end || (size_t)(end - start) >= outLen) return;
  strncpy(out, start, end - start);
  out[end - start] = '\0';
}

static const char* jsonCmd(const char* json) {
  static char cmd[16];
  jsonStr(json, "cmd", cmd, sizeof(cmd));
  return cmd;
}

// ── Motors ───────────────────────────────────────────────────────────────────
static void stopDrive() {
  motorLeft.run(0);
  motorRight.run(0);
}

static void runDrive(int left, int right) {
  left = constrain(left, -255, 255);
  right = constrain(right, -255, 255);
  motorLeft.run(-left);
  motorRight.run(-right);
}

// ── Gripper ──────────────────────────────────────────────────────────────────
static void startGrip(const char* action) {
  gripActive = true;
  gripStopMs = millis() + GRIP_RUN_MS;
  if (strcmp(action, "close") == 0) {
    gripPwm = GRIP_PWM;
  } else {
    gripPwm = -GRIP_PWM;
  }
  motorGripper.run(gripPwm);
}

static void updateGrip() {
  if (!gripActive) return;
  if ((long)(millis() - gripStopMs) >= 0) {
    motorGripper.run(0);
    gripActive = false;
    Serial.println(F("{\"evt\":\"gripper\",\"state\":\"idle\"}"));
  }
}

// ── Arm lift (PORT3B) ─────────────────────────────────────────────────────────
static void stopArm() {
  armPulseActive = false;
  armPwm = 0;
  motorArm.run(0);
}

static void runArm(int speed) {
  armPulseActive = false;
  armPwm = constrain(speed, -255, 255);
  motorArm.run(armPwm);
}

static void startArmPulse(const char* action) {
  armPulseActive = true;
  armPulseStopMs = millis() + ARM_PULSE_MS;
  if (strcmp(action, "down") == 0) {
    armPwm = -ARM_PWM;
  } else {
    armPwm = ARM_PWM;
  }
  motorArm.run(armPwm);
}

static void updateArm() {
  if (!armPulseActive) return;
  if ((long)(millis() - armPulseStopMs) >= 0) {
    motorArm.run(0);
    armPulseActive = false;
    armPwm = 0;
    Serial.println(F("{\"evt\":\"arm\",\"state\":\"idle\"}"));
  }
}

static void handleArm(const char* json) {
  if (strstr(json, "\"speed\"") != NULL) {
    int speed = jsonInt(json, "speed");
    if (speed == 0) {
      stopArm();
    } else {
      runArm(speed);
    }
    Serial.println(F("{\"evt\":\"ack\",\"cmd\":\"arm\"}"));
    return;
  }
  char action[8];
  jsonStr(json, "action", action, sizeof(action));
  if (action[0] == '\0' || strcmp(action, "stop") == 0) {
    stopArm();
    Serial.println(F("{\"evt\":\"arm\",\"state\":\"stop\"}"));
    return;
  }
  if (strcmp(action, "up") == 0 || strcmp(action, "down") == 0) {
    startArmPulse(action);
    Serial.print(F("{\"evt\":\"arm\",\"state\":\""));
    Serial.print(action);
    Serial.println(F("\"}"));
    return;
  }
  Serial.println(F("{\"evt\":\"error\",\"msg\":\"arm: use speed or action up/down/stop\"}"));
}

// ── Ultrasonic ───────────────────────────────────────────────────────────────
// MakeBlock distanceCm(MAX): on echo timeout returns exactly MAX — never treat as real.
// Valid echo: 1..399 cm. Returns -1 if no echo / unplugged / not found.
static bool ultraValid(float d) {
  return d >= 1.0f && d < 400.0f;
}

static int readFromIndex(int8_t idx, uint16_t maxCm) {
  float d = ultraPorts[idx]->distanceCm(maxCm);
  delay(30);  // MeUltrasonicSensor needs ~23 ms between triggers
  if (ultraValid(d)) return (int)(d + 0.5f);
  return -1;
}

static int scanUltrasonicPort() {
  for (int8_t i = 0; i < 8; i++) {
    int cm = readFromIndex(i, US_SCAN_MAX_CM);
    if (cm > 0) {
      ultraIndex = i;
      ultraPortLocked = true;
      Serial.print(F("{\"evt\":\"info\",\"msg\":\"ultrasonic on PORT_"));
      Serial.print((int)(i + 1));
      Serial.println(F("\"}"));
      return cm;
    }
  }
  return -1;
}

static int readUltrasonicCm() {
  int cm = readFromIndex(ultraIndex, US_MAX_CM);
  if (cm > 0) {
    ultraMissCount = 0;
    return cm;
  }
  if (!ultraPortLocked) return scanUltrasonicPort();
  if (++ultraMissCount < 20) return -1;
  ultraPortLocked = false;
  ultraMissCount = 0;
  return scanUltrasonicPort();
}

static void sendUltrasonic(int cm) {
  if (cm > 0) {
    Serial.print(F("{\"evt\":\"sensor\",\"ultrasonic_cm\":"));
    Serial.print(cm);
    Serial.println(F("}"));
  } else {
    Serial.println(F("{\"evt\":\"sensor\",\"ultrasonic_cm\":null}"));
  }
}

// ── Command handler ────────────────────────────────────────────────────────────
static void handleLine(const char* json) {
  const char* cmd = jsonCmd(json);
  if (strlen(cmd) == 0) {
    Serial.println(F("{\"evt\":\"error\",\"msg\":\"missing cmd\"}"));
    return;
  }

  if (strcmp(cmd, "drive") == 0) {
    runDrive(jsonInt(json, "l"), jsonInt(json, "r"));
    Serial.println(F("{\"evt\":\"ack\",\"cmd\":\"drive\"}"));
  } else if (strcmp(cmd, "stop") == 0) {
    stopDrive();
    motorGripper.run(0);
    gripActive = false;
    stopArm();
    Serial.println(F("{\"evt\":\"ack\",\"cmd\":\"stop\"}"));
  } else if (strcmp(cmd, "arm") == 0) {
    handleArm(json);
  } else if (strcmp(cmd, "grip") == 0) {
    char action[8];
    jsonStr(json, "action", action, sizeof(action));
    if (action[0] == '\0') strcpy(action, "open");
    startGrip(action);
    Serial.print(F("{\"evt\":\"gripper\",\"state\":\""));
    Serial.print(action);
    Serial.println(F("\"}"));
  } else if (strcmp(cmd, "sensor_req") == 0) {
    char sensor[16];
    jsonStr(json, "sensor", sensor, sizeof(sensor));
    if (strcmp(sensor, "ultrasonic") == 0) {
      sendUltrasonic(readUltrasonicCm());
    } else {
      Serial.println(F("{\"evt\":\"error\",\"msg\":\"unknown sensor\"}"));
    }
  } else if (strcmp(cmd, "ping") == 0) {
    Serial.println(F("{\"evt\":\"pong\"}"));
  } else if (strcmp(cmd, "version") == 0) {
    Serial.println(F("{\"evt\":\"version\",\"fw\":\"rover2-basic-1.0.3\"}"));
  } else {
    Serial.print(F("{\"evt\":\"error\",\"msg\":\"unknown cmd\"}"));
  }
}

// ── Arduino ──────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  stopDrive();
  motorGripper.run(0);
  stopArm();
  serialLen = 0;
  lastSerialMs = millis();
  watchdogFired = false;
  Serial.println(F("{\"evt\":\"ready\",\"fw\":\"rover2-basic-1.0.3\",\"motors\":3}"));
}

void loop() {
  while (Serial.available() > 0) {
    lastSerialMs = millis();
    watchdogFired = false;
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (serialLen > 0) {
        serialBuf[serialLen] = '\0';
        handleLine(serialBuf);
        serialLen = 0;
      }
    } else if (serialLen < SERIAL_BUF_LEN - 1) {
      serialBuf[serialLen++] = c;
    } else {
      serialLen = 0;
      Serial.println(F("{\"evt\":\"error\",\"msg\":\"buffer overflow\"}"));
    }
  }

  if (!watchdogFired && (millis() - lastSerialMs) >= WATCHDOG_MS) {
    stopDrive();
    motorGripper.run(0);
    gripActive = false;
    stopArm();
    watchdogFired = true;
    Serial.println(F("{\"evt\":\"warn\",\"msg\":\"watchdog stop\"}"));
  }

  updateGrip();
  updateArm();
}
