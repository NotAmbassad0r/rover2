# HAILO_NOTES.md — Current Known-Good Hailo State

**Read this before touching anything Hailo-related.** Documents the current, working, but
intentionally mismatched state of the Hailo stack — so a future session doesn't "fix" it
and break a working robot.

## Summary

| Component | Version | Notes |
|-----------|---------|-------|
| Hailo-10H firmware | **5.2.0** | Flashed on-device. |
| `libhailort.so` used by rover2-api | **5.2.0** | Hand-built from source (`hailo-ai/hailort` v5.2.0), installed at `/usr/local/lib/libhailort.so.5.2.0`. `rover2-api`'s venv links against this build directly. |
| dpkg-installed Hailo packages | **5.1.1** | `h10-hailort` and related `.deb` packages on the system are still 5.1.1 — never upgraded, left alone deliberately. |
| `hailort_server.service` | **failed, since the 2026-07-04 boot** | Fails because `/dev/hailo_pci_ep` does not exist on this system. |

## Why this is fine as-is

`rover2-api` performs all Hailo inference (YOLOv8m body tracker, Qwen2-VL VLM, and the
qwen2.5-instruct LLM) via the **direct-device path**: `hailo_platform.VDevice` /
`hailo_platform.genai.{LLM,VLM}` opened in-process, against the hand-built 5.2.0
`libhailort.so`. This path does **not** go through `hailort_server` at all.

`hailort_server.service` exists to arbitrate the Hailo device **across separate OS
processes** (e.g. multiple independent programs sharing one physical chip via a
`group_id`). rover2-api never needs that arbitration — body tracker, VLM, and LLM all run
inside the single rover2-api process, coordinated by Python-level locks
(`pi/hailo_session.py`) and VDevice ref-counting (`body_tracker._HailoInference`), not by
the server.

**Net effect: `hailort_server` being failed has zero functional impact on ROVER2.** It only
means a second, separate process cannot join the same `group_id` session externally.

## What this broke (and how it was fixed)

`pi/capture_live_nms.py` (a standalone one-shot script, run as a **separate process** from
rover2-api) tried to open its own `_HailoInference` device instance. As a second process,
it hit the multi-process arbitration path — which routes through `hailort_server` — and
since that server can't start (no `/dev/hailo_pci_ep`), the second-process open fails with
`HAILO_INVALID_DRIVER_VERSION` (error 76), because the dpkg 5.1.1 install is what a
`hailort_server`-mediated open would actually resolve to, not the hand-built 5.2.0 the
running rover2-api process itself uses.

This is why `capture_live_nms.py` is **superseded by the in-service NMS dump tap**
(`BodyTracker._maybe_dump_nms` in `pi/body_tracker.py`, triggered by the presence of
`/tmp/rover2_dump_nms`): the dump tap runs *inside* the already-running rover2-api
process, reusing the same in-process `_HailoInference` instance body_tracker already has
open — no second device open, no multi-process arbitration, no `hailort_server`
dependency, no version mismatch possible.

`capture_live_nms.py` is kept for reference / for use when rover2-api is stopped
(`sudo systemctl stop rover2-api`, freeing the device for a single-process capture), but
the in-service dump tap is the preferred capture path going forward.

## Deliberately NOT done

- **Not reconciling the 5.1.1 (dpkg) vs 5.2.0 (hand-built) split.** Reinstalling/upgrading
  the dpkg packages to 5.2.0, or otherwise unifying the two, is real work with real risk
  (this is the same HAT that runs the live robot) and is left as **deliberate future work**,
  not something to be automated by an assistant session. Do not attempt to "fix" the
  version mismatch without explicit sign-off — the current state, while inconsistent, is
  the known-good, tested configuration.
- **Not masking or restarting `hailort_server`** from an automated session. See below for
  the manual option.

## Optional manual cleanup (run yourself, not automated)

`hailort_server.service` will keep showing as failed in `systemctl status` / boot logs,
which is just noise given it's confirmed to have zero functional impact. If the repeated
failed-unit noise is bothersome, you can silence it yourself:

```bash
sudo systemctl mask hailort_server
```

This is optional and has no effect on rover2-api, body tracker, VLM, or the agent LLM path.
