/**
 * motor_control.h — DC motor detection, encoder motors, gripper.
 *
 * Physical wiring (Makeblock Ultimate 2.0):
 *   Drive motor power    → MegaPi DC driver ports (PORT1A/1B/2A/2B)
 *   Drive motor encoders → SLOT1 / SLOT2 interrupt pins
 *   Gripper DC motor     → PORT4B (black wire → 4B−, white wire → 4B+)
 *
 * Detection is ONLY triggered by {"cmd":"detect"} over serial.
 * It is NEVER called automatically in setup() or loop().
 *
 * Include order in rover_firmware.ino (before this header):
 *   #include <SoftwareSerial.h>
 *   #include "MeMegaPi.h"
 */
#ifndef MOTOR_CONTROL_H
#define MOTOR_CONTROL_H

// ── Drive DC motor objects ────────────────────────────────────────────────────

MeMegaPiDCMotor dcMotors[8] = {
  MeMegaPiDCMotor(PORT1A),  // 0
  MeMegaPiDCMotor(PORT1B),  // 1
  MeMegaPiDCMotor(PORT2A),  // 2
  MeMegaPiDCMotor(PORT2B),  // 3
  MeMegaPiDCMotor(PORT3A),  // 4
  MeMegaPiDCMotor(PORT3B),  // 5
  MeMegaPiDCMotor(PORT4A),  // 6
  MeMegaPiDCMotor(PORT4B),  // 7
};

// Pre-assigned to correct ports so drive works immediately on boot.
// detectDCMotors() reassigns these same values when {"cmd":"detect"} runs.
int leftMotorIdx  = 1;  // PORT1B
int rightMotorIdx = 3;  // PORT2B

// ── Gripper DC motor ──────────────────────────────────────────────────────────
// Close: +150 for 600 ms.  Open: -150 for 600 ms.
// Non-blocking: setGripper() starts the motor; updateGripper() stops it.
// Port is selectable at runtime via {"cmd":"set_gripper_port","port":"PORTxY"}.

// -1 kept for protocol compatibility; gripper is DC-driven, not servo-based.
int detectedServoPort = -1;

// Index into dcMotors[] for the active gripper port. Default 7 = PORT4B.
// Updated at runtime by the set_gripper_port command.
int gripperMotorIdx = 7;

unsigned long gripperStopMs      = 0;
bool          gripperRunning     = false;
String        gripperPendingAck  = "";

// ── Encoder motor objects ─────────────────────────────────────────────────────

MeEncoderOnBoard enc1(SLOT1);
MeEncoderOnBoard enc2(SLOT2);

bool enc1Active   = false;
bool enc2Active   = false;
long enc1LastSent = 0;
long enc2LastSent = 0;

// ── DC motor detection ────────────────────────────────────────────────────────
// Called ONLY from handleCommand() when {"cmd":"detect"} is received.
// Never called from setup() or loop().
//
// MeEncoderOnBoard (SLOT1/SLOT2) are interrupt-based encoder inputs wired to
// encoder-equipped motors, NOT to the DC driver outputs on PORT1A/1B/2A/2B.
// Encoder tick counting cannot detect DC-only motors — hardcode instead.
//
// Makeblock Ultimate 2.0 drive wiring:
//   Left motor  → PORT1B (index 1 in dcMotors[])
//   Right motor → PORT2B (index 3 in dcMotors[])

void detectDCMotors() {
  leftMotorIdx  = 1;  // PORT1B
  rightMotorIdx = 3;  // PORT2B

  Serial.println(F("{\"evt\":\"info\",\"msg\":\"drive motors assigned: PORT1B (left) PORT2B (right)\"}"));

  // Safety: ensure all drive ports are stopped after assignment.
  for (int i = 0; i < 4; i++) dcMotors[i].run(0);
}

// ── Motor drive ───────────────────────────────────────────────────────────────

void stopMotors() {
  if (leftMotorIdx  >= 0) dcMotors[leftMotorIdx].run(0);
  if (rightMotorIdx >= 0) dcMotors[rightMotorIdx].run(0);
  // Stop all ports defensively (handles pre-detection state and gripper).
  for (int i = 0; i < 8; i++) dcMotors[i].run(0);
}

void runMotors(int leftSpeed, int rightSpeed) {
  leftSpeed  = constrain(leftSpeed,  -255, 255);
  rightSpeed = constrain(rightSpeed, -255, 255);
  if (leftMotorIdx  >= 0) dcMotors[leftMotorIdx].run(-leftSpeed);
  if (rightMotorIdx >= 0) dcMotors[rightMotorIdx].run(-rightSpeed);
}

// ── Encoder processing ────────────────────────────────────────────────────────

void updateEncoders() {
  enc1.loop();
  enc2.loop();
}

void reportEncoders() {
  long t1 = enc1.getCurPos();
  long t2 = enc2.getCurPos();

  if (t1 != enc1LastSent) {
    enc1Active = true;
    sendEncoderEvent(1, t1);
    enc1LastSent = t1;
  }
  if (t2 != enc2LastSent) {
    enc2Active = true;
    sendEncoderEvent(2, t2);
    enc2LastSent = t2;
  }
}

long readEncoder(int slot) {
  return (slot == 1) ? enc1.getCurPos() : enc2.getCurPos();
}

// ── Gripper ───────────────────────────────────────────────────────────────────

void setGripper(const String& action) {
  if (action == "close") {
    dcMotors[gripperMotorIdx].run(150);
    gripperStopMs     = millis() + 600UL;
    gripperRunning    = true;
    gripperPendingAck = "close";
  } else if (action == "open") {
    dcMotors[gripperMotorIdx].run(-150);
    gripperStopMs     = millis() + 600UL;
    gripperRunning    = true;
    gripperPendingAck = "open";
  }
}

// Must be called every loop() iteration — stops the gripper motor after 600 ms.
void updateGripper() {
  if (gripperRunning && millis() >= gripperStopMs) {
    dcMotors[gripperMotorIdx].run(0);
    gripperRunning = false;
    sendGripperAck(gripperPendingAck);
    gripperPendingAck = "";
  }
}

// Stop current port before switching so it does not stay powered.
void setGripperPort(int idx) {
  dcMotors[gripperMotorIdx].run(0);
  gripperMotorIdx = idx;
}

// ── Init ──────────────────────────────────────────────────────────────────────
// ONLY initialises encoders. No motor pulses, no detection, no movement.

void initMotors() {
  enc1.setMotionMode(DIRECT_MODE);
  enc2.setMotionMode(DIRECT_MODE);
  // Stop all motor ports as a safety measure.
  for (int i = 0; i < 8; i++) dcMotors[i].run(0);
}

#endif
