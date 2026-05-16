/**
 * serial_protocol.h — JSON command parser and response senders.
 *
 * IMPORTANT: MeMegaPi.h (and SoftwareSerial.h before it) must be included
 * in rover_firmware.ino BEFORE this header is included.
 */
#ifndef SERIAL_PROTOCOL_H
#define SERIAL_PROTOCOL_H

const char COMMAND_TERMINATOR = '\n';

// ── Command structure ─────────────────────────────────────────────────────────

struct Command {
  String cmd;
  int    left_speed;
  int    right_speed;
  int    servo_port;
  int    servo_angle;
  bool   servo_confirm;
  String grip_action;
  String sensor_type;
  bool   valid;
};

// ── JSON helpers ──────────────────────────────────────────────────────────────

String getJsonString(const String& json, const char* key) {
  String search = "\"" + String(key) + "\":\"";
  int start = json.indexOf(search);
  if (start == -1) return "";
  start += search.length();
  int end = json.indexOf("\"", start);
  if (end == -1) return "";
  return json.substring(start, end);
}

int getJsonInt(const String& json, const char* key) {
  String search = "\"" + String(key) + "\":";
  int start = json.indexOf(search);
  if (start == -1) return 0;
  start += search.length();
  while (start < (int)json.length() && (json[start] == ' ' || json[start] == '\t')) start++;
  int end = start;
  if (json[end] == '-') end++;
  while (end < (int)json.length() && json[end] >= '0' && json[end] <= '9') end++;
  if (end == start || (end == start + 1 && json[start] == '-')) return 0;
  return json.substring(start, end).toInt();
}

String getCommand(const String& json) { return getJsonString(json, "cmd"); }

// ── Command parser ────────────────────────────────────────────────────────────

Command parseCommand(const String& jsonString) {
  Command cmd = {"", 0, 0, 0, 90, false, "", "", false};
  String json = jsonString;
  json.trim();
  if (!json.startsWith("{") || !json.endsWith("}")) {
    Serial.println(F("{\"evt\":\"error\",\"msg\":\"bad JSON\"}"));
    return cmd;
  }

  cmd.cmd = getCommand(json);
  if (cmd.cmd.length() == 0) {
    Serial.println(F("{\"evt\":\"error\",\"msg\":\"missing cmd\"}"));
    return cmd;
  }

  if (cmd.cmd == "drive") {
    cmd.left_speed  = getJsonInt(json, "l");
    cmd.right_speed = getJsonInt(json, "r");
    cmd.valid = true;
  }
  else if (cmd.cmd == "servo") {
    String portStr = getJsonString(json, "port");
    if (portStr == "confirm") {
      cmd.servo_confirm = true;
    } else {
      cmd.servo_port  = getJsonInt(json, "port");
      cmd.servo_angle = getJsonInt(json, "angle");
      if (cmd.servo_angle == 0) cmd.servo_angle = 90;
    }
    cmd.valid = true;
  }
  else if (cmd.cmd == "grip") {
    cmd.grip_action = getJsonString(json, "action");
    if (cmd.grip_action.length() == 0) cmd.grip_action = "open";
    cmd.valid = true;
  }
  else if (cmd.cmd == "sensor_req") {
    cmd.sensor_type = getJsonString(json, "sensor");
    cmd.valid = true;
  }
  else if (cmd.cmd == "detect" || cmd.cmd == "version" ||
           cmd.cmd == "ping"   || cmd.cmd == "stop") {
    cmd.valid = true;
  }
  else {
    Serial.print(F("{\"evt\":\"error\",\"msg\":\"unknown cmd: "));
    Serial.print(cmd.cmd);
    Serial.println(F("\"}"));
  }

  return cmd;
}

// ── Response senders ──────────────────────────────────────────────────────────

void sendReady(int motorsFound, int servoPort, bool gyroFound) {
  Serial.print(F("{\"evt\":\"ready\",\"motors\":"));
  Serial.print(motorsFound);
  Serial.print(F(",\"servo_port\":"));
  Serial.print(servoPort);
  Serial.print(F(",\"gyro\":"));
  Serial.print(gyroFound ? F("true") : F("false"));
  Serial.println(F("}"));
}

void sendDetected(int motors, int servo, int ultraPort, bool gyro, int linePort) {
  Serial.print(F("{\"evt\":\"detected\",\"motors\":"));
  Serial.print(motors);
  Serial.print(F(",\"servo\":"));
  Serial.print(servo);
  Serial.print(F(",\"ultrasonic\":"));
  Serial.print(ultraPort);
  Serial.print(F(",\"gyro\":"));
  Serial.print(gyro ? F("true") : F("false"));
  Serial.print(F(",\"line\":"));
  Serial.print(linePort);
  Serial.println(F("}"));
}

void sendEncoderEvent(int slot, long ticks) {
  Serial.print(F("{\"evt\":\"encoder\",\"motor\":"));
  Serial.print(slot);
  Serial.print(F(",\"ticks\":"));
  Serial.print(ticks);
  Serial.println(F("}"));
}

void sendUltrasonicResponse(int cm) {
  Serial.print(F("{\"evt\":\"sensor\",\"ultrasonic_cm\":"));
  Serial.print(cm);
  Serial.println(F("}"));
}

void sendGyroResponse(float x, float y, float z) {
  Serial.print(F("{\"evt\":\"sensor\",\"gyro_x\":"));
  Serial.print(x, 2);
  Serial.print(F(",\"gyro_y\":"));
  Serial.print(y, 2);
  Serial.print(F(",\"gyro_z\":"));
  Serial.print(z, 2);
  Serial.println(F("}"));
}

void sendAck(const char* cmdName) {
  Serial.print(F("{\"evt\":\"ack\",\"cmd\":\""));
  Serial.print(cmdName);
  Serial.println(F("\"}"));
}

void sendGripperAck(const String& state) {
  Serial.print(F("{\"evt\":\"gripper\",\"state\":\""));
  Serial.print(state);
  Serial.println(F("\"}"));
}

void sendWarn(const __FlashStringHelper* msg) {
  Serial.print(F("{\"evt\":\"warn\",\"msg\":\""));
  Serial.print(msg);
  Serial.println(F("\"}"));
}

void sendInfo(const __FlashStringHelper* msg) {
  Serial.print(F("{\"evt\":\"info\",\"msg\":\""));
  Serial.print(msg);
  Serial.println(F("\"}"));
}

void sendPong() {
  Serial.println(F("{\"evt\":\"pong\"}"));
}

#endif
