"""Tests for pi/config_store.py — validation, clamping, merge, and persistence."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from config_store import (
    _clamp,
    extract_tuning_patch,
    merge_tuning,
    save_config,
    tuning_schema_public,
)


class TestClamp:
    def test_int_within_range(self):
        assert _clamp(100, 30, 255, "int") == 100

    def test_int_above_max_clamped(self):
        assert _clamp(300, 30, 255, "int") == 255

    def test_int_below_min_clamped(self):
        assert _clamp(10, 30, 255, "int") == 30

    def test_float_within_range(self):
        assert abs(_clamp(0.5, 0.1, 1.0, "float") - 0.5) < 1e-9

    def test_float_at_min_boundary(self):
        assert abs(_clamp(0.1, 0.1, 1.0, "float") - 0.1) < 1e-9

    def test_float_at_max_boundary(self):
        assert abs(_clamp(1.0, 0.1, 1.0, "float") - 1.0) < 1e-9

    def test_bool_true(self):
        assert _clamp(True, None, None, "bool") is True

    def test_bool_truthy_coerced(self):
        assert _clamp(1, None, None, "bool") is True

    def test_bool_falsy_coerced(self):
        assert _clamp(0, None, None, "bool") is False

    def test_str_stripped(self):
        assert _clamp("  hello  ", None, None, "str") == "hello"


class TestExtractTuningPatch:
    def test_valid_body_tracker_confidence(self):
        patch = extract_tuning_patch({"body_tracker": {"confidence": 0.55}})
        assert patch["body_tracker"]["confidence"] == 0.55

    def test_out_of_range_float_clamped(self):
        # confidence max is 0.95
        patch = extract_tuning_patch({"body_tracker": {"confidence": 1.5}})
        assert patch["body_tracker"]["confidence"] == 0.95

    def test_out_of_range_int_clamped(self):
        # turn_speed min is 30
        patch = extract_tuning_patch({"body_tracker": {"turn_speed": 10}})
        assert patch["body_tracker"]["turn_speed"] == 30

    def test_unknown_section_ignored(self):
        patch = extract_tuning_patch({
            "body_tracker": {"confidence": 0.5},
            "nonexistent": {"foo": "bar"},
        })
        assert "nonexistent" not in patch
        assert "body_tracker" in patch

    def test_unknown_fields_in_known_section_ignored(self):
        patch = extract_tuning_patch({
            "body_tracker": {"confidence": 0.5, "made_up_field": 99},
        })
        assert "made_up_field" not in patch["body_tracker"]

    def test_avoid_default_left(self):
        patch = extract_tuning_patch({"body_tracker": {"avoid_default": "left"}})
        assert patch["body_tracker"]["avoid_default"] == "left"

    def test_avoid_default_right(self):
        patch = extract_tuning_patch({"body_tracker": {"avoid_default": "Right"}})
        assert patch["body_tracker"]["avoid_default"] == "right"

    def test_avoid_default_invalid_raises(self):
        with pytest.raises(ValueError, match="avoid_default"):
            extract_tuning_patch({"body_tracker": {"avoid_default": "forward"}})

    def test_non_dict_body_raises(self):
        with pytest.raises(ValueError):
            extract_tuning_patch("not a dict")  # type: ignore[arg-type]

    def test_non_dict_section_raises(self):
        with pytest.raises(ValueError):
            extract_tuning_patch({"body_tracker": "oops"})

    def test_empty_body_raises(self):
        with pytest.raises(ValueError, match="no recognized tuning fields"):
            extract_tuning_patch({})

    def test_all_unknown_fields_raises(self):
        with pytest.raises(ValueError, match="no recognized tuning fields"):
            extract_tuning_patch({"nonexistent": {"foo": 1}})

    def test_safety_section(self):
        patch = extract_tuning_patch({"safety": {"safe_distance_cm": 35}})
        assert patch["safety"]["safe_distance_cm"] == 35

    def test_multiple_sections(self):
        patch = extract_tuning_patch({
            "body_tracker": {"confidence": 0.6},
            "safety": {"safe_distance_cm": 40},
        })
        assert "body_tracker" in patch
        assert "safety" in patch


class TestMergeTuning:
    def test_section_added_if_missing(self):
        merged = merge_tuning({}, {"body_tracker": {"confidence": 0.6}})
        assert merged["body_tracker"]["confidence"] == 0.6

    def test_existing_key_updated(self):
        cfg = {"body_tracker": {"confidence": 0.4, "turn_speed": 200}}
        merged = merge_tuning(cfg, {"body_tracker": {"confidence": 0.7}})
        assert merged["body_tracker"]["confidence"] == 0.7
        assert merged["body_tracker"]["turn_speed"] == 200

    def test_original_config_not_mutated(self):
        cfg = {"body_tracker": {"confidence": 0.4}}
        merge_tuning(cfg, {"body_tracker": {"confidence": 0.8}})
        assert cfg["body_tracker"]["confidence"] == 0.4

    def test_non_dict_section_reset_to_dict(self):
        cfg = {"body_tracker": "broken"}
        merged = merge_tuning(cfg, {"body_tracker": {"confidence": 0.6}})
        assert isinstance(merged["body_tracker"], dict)

    def test_multiple_sections_merged(self):
        cfg = {"body_tracker": {"confidence": 0.4}, "safety": {"safe_distance_cm": 30}}
        merged = merge_tuning(cfg, {
            "body_tracker": {"confidence": 0.6},
            "drive": {"max_speed": 200},
        })
        assert merged["body_tracker"]["confidence"] == 0.6
        assert merged["safety"]["safe_distance_cm"] == 30
        assert merged["drive"]["max_speed"] == 200


class TestSaveConfig:
    def test_saves_valid_yaml(self, tmp_path: Path):
        cfg = {"server": {"host": "0.0.0.0", "port": 8082}, "drive": {"max_speed": 255}}
        path = tmp_path / "config.yaml"
        save_config(path, cfg)
        loaded = yaml.safe_load(path.read_text())
        assert loaded["server"]["port"] == 8082
        assert loaded["drive"]["max_speed"] == 255

    def test_round_trips_all_types(self, tmp_path: Path):
        cfg = {
            "body_tracker": {
                "confidence": 0.55,
                "turn_speed": 210,
                "enabled": True,
                "avoid_default": "left",
            }
        }
        path = tmp_path / "config.yaml"
        save_config(path, cfg)
        loaded = yaml.safe_load(path.read_text())
        assert loaded["body_tracker"]["confidence"] == 0.55
        assert loaded["body_tracker"]["turn_speed"] == 210
        assert loaded["body_tracker"]["enabled"] is True


class TestTuningSchemaPublic:
    def test_returns_dict(self):
        schema = tuning_schema_public()
        assert isinstance(schema, dict)

    def test_body_tracker_section_present(self):
        schema = tuning_schema_public()
        assert "body_tracker" in schema
        assert "confidence" in schema["body_tracker"]

    def test_field_has_type_min_max(self):
        schema = tuning_schema_public()
        conf = schema["body_tracker"]["confidence"]
        assert "type" in conf
        assert "min" in conf
        assert "max" in conf
