from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import asynccontextmanager

import pytest
from click.testing import CliRunner

from memtomem.config import (
    Mem2MemConfig,
    load_config_overrides,
    coerce_and_validate,
    FIELD_CONSTRAINTS,
)
from memtomem.observability.session_tracing import (
    format_payload,
    format_propagated_metadata,
    sanitize_metadata_key,
    sanitize_metadata_value,
    trace_session,
)
from memtomem.cli import cli


@pytest.fixture
def override_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import memtomem.config as _cfg

    p = tmp_path / "config.json"
    monkeypatch.setattr(_cfg, "_override_path", lambda: p)
    return p


class TestConfigConstraints:
    def test_valid_fields(self, override_path: Path):
        override_path.write_text(
            json.dumps(
                {
                    "session_trace": {
                        "enabled": True,
                        "sampling_rate": 0.5,
                        "payload_mode": "full",
                        "max_payload_chars": 5000,
                    }
                }
            ),
            encoding="utf-8",
        )
        cfg = Mem2MemConfig()
        load_config_overrides(cfg)
        assert cfg.session_trace.enabled is True
        assert cfg.session_trace.sampling_rate == 0.5
        assert cfg.session_trace.payload_mode == "full"
        assert cfg.session_trace.max_payload_chars == 5000

    def test_invalid_sampling_rate(self):
        constraint = FIELD_CONSTRAINTS["session_trace.sampling_rate"]
        with pytest.raises(ValueError):
            coerce_and_validate(1.5, constraint)
        with pytest.raises(ValueError):
            coerce_and_validate(-0.1, constraint)

    def test_invalid_payload_mode(self):
        constraint = FIELD_CONSTRAINTS["session_trace.payload_mode"]
        with pytest.raises(ValueError):
            coerce_and_validate("everything", constraint)

    def test_invalid_max_payload_chars(self):
        constraint = FIELD_CONSTRAINTS["session_trace.max_payload_chars"]
        with pytest.raises(ValueError):
            coerce_and_validate(0, constraint)
        with pytest.raises(ValueError):
            coerce_and_validate(-10, constraint)

    def test_langfuse_validator_keys_missing(self):
        # Setting enabled=True and langfuse_enabled=True without keys should fail validation
        with pytest.raises(Exception):
            from memtomem.config import SessionTraceConfig

            SessionTraceConfig(enabled=True, langfuse_enabled=True)

    def test_langfuse_validator_package_missing(self):
        # Setting enabled=True and langfuse_enabled=True but mocking find_spec to return None
        with patch("importlib.util.find_spec", return_value=None):
            with pytest.raises(Exception) as excinfo:
                from memtomem.config import SessionTraceConfig

                SessionTraceConfig(
                    enabled=True,
                    langfuse_enabled=True,
                    langfuse_public_key="pk",
                    langfuse_secret_key="sk",
                )
            assert "package is not installed" in str(excinfo.value)


class TestPayloadSanitization:
    def test_metadata_mode(self):
        payload = {"foo": "bar", "api_key": "secret"}
        formatted = format_payload(payload, "metadata", 1000)
        assert formatted is None

    def test_full_mode(self):
        payload = {"foo": "bar", "secret": "keep_intact"}
        formatted = format_payload(payload, "full", 1000)
        assert formatted == {"foo": "bar", "secret": "keep_intact"}

    def test_redacted_mode(self):
        payload = {
            "foo": "bar",
            "api_key": "sk-12345",
            "secret_token": "some-token",
            "password": "pass",
            "user_key": "ukey",
        }
        formatted = format_payload(payload, "redacted", 1000)
        assert formatted["foo"] == "bar"
        assert formatted["api_key"] == "***"
        assert formatted["secret_token"] == "***"
        assert formatted["password"] == "***"
        assert formatted["user_key"] == "***"

    def test_truncation(self):
        payload = {"long_text": "a" * 1000}
        formatted = format_payload(payload, "full", 100)
        assert len(formatted) <= 100
        assert "...[TRUNCATED]" in formatted

    def test_metadata_keys_values(self):
        assert sanitize_metadata_key("my-key_123") == "mykey123"
        assert sanitize_metadata_key("") == "key"
        assert sanitize_metadata_key("@#$%^") == "key"

        long_val = "x" * 300
        sanitized_val = sanitize_metadata_value(long_val)
        assert len(sanitized_val) == 200
        assert sanitized_val.endswith("...")

        meta = {
            "my-key-1": "normal",
            "special@key": "x" * 250,
        }
        clean_meta = format_propagated_metadata(meta)
        assert "mykey1" in clean_meta
        assert clean_meta["mykey1"] == "normal"
        assert "specialkey" in clean_meta
        assert len(clean_meta["specialkey"]) == 200
        assert clean_meta["specialkey"].endswith("...")


class TestTraceSessionContext:
    def test_disabled_no_op(self, tmp_path: Path):
        # Create dummy config with enabled=False
        class DummyConfig:
            enabled = False
            jsonl_enabled = True
            jsonl_path = tmp_path / "traces.jsonl"
            sampling_rate = 1.0
            payload_mode = "full"
            max_payload_chars = 10000

        with patch(
            "memtomem.observability.session_tracing.get_trace_config", return_value=DummyConfig()
        ):
            with trace_session("cmd", "evt") as trace_ctx:
                trace_ctx["session_id"] = "s1"
                trace_ctx["metadata"]["foo"] = "bar"

            # Assert file wasn't created
            assert not (tmp_path / "traces.jsonl").exists()

    def test_local_jsonl_writing(self, tmp_path: Path):
        jsonl_file = tmp_path / "traces.jsonl"

        class DummyConfig:
            enabled = True
            jsonl_enabled = True
            jsonl_path = jsonl_file
            sampling_rate = 1.0
            payload_mode = "full"
            max_payload_chars = 10000
            langfuse_enabled = False

        with patch(
            "memtomem.observability.session_tracing.get_trace_config", return_value=DummyConfig()
        ):
            with trace_session("test_cmd", "test_evt", agent_id="my-agent") as trace_ctx:
                trace_ctx["session_id"] = "session-123"
                trace_ctx["metadata"]["my_meta"] = "val"
                trace_ctx["payload"]["arg"] = "x"

            assert jsonl_file.exists()
            lines = jsonl_file.read_text(encoding="utf-8").strip().split("\n")
            assert len(lines) == 1
            data = json.loads(lines[0])
            assert data["command"] == "test_cmd"
            assert data["event_type"] == "test_evt"
            assert data["agent_id"] == "my-agent"
            assert data["session_id"] == "session-123"
            assert data["metadata"] == {"my_meta": "val"}
            assert data["payload"] == {"arg": "x"}
            assert data["status"] == "success"
            assert data["exit_code"] == 0

    def test_failure_isolation(self, tmp_path: Path):
        # Test that write errors to JSONL do not crash the context block
        class DummyConfig:
            enabled = True
            jsonl_enabled = True
            jsonl_path = "/nonexistent/dir/file.jsonl"  # fails to write
            sampling_rate = 1.0
            payload_mode = "full"
            max_payload_chars = 10000
            langfuse_enabled = False

        with patch(
            "memtomem.observability.session_tracing.get_trace_config", return_value=DummyConfig()
        ):
            # Verify the context manager executes successfully and doesn't propagate file errors
            completed = False
            with trace_session("cmd", "evt"):
                completed = True
            assert completed is True


class TestCLIIntegration:
    @pytest.fixture
    def runner(self) -> CliRunner:
        return CliRunner()

    def test_session_start_traces_written(self, runner, tmp_path: Path, monkeypatch):
        # Redirect config and override path to test tracing
        jsonl_file = tmp_path / "traces.jsonl"

        class DummyConfig:
            enabled = True
            jsonl_enabled = True
            jsonl_path = jsonl_file
            sampling_rate = 1.0
            payload_mode = "full"
            max_payload_chars = 10000
            langfuse_enabled = False

        # Mock config loader to return enabled trace config
        monkeypatch.setattr(
            "memtomem.observability.session_tracing.get_trace_config",
            lambda *args, **kwargs: DummyConfig(),
        )

        # Mock CLI components to avoid DB initialization errors in this CLI unit test
        storage = MagicMock()
        storage.find_stale_active_sessions = AsyncMock(return_value=[])
        storage.get_session = AsyncMock(return_value=None)
        storage.create_session = AsyncMock(return_value=None)
        comp = MagicMock(storage=storage)

        @asynccontextmanager
        async def fake_cli_components():
            yield comp

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake_cli_components)

        result = runner.invoke(cli, ["session", "start", "--agent-id", "claude"])
        assert result.exit_code == 0

        # Verify JSONL trace is written
        assert jsonl_file.exists()
        lines = jsonl_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["command"] == "session_start"
        assert data["agent_id"] == "claude"
        assert "session_id" in data["payload"]
        assert data["payload"]["resumed"] is False

    def test_config_credential_redaction(self, runner, override_path: Path, monkeypatch):
        # Stub provider-dir discovery to [] so auto_discover migration doesn't trigger log warning
        import memtomem.config as _cfg

        monkeypatch.setattr(_cfg, "_canonical_provider_dirs", lambda: [])

        override_path.write_text(
            json.dumps(
                {
                    "indexing": {
                        "auto_discover": False,
                    },
                    "session_trace": {
                        "langfuse_secret_key": "my-secret-key-12345",
                    },
                }
            ),
            encoding="utf-8",
        )

        # Test config show JSON format masks the secret key
        result = runner.invoke(cli, ["config", "show", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["session_trace"]["langfuse_secret_key"] == "***"

        # Test config show table format masks the secret key
        result_table = runner.invoke(cli, ["config", "show"])
        assert result_table.exit_code == 0
        assert "my-secret-key-12345" not in result_table.output
        assert "langfuse_secret_key = ***" in result_table.output

        # Test config set output masks the secret key
        result_set = runner.invoke(
            cli, ["config", "set", "session_trace.langfuse_secret_key", "new-secret-999"]
        )
        assert result_set.exit_code == 0
        assert "new-secret-999" not in result_set.output
        assert "my-secret-key-12345" not in result_set.output
        assert "session_trace.langfuse_secret_key: *** -> ***" in result_set.output
