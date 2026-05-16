/**
 * sensor_reader.h — Ultrasonic, line follower, and gyro with auto-detect.
 *
 * Ultrasonic and line follower: detect on first read, cache port permanently.
 * Gyro: I2C ping at startup — gyroDetected flag gates all gyro reads.
 *
 * MeMegaPi.h and Wire.h must be included in rover_firmware.ino before this header.
 * serial_protocol.h must also be included before this header.
 */
#ifndef SENSOR_READER_H
#define SENSOR_READER_H

// ── Ultrasonic sensor objects (one per RJ25 port) ────────────────────────────

MeUltrasonicSensor us1(PORT_1), us2(PORT_2), us3(PORT_3), us4(PORT_4);
MeUltrasonicSensor us5(PORT_5), us6(PORT_6), us7(PORT_7), us8(PORT_8);
MeUltrasonicSensor* ultraSensors[8] = {
  &us1, &us2, &us3, &us4, &us5, &us6, &us7, &us8
};

// -1 = not yet set; 0–7 = port index (PORT_3=0 … PORT_8=5).
// Set remotely via {"cmd":"set_port","sensor":"ultrasonic","port":N} on every
// Pi connect before any sensor_req. Falls back to auto-scan if never set.
int  detectedUltrasonicPort = -1;

// ── Line follower objects (one per RJ25 port) ────────────────────────────────

MeLineFollower lf1(PORT_1), lf2(PORT_2), lf3(PORT_3), lf4(PORT_4);
MeLineFollower lf5(PORT_5), lf6(PORT_6), lf7(PORT_7), lf8(PORT_8);
MeLineFollower* lineFollowers[8] = {
  &lf1, &lf2, &lf3, &lf4, &lf5, &lf6, &lf7, &lf8
};

int  detectedLinePort = -1;
bool lineWarnedOnce   = false;

// ── Gyro ─────────────────────────────────────────────────────────────────────

MeGyro gyro;
bool   gyroDetected = false;

// ── Gyro detection ────────────────────────────────────────────────────────────

static bool i2cPing(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

bool detectGyro() {
  uint8_t addr = 0;
  if      (i2cPing(0x68)) addr = 0x68;
  else if (i2cPing(0x69)) addr = 0x69;

  if (addr == 0) {
    sendWarn(F("gyro not detected — check I2C connection"));
    return false;
  }

  gyro.begin();

  char buf[56];
  snprintf(buf, sizeof(buf), "{\"evt\":\"info\",\"msg\":\"gyro detected at 0x%02X\"}", addr);
  Serial.println(buf);
  return true;
}

// ── Ultrasonic scan ───────────────────────────────────────────────────────────

// Scan all 8 ports for a sensor giving a valid in-range reading (>= 1 cm).
// Use >= 1.0 (not > 0) to avoid noise triggering a false positive at 0.
static int scanUltrasonicPorts() {
  for (int i = 0; i < 8; i++) {
    float d = ultraSensors[i]->distanceCm(500);
    if (d >= 1.0 && d < 380.0) return i;
  }
  return -1;
}

// Returns:
//   >= 1  : valid distance in cm
//     -1  : no valid echo (echo timeout, sensor unplugged, or no sensor found)
//
// distanceCm(500) returns 0 on echo timeout and values >= 400 when the sensor
// is disconnected. Both are treated as no valid echo.
int readUltrasonic() {
  if (detectedUltrasonicPort == -1) {
    detectedUltrasonicPort = scanUltrasonicPorts();
    if (detectedUltrasonicPort == -1) return -1;
    char buf[64];
    snprintf(buf, sizeof(buf),
      "{\"evt\":\"info\",\"msg\":\"ultrasonic detected on PORT_%d\"}",
      detectedUltrasonicPort + 1);
    Serial.println(buf);
  }

  float d = ultraSensors[detectedUltrasonicPort]->distanceCm(500);

  if (d >= 1.0 && d < 400.0) return (int)d;

  // d == 0: echo timeout (nothing in range).
  // d >= 400: sensor unplugged or invalid reading.
  // Both cases: no valid echo.
  return -1;
}

// ── Line follower scan ────────────────────────────────────────────────────────

static int scanLineFollowerPorts() {
  int fallback = -1;
  for (int i = 0; i < 8; i++) {
    uint8_t s = lineFollowers[i]->readSensors();
    if (s == 1 || s == 2) return i;
    if (s == 0 || s == 3) {
      if (fallback == -1) fallback = i;
    }
  }
  return fallback;
}

int readLineSensor() {
  if (detectedLinePort == -1) {
    detectedLinePort = scanLineFollowerPorts();
    if (detectedLinePort == -1) {
      if (!lineWarnedOnce) {
        sendWarn(F("line follower not detected on any port"));
        lineWarnedOnce = true;
      }
      return -1;
    }
    char buf[64];
    snprintf(buf, sizeof(buf),
      "{\"evt\":\"info\",\"msg\":\"line follower detected on PORT_%d\"}",
      detectedLinePort + 1);
    Serial.println(buf);
  }

  uint8_t s = lineFollowers[detectedLinePort]->readSensors();
  if (s > 3) {
    detectedLinePort = -1;
    lineWarnedOnce   = false;
    return -1;
  }
  return (int)s;
}

// ── Gyro read ─────────────────────────────────────────────────────────────────

void readGyro(float& gx, float& gy, float& gz) {
  if (!gyroDetected) { gx = gy = gz = 0.0f; return; }
  gyro.update();
  gx = gyro.getAngleX();
  gy = gyro.getAngleY();
  gz = gyro.getAngleZ();
}

// ── Periodic sensor update (called from loop on SENSOR_UPDATE_INTERVAL) ───────

void updateSensorData() {
  if (gyroDetected) {
    float gx, gy, gz;
    readGyro(gx, gy, gz);
    sendGyroResponse(gx, gy, gz);
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────

void initSensors() {
  gyroDetected = detectGyro();
  // Ultrasonic and line follower detect on first read.
}

#endif
