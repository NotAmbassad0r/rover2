/**
 * ROVER2 MegaPi Firmware v2.1.3
 *
 * Safety-first design:
 *   - setup() does NOT run any motor detection or drive pulses.
 *   - loop() does NOT auto-detect hardware or send drive commands.
 *   - Hardware detection runs ONLY when {"cmd":"detect"} is received.
 *   - Emergency stop: if no serial command arrives within 5 s, all motors stop.
 *
 * Startup sequence:
 *   1. Serial 115200
 *   2. Wire.begin() for I2C
 *   3. Encoder init (no movement)
 *   4. Gyro I2C detection
 *   5. {"evt":"ready","motors":0,"servo_port":-1,"gyro":bool}
 *
 * Supported commands:
 *   {"cmd":"drive","l":N,"r":N}             → move motors
 *   {"cmd":"stop"}                           → emergency stop
 *   {"cmd":"sensor_req","sensor":"..."}      → read a sensor once
 *   {"cmd":"grip","action":"open"|"close"}   → move gripper (DC motor, PORT4B)
 *   {"cmd":"detect"}                         → full hardware detection (ONCE)
 *   {"cmd":"ping"}                           → {"evt":"pong"}
 *   {"cmd":"version"}                        → print version string
 */

// SoftwareSerial must appear before MeMegaPi.h.
// MeMegaPi.h bundles its own Servo — do NOT add #include <Servo.h>.
#include <SoftwareSerial.h>
#include "MeMegaPi.h"
#include <Wire.h>

#include "serial_protocol.h"
#include "motor_control.h"
#include "sensor_reader.h"

// ── Serial buffer ─────────────────────────────────────────────────────────────

const int SERIAL_BUFFER_SIZE = 256;
String serialBuffer = "";

// ── Timing ────────────────────────────────────────────────────────────────────

unsigned long lastSensorUpdate      = 0;
const unsigned long SENSOR_UPDATE_MS = 100;

// Emergency stop watchdog: stop motors if no serial activity for 5 s.
unsigned long lastSerialActivityMs  = 0;
const unsigned long WATCHDOG_MS     = 5000UL;
bool watchdogTriggered              = false;

// ── Setup ─────────────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);

  Wire.begin();

  // Encoder init only — no drive pulses, no detection.
  initMotors();

  // Gyro I2C detection.
  initSensors();

  Serial.println(F("ROVER2 v1.0.0"));

  // Report ready with 0 motors — detection happens on {"cmd":"detect"}.
  sendReady(0, -1, gyroDetected);

  lastSerialActivityMs = millis();
}

// ── Main loop ─────────────────────────────────────────────────────────────────

void loop() {
  // Non-blocking serial read.
  while (Serial.available() > 0) {
    lastSerialActivityMs = millis();
    watchdogTriggered    = false;
    char c = Serial.read();
    if (c == COMMAND_TERMINATOR) {
      if (serialBuffer.length() > 0) {
        handleCommand(serialBuffer);
        serialBuffer = "";
      }
    } else {
      serialBuffer += c;
      if (serialBuffer.length() >= SERIAL_BUFFER_SIZE) {
        Serial.println(F("{\"evt\":\"error\",\"msg\":\"serial buffer overflow\"}"));
        serialBuffer = "";
      }
    }
  }

  // Emergency stop watchdog.
  if (!watchdogTriggered &&
      (millis() - lastSerialActivityMs) >= WATCHDOG_MS) {
    stopMotors();
    watchdogTriggered = true;
    sendWarn(F("watchdog: no serial for 5s — motors stopped"));
  }

  // Encoder ISR processing and reporting.
  updateEncoders();
  reportEncoders();

  // Gripper state machine (no-op — servo holds position).
  updateGripper();

  // Periodic sensor reads.
  unsigned long now = millis();
  if (now - lastSensorUpdate >= SENSOR_UPDATE_MS) {
    lastSensorUpdate = now;
    updateSensorData();
  }
}

// ── Command handler ───────────────────────────────────────────────────────────

void handleCommand(const String& jsonString) {
  // set_gripper_port — update which dcMotors[] index the gripper uses.
  if (getCommand(jsonString) == "set_gripper_port") {
    String portName = getJsonString(jsonString, "port");
    int idx = -1;
    if      (portName == "PORT1A") idx = 0;
    else if (portName == "PORT1B") idx = 1;
    else if (portName == "PORT2A") idx = 2;
    else if (portName == "PORT2B") idx = 3;
    else if (portName == "PORT3A") idx = 4;
    else if (portName == "PORT3B") idx = 5;
    else if (portName == "PORT4A") idx = 6;
    else if (portName == "PORT4B") idx = 7;
    if (idx >= 0) {
      setGripperPort(idx);
      char buf[64];
      snprintf(buf, sizeof(buf),
        "{\"evt\":\"info\",\"msg\":\"gripper port set to %s\"}", portName.c_str());
      Serial.println(buf);
    } else {
      Serial.println(F("{\"evt\":\"error\",\"msg\":\"set_gripper_port: unknown port\"}"));
    }
    return;
  }

  // set_port is not in the Command struct — handle it before parseCommand()
  // so it doesn't trigger the "unknown cmd" error path.
  if (getCommand(jsonString) == "set_port") {
    String sensor = getJsonString(jsonString, "sensor");
    int port      = getJsonInt(jsonString, "port");
    if (sensor == "ultrasonic" && port >= 3 && port <= 8) {
      detectedUltrasonicPort = port - 3;
      char buf[56];
      snprintf(buf, sizeof(buf),
        "{\"evt\":\"info\",\"msg\":\"ultrasonic port set to PORT_%d\"}", port);
      Serial.println(buf);
    } else {
      Serial.println(F("{\"evt\":\"error\",\"msg\":\"set_port: invalid sensor or port\"}"));
    }
    return;
  }

  Command cmd = parseCommand(jsonString);
  if (!cmd.valid) return;

  if (cmd.cmd == "drive") {
    runMotors(cmd.left_speed, cmd.right_speed);
    sendAck("drive");
  }

  else if (cmd.cmd == "stop") {
    stopMotors();
    sendAck("stop");
  }

  else if (cmd.cmd == "ping") {
    sendPong();
  }

  else if (cmd.cmd == "servo") {
    sendWarn(F("servo not available — gripper uses DC motor on PORT4B"));
  }

  else if (cmd.cmd == "grip") {
    setGripper(cmd.grip_action);
  }

  else if (cmd.cmd == "sensor_req") {
    if (cmd.sensor_type == "ultrasonic") {
      int d = readUltrasonic();
      if (d > 0) sendUltrasonicResponse(d);
      else       Serial.println(F("{\"evt\":\"sensor\",\"ultrasonic_cm\":null,\"err\":\"no echo\"}"));
    }
    else if (cmd.sensor_type == "gyro") {
      float gx, gy, gz;
      readGyro(gx, gy, gz);
      sendGyroResponse(gx, gy, gz);
    }
    else if (cmd.sensor_type == "line") {
      int s = readLineSensor();
      if (s >= 0) {
        char buf[48];
        snprintf(buf, sizeof(buf), "{\"evt\":\"sensor\",\"line_state\":%d}", s);
        Serial.println(buf);
      }
    }
    else {
      Serial.println(F("{\"evt\":\"error\",\"msg\":\"unknown sensor type\"}"));
    }
  }

  else if (cmd.cmd == "detect") {
    lastSerialActivityMs = millis();
    // Full hardware detection — runs ONCE per command, never autonomously.
    detectDCMotors();

    int motorsFound = (leftMotorIdx != -1 ? 1 : 0) + (rightMotorIdx != -1 ? 1 : 0);
    lastSerialActivityMs = millis();
    sendDetected(motorsFound, detectedServoPort,
                 detectedUltrasonicPort, gyroDetected, detectedLinePort);
  }

  else if (cmd.cmd == "version") {
    Serial.println(F("ROVER2 v1.0.0"));
  }
}
