"""Tests for pi/config_public.py — pure dict masking, no hardware."""
from __future__ import annotations

from config_public import mask_config, public_config


class TestMaskConfig:
    def test_token_masked(self):
        result = mask_config({"ha_token": "abc123"})
        assert result["ha_token"] == "***"

    def test_api_key_masked(self):
        result = mask_config({"api_key": "secret"})
        assert result["api_key"] == "***"

    def test_password_masked(self):
        result = mask_config({"password": "letmein"})
        assert result["password"] == "***"

    def test_secret_masked(self):
        result = mask_config({"my_secret": "xyz"})
        assert result["my_secret"] == "***"

    def test_credential_masked(self):
        result = mask_config({"credential": "abc"})
        assert result["credential"] == "***"

    def test_private_masked(self):
        result = mask_config({"private_key": "pem..."})
        assert result["private_key"] == "***"

    def test_non_secret_passes_through(self):
        result = mask_config({"server_host": "localhost"})
        assert result["server_host"] == "localhost"

    def test_nested_secret_masked(self):
        cfg = {"guard": {"ha_token": "tok123", "enabled": True}}
        result = mask_config(cfg)
        assert result["guard"]["ha_token"] == "***"
        assert result["guard"]["enabled"] is True

    def test_list_items_processed(self):
        cfg = [{"token": "abc"}, {"host": "127.0.0.1"}]
        result = mask_config(cfg)
        assert result[0]["token"] == "***"
        assert result[1]["host"] == "127.0.0.1"

    def test_scalar_passthrough(self):
        assert mask_config(42) == 42
        assert mask_config("hello") == "hello"
        assert mask_config(None) is None

    def test_original_not_mutated(self):
        original = {"ha_token": "real_token", "host": "pi"}
        mask_config(original)
        assert original["ha_token"] == "real_token"

    def test_case_insensitive_key_match(self):
        result = mask_config({"HA_TOKEN": "tok"})
        assert result["HA_TOKEN"] == "***"


class TestPublicConfig:
    def test_public_config_masks_and_returns_copy(self):
        cfg = {"guard": {"ha_token": "s3cr3t"}, "server": {"host": "pi"}}
        result = public_config(cfg)
        assert result["guard"]["ha_token"] == "***"
        assert result["server"]["host"] == "pi"
        # Original unchanged
        assert cfg["guard"]["ha_token"] == "s3cr3t"
