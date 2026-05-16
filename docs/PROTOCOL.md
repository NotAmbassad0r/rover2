# ROVER2 serial protocol (MegaPi)

Newline-delimited JSON over USB serial at **115200** baud.

## Commands (Pi → Arduino)

| Command | JSON | Notes |
|---------|------|-------|
| Drive | `{"cmd":"drive","l":-120,"r":120}` | `l`/`r` in −255…255 |
| Stop | `{"cmd":"stop"}` | Drive, gripper, and arm all off |
| Grip | `{"cmd":"grip","action":"open"}` or `"close"` | DC gripper open/close on PORT4B |
| Arm | `{"cmd":"arm","speed":150}` | Lift motor on PORT3B (−255…255, `0` = stop arm) |
| Arm pulse | `{"cmd":"arm","action":"up"}` or `"down"` | ~800 ms nudge (bench test) |
| Ultrasonic | `{"cmd":"sensor_req","sensor":"ultrasonic"}` | One-shot read |
| Ping | `{"cmd":"ping"}` | `{"evt":"pong"}` |
| Version | `{"cmd":"version"}` | `{"evt":"version","fw":"rover2-basic-1.0.0"}` |

## Events (Arduino → Pi)

| Event | Example |
|-------|---------|
| Ready | `{"evt":"ready","fw":"rover2-basic-1.0.3","motors":3}` |
| Sensor | `{"evt":"sensor","ultrasonic_cm":62}` or `"ultrasonic_cm":null` if no echo |
| Gripper | `{"evt":"gripper","state":"open"}` |
| Arm | `{"evt":"arm","state":"idle"}` or `"up"` / `"down"` during pulse |
| Error | `{"evt":"error","msg":"..."}` |

## Hardware ports (Makeblock Ultimate 2.0)

| Function | Port |
|----------|------|
| Left drive | PORT1B |
| Right drive | PORT2B |
| Gripper DC | PORT4B |
| Arm lift DC | PORT3B |
| Ultrasonic | PORT_6 (change default in `firmware/rover2_basic/rover2_basic.ino` if needed) |

Watchdog: motors stop if no serial command for **5 seconds**.
