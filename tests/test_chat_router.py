"""Tests for pi/chat_router.py — pure regex routing, no hardware."""
from __future__ import annotations

import pytest

from chat_router import route


class TestRoute:
    def test_stop(self):
        r = route("stop")
        assert r["action"] == "stop"
        assert r["needs_camera"] is False

    def test_halt(self):
        assert route("halt")["action"] == "stop"

    def test_forward(self):
        assert route("move forward")["action"] == "forward"

    def test_advance(self):
        assert route("advance")["action"] == "forward"

    def test_backward(self):
        assert route("reverse")["action"] == "backward"

    def test_go_back(self):
        assert route("go back")["action"] == "backward"

    def test_left(self):
        assert route("turn left")["action"] == "left"

    def test_right(self):
        assert route("turn right")["action"] == "right"

    def test_follow(self):
        assert route("follow me")["action"] == "follow"

    def test_come_here(self):
        assert route("come here")["action"] == "follow"

    def test_describe(self):
        r = route("what do you see")
        assert r["action"] == "describe"
        assert r["needs_camera"] is True
        assert r["reply"] is None  # VLM fills this in

    def test_describe_variant(self):
        assert route("look around")["action"] == "describe"

    def test_grip_open(self):
        assert route("open gripper")["action"] == "grip_open"

    def test_grip_close(self):
        assert route("close gripper")["action"] == "grip_close"

    def test_arm_up(self):
        assert route("arm up")["action"] == "arm_up"

    def test_arm_down(self):
        assert route("arm down")["action"] == "arm_down"

    def test_lift_arm(self):
        assert route("lift arm")["action"] == "arm_up"

    def test_unfollow_beats_stop(self):
        r = route("stop following")
        assert r["action"] == "unfollow"  # more specific — comes before generic "stop"

    def test_detect(self):
        assert route("detect")["action"] == "detect"

    def test_undetect(self):
        assert route("stop detecting")["action"] == "undetect"

    def test_unknown_command_returns_none_action(self):
        r = route("sing me a song")
        assert r["action"] is None
        assert r["needs_camera"] is False
        assert r["reply"] is not None  # canned "unrecognised" reply

    def test_case_insensitive(self):
        assert route("STOP")["action"] == "stop"
        assert route("Follow Me")["action"] == "follow"

    def test_reply_present_for_all_actions(self):
        commands = ["stop", "forward", "backward", "left", "right",
                    "follow me", "stop following", "detect", "stop detecting",
                    "open gripper", "close gripper", "arm up", "arm down"]
        for cmd in commands:
            r = route(cmd)
            # Every action except describe should have a non-None reply
            if r["action"] != "describe":
                assert r["reply"] is not None, f"No reply for: {cmd}"
