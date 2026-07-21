"""Tests for BodyTracker._update_person_presence() — the person-presence
hysteresis gate that follow-mode decisions key off of (person_tracked ==
person_detected). No hardware, no Hailo, no camera — conftest.py stubs those
at import time.
"""
from __future__ import annotations

import unittest

from body_tracker import BodyTracker


def _make_tracker(**overrides) -> BodyTracker:
    cfg = {"body_tracker": dict(overrides)}
    return BodyTracker(drive=lambda l, r: None, stop=lambda: None, directions={}, config=cfg)


def _run_new_logic(tracker: BodyTracker, hits: list[bool]) -> list[bool]:
    """Feed a hit/miss sequence through the real (decay) implementation."""
    out = []
    for seen in hits:
        tracker._update_person_presence(seen)
        out.append(tracker._person_tracked)
    return out


def _run_old_logic(hits: list[bool], confirm_frames: int = 3, lost_frames: int = 4) -> list[bool]:
    """Reference re-implementation of the OLD reset-to-zero behaviour, for
    same-input comparison only — never touches the real class."""
    seen_streak = 0
    lost_streak = 0
    tracked = False
    out = []
    for seen in hits:
        if seen:
            seen_streak += 1
            lost_streak = 0
            if seen_streak >= confirm_frames:
                tracked = True
        else:
            lost_streak += 1
            seen_streak = 0  # old behaviour: hard reset
            if lost_streak >= lost_frames:
                tracked = False
        out.append(tracked)
    return out


# Real 60-sample bbox-presence sequence captured live this session (30s window,
# 0.5s polling, person genuinely in frame and moving) — 28/60 True (~47%).
REAL_CHOPPY_SEQUENCE = [
    True, True, False, False, False, False, False, True, True, False,
    False, False, True, False, True, False, True, False, True, True,
    True, False, True, True, False, False, False, True, False, True,
    True, False, False, True, False, True, True, False, False, False,
    False, False, False, True, True, True, True, False, False, True,
    True, False, False, False, False, False, True, True, True, True,
]

# Synthetic worst-case-for-old-logic pattern: "hit, hit, miss" repeated — max
# run of consecutive hits is exactly 2, NEVER 3, at a 67% hit rate. Old
# reset-on-any-miss logic can therefore never reach _CONFIRM_FRAMES=3 (every
# miss wipes the streak back to 0 before a 3rd consecutive hit ever lands),
# while decay logic climbs 1,2,(decay to 1),2,3(confirmed),... and holds.
SYNTHETIC_NO_TRIPLE_SEQUENCE = [True, True, False] * 20


class PresenceGateTests(unittest.TestCase):
    def test_choppy_real_sequence_confirms_and_holds_under_new_logic(self) -> None:
        tracker = _make_tracker()
        results = _run_new_logic(tracker, REAL_CHOPPY_SEQUENCE)

        # New (decay) logic must actually confirm presence at some point.
        self.assertTrue(any(results), "decay logic never confirmed presence on real choppy data")

        first_true = results.index(True)
        held_true_fraction = sum(results[first_true:]) / len(results[first_true:])
        self.assertGreaterEqual(
            held_true_fraction, 0.8,
            f"presence should HOLD through the rest of the choppy run, "
            f"only held {held_true_fraction:.0%} of the time after first confirming",
        )

    def test_decay_confirms_where_old_reset_logic_never_could(self) -> None:
        # Same input, two implementations: on a pattern that never contains 3
        # consecutive hits, old reset-on-any-miss logic can mathematically
        # never confirm presence — every single miss wipes the streak before
        # a 3rd consecutive hit can land. Decay logic climbs through the
        # misses instead and does confirm. This isolates the exact mechanism
        # this change fixes.
        old_results = _run_old_logic(SYNTHETIC_NO_TRIPLE_SEQUENCE)
        self.assertFalse(
            any(old_results),
            "old reset-to-zero logic unexpectedly confirmed presence — pattern assumption wrong",
        )

        tracker = _make_tracker()
        new_results = _run_new_logic(tracker, SYNTHETIC_NO_TRIPLE_SEQUENCE)
        self.assertTrue(
            any(new_results),
            "decay logic failed to confirm presence on a pattern old logic structurally cannot",
        )

    def test_sustained_absence_still_flips_to_lost(self) -> None:
        tracker = _make_tracker()
        # Confirm presence first.
        for _ in range(3):
            tracker._update_person_presence(True)
        self.assertTrue(tracker._person_tracked)

        # Person genuinely leaves — many consecutive misses.
        results = []
        for _ in range(10):
            tracker._update_person_presence(False)
            results.append(tracker._person_tracked)

        self.assertIn(False, results, "sustained absence never flipped person_tracked to False")
        # Once lost, stays lost for the remainder of a continued-absence run.
        first_false = results.index(False)
        self.assertFalse(any(results[first_false:]))
        # And it shouldn't take forever — default lost_frames is 6.
        self.assertLessEqual(first_false + 1, tracker._lost_frames)

    def test_isolated_single_hits_do_not_falsely_confirm_presence(self) -> None:
        tracker = _make_tracker()
        # Long miss runs between single, isolated hits — pure noise, not a
        # real sustained presence. 20 misses is far more than enough for the
        # decay (-1/miss, floor 0) to fully erase a single +1 hit before the
        # next isolated hit arrives.
        pattern = ([False] * 20 + [True]) * 5
        results = _run_new_logic(tracker, pattern)
        self.assertFalse(
            any(results),
            "isolated single hits separated by long gaps falsely confirmed presence",
        )

    def test_confirm_and_lost_frames_are_config_tunable(self) -> None:
        tracker = _make_tracker(presence_confirm_frames=5, presence_lost_frames=10)
        self.assertEqual(tracker._confirm_frames, 5)
        self.assertEqual(tracker._lost_frames, 10)

    def test_defaults_when_not_configured(self) -> None:
        tracker = _make_tracker()
        self.assertEqual(tracker._confirm_frames, 3)
        self.assertEqual(tracker._lost_frames, 6)


if __name__ == "__main__":
    unittest.main()
