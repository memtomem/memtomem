from __future__ import annotations

import builtins
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
    _redact_metadata,
    format_payload,
    format_propagated_metadata,
    sanitize_metadata_key,
    sanitize_metadata_value,
    trace_session,
)
from memtomem.cli import cli

from .helpers import set_home


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

    def test_nested_and_bare_token_redaction(self):
        payload = {
            "harmless_field": "some-value",
            "nested_dict": {
                "password": "mypassword",
                "normal": "ok",
            },
            "nested_list": [
                {"token": "some-token"},
                "just a string",
                "sk-123456789012345678901234567890",
            ],
            "json_string": '{"nested":{"api_key":"sk-abcdef"}}',
            "harmless_string_with_secret": "Here is a secret: sk-123456789012345678901234567890",
            "cli_args": "mm init --api-key my-api-key-value",
        }
        formatted = format_payload(payload, "redacted", 5000)
        assert formatted["harmless_field"] == "some-value"
        assert formatted["nested_dict"]["password"] == "***"
        assert formatted["nested_dict"]["normal"] == "ok"
        assert formatted["nested_list"][0]["token"] == "***"
        assert formatted["nested_list"][1] == "just a string"
        assert formatted["nested_list"][2] == "***"

        json_redacted = json.loads(formatted["json_string"])
        assert json_redacted["nested"]["api_key"] == "***"

        assert "sk-" not in formatted["harmless_string_with_secret"]
        assert "***" in formatted["harmless_string_with_secret"]
        assert "my-api-key-value" not in formatted["cli_args"]
        assert "***" in formatted["cli_args"]

    def test_redact_metadata_modes(self):
        meta = {
            "command": "deploy --api-key sk-SECRETABC123456",
            # a bare secret under a secret-named KEY (no inline marker) must be
            # scrubbed by the dict-key policy — exactly like the payload path.
            "api_key": "plain-secret-value",
            "harmless": "ok",
        }

        # full mode is the explicit verbatim opt-in — returned unchanged.
        assert _redact_metadata(meta, "full") is meta

        # redacted and the default metadata mode both scrub secret-bearing
        # values while leaving harmless ones intact.
        for mode in ("redacted", "metadata"):
            out = _redact_metadata(meta, mode)
            assert "sk-SECRETABC123456" not in out["command"]
            assert "***" in out["command"]
            assert out["api_key"] == "***"
            assert "plain-secret-value" not in json.dumps(out)
            assert out["harmless"] == "ok"
            # the source dict must not be mutated in place.
            assert meta["command"] == "deploy --api-key sk-SECRETABC123456"
            assert meta["api_key"] == "plain-secret-value"

        # empty metadata is a no-op regardless of mode.
        assert _redact_metadata({}, "redacted") == {}

    def test_redact_metadata_non_string_object_value(self):
        # A value whose __str__ leaks a secret must be scrubbed: the exporters
        # stringify it (json.dumps(default=str), sanitize_metadata_value), so
        # leaving the object verbatim would leak the secret downstream.
        class Secretish:
            def __str__(self):
                return "wrapped --api-key sk-SECRETABC123456"

        out = _redact_metadata({"obj": Secretish()}, "redacted")
        assert "sk-SECRETABC123456" not in str(out["obj"])
        assert "***" in str(out["obj"])

        # JSON scalars must pass through untouched — no spurious stringification.
        assert _redact_metadata({"n": 5, "f": 1.5, "b": True, "z": None}, "redacted") == {
            "n": 5,
            "f": 1.5,
            "b": True,
            "z": None,
        }

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

    def test_langfuse_failure_isolation(self):
        class DummyConfig:
            enabled = True
            jsonl_enabled = False
            sampling_rate = 1.0
            payload_mode = "full"
            max_payload_chars = 10000
            langfuse_enabled = True
            langfuse_public_key = "pk"
            langfuse_secret_key = "sk"

        mock_span = MagicMock()
        mock_span.__enter__.return_value = mock_span
        mock_span.update.side_effect = Exception("Telemetry update failed")

        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value = mock_span

        with (
            patch(
                "memtomem.observability.session_tracing.get_trace_config",
                return_value=DummyConfig(),
            ),
            patch(
                "memtomem.observability.session_tracing.get_langfuse_client",
                return_value=mock_client,
            ),
        ):
            completed = False
            with trace_session("cmd", "evt"):
                completed = True
            assert completed is True

            with pytest.raises(ValueError, match="command failed"):
                with trace_session("cmd", "evt"):
                    raise ValueError("command failed")

            assert mock_span.update.called

    def test_langfuse_propagate_import_failure_isolation(self, monkeypatch):
        class DummyConfig:
            enabled = True
            jsonl_enabled = False
            sampling_rate = 1.0
            payload_mode = "full"
            max_payload_chars = 10000
            langfuse_enabled = True
            langfuse_public_key = "pk"
            langfuse_secret_key = "sk"

        mock_span = MagicMock()
        mock_span.__enter__.return_value = mock_span

        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value = mock_span

        original_import = builtins.__import__

        def failing_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "langfuse" and "propagate_attributes" in fromlist:
                raise RuntimeError("propagate import failed")
            return original_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", failing_import)

        with (
            patch(
                "memtomem.observability.session_tracing.get_trace_config",
                return_value=DummyConfig(),
            ),
            patch(
                "memtomem.observability.session_tracing.get_langfuse_client",
                return_value=mock_client,
            ),
        ):
            completed = False
            with trace_session("cmd", "evt"):
                completed = True
            assert completed is True

            with pytest.raises(ValueError, match="command failed"):
                with trace_session("cmd", "evt"):
                    raise ValueError("command failed")


class TestCLIIntegration:
    @pytest.fixture
    def runner(self) -> CliRunner:
        return CliRunner()

    def test_session_start_traces_written(self, runner, tmp_path: Path, monkeypatch):
        # Isolate HOME so `mm session start` → _write_current_session() writes
        # to tmp_path/.memtomem/.current_session, not the real
        # ~/.memtomem/.current_session (PermissionError in protected-home CI,
        # and overwrites a developer's live session otherwise).
        home = tmp_path / "home"
        home.mkdir()
        set_home(monkeypatch, home)

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

    def _trace_with_secret_metadata(self, tmp_path, monkeypatch, mode: str) -> dict:
        """Run one trace whose metadata carries a secret, return the JSONL row."""
        jsonl_file = tmp_path / "traces.jsonl"

        class DummyConfig:
            enabled = True
            jsonl_enabled = True
            jsonl_path = jsonl_file
            sampling_rate = 1.0
            payload_mode = mode
            max_payload_chars = 10000
            langfuse_enabled = False

        monkeypatch.setattr(
            "memtomem.observability.session_tracing.get_trace_config",
            lambda *args, **kwargs: DummyConfig(),
        )
        secret_cmd = "deploy --api-key sk-SECRETABC123456"
        with trace_session("session_wrap", "session_wrap", agent_id="claude") as ctx:
            ctx["metadata"]["command"] = secret_cmd
            ctx["payload"]["command"] = secret_cmd
        return json.loads(jsonl_file.read_text(encoding="utf-8").strip())

    def test_metadata_redacted_in_jsonl(self, runner, tmp_path: Path, monkeypatch):
        # The command metadata leaks the secret pre-fix; redacted mode must
        # scrub it everywhere, not just in the payload (P1).
        data = self._trace_with_secret_metadata(tmp_path, monkeypatch, "redacted")
        blob = json.dumps(data)
        assert "sk-SECRETABC123456" not in blob
        assert "***" in data["metadata"]["command"]

    def test_metadata_verbatim_in_full_mode(self, runner, tmp_path: Path, monkeypatch):
        # full mode is an explicit opt-in to verbatim content for both payload
        # and metadata — no redaction.
        data = self._trace_with_secret_metadata(tmp_path, monkeypatch, "full")
        assert "sk-SECRETABC123456" in data["metadata"]["command"]

    def test_metadata_redacted_in_langfuse_sinks(self, tmp_path: Path, monkeypatch):
        # The JSONL tests force langfuse_enabled=False, so they only prove the
        # JSONL sink. Cover the three Langfuse sinks too: initial + final
        # propagate_attributes(metadata=...) and span.update(output=...). A
        # regression that dropped redaction on any of these would otherwise pass.
        import sys
        import types
        from contextlib import contextmanager

        captured_propagate: list = []
        captured_output: list = []

        class FakeSpan:
            def update(self, **kwargs):
                captured_output.append(kwargs.get("output"))

        class FakeObs:
            def __enter__(self):
                return FakeSpan()

            def __exit__(self, *exc):
                return False

        class FakeClient:
            def start_as_current_observation(self, **kwargs):
                return FakeObs()

            def flush(self):
                pass

        @contextmanager
        def fake_propagate_attributes(*, session_id, user_id, metadata):
            captured_propagate.append(metadata)
            yield

        fake_langfuse = types.ModuleType("langfuse")
        fake_langfuse.propagate_attributes = fake_propagate_attributes
        monkeypatch.setitem(sys.modules, "langfuse", fake_langfuse)
        monkeypatch.setattr(
            "memtomem.observability.session_tracing.get_langfuse_client",
            lambda *args, **kwargs: FakeClient(),
        )

        class DummyConfig:
            enabled = True
            jsonl_enabled = False
            langfuse_enabled = True
            sampling_rate = 1.0
            payload_mode = "redacted"
            max_payload_chars = 10000

        monkeypatch.setattr(
            "memtomem.observability.session_tracing.get_trace_config",
            lambda *args, **kwargs: DummyConfig(),
        )

        secret = "deploy --api-key sk-SECRETABC123456"
        # Pass the secret via initial_metadata so the INITIAL propagate_attributes
        # (which fires before the yield) sees it too — otherwise that sink only
        # ever sees {} and a redaction regression there would go uncaught.
        with trace_session(
            "session_wrap",
            "session_wrap",
            agent_id="claude",
            initial_metadata={"command": secret},
        ):
            pass

        # Both propagate sinks (initial + final) and the span output sink fired,
        # and none may carry the raw secret.
        assert len(captured_propagate) >= 2
        assert captured_output
        blob = json.dumps({"propagate": captured_propagate, "output": captured_output}, default=str)
        assert "sk-SECRETABC123456" not in blob
        # the INITIAL propagate sink (index 0) must be redacted, not just the final
        assert "***" in str(captured_propagate[0].get("command", ""))
        assert "***" in str(captured_propagate[-1].get("command", ""))
        assert "***" in captured_output[-1]["metadata"]["command"]

    def test_jsonl_redaction_failure_does_not_escape(self, tmp_path: Path, monkeypatch):
        # A pathological metadata value (here a failing __str__) must not let a
        # telemetry failure escape _write_local_jsonl and override the wrapped
        # command result — redaction now runs inside the JSONL write guard (P2).
        jsonl_file = tmp_path / "traces.jsonl"

        class Boom:
            def __str__(self):
                raise RuntimeError("boom")

        class DummyConfig:
            enabled = True
            jsonl_enabled = True
            jsonl_path = jsonl_file
            sampling_rate = 1.0
            payload_mode = "redacted"
            max_payload_chars = 10000
            langfuse_enabled = False

        monkeypatch.setattr(
            "memtomem.observability.session_tracing.get_trace_config",
            lambda *args, **kwargs: DummyConfig(),
        )

        # Reaching the assertions proves no telemetry exception leaked out of the
        # context manager during cleanup.
        with trace_session("session_wrap", "session_wrap", agent_id="claude") as ctx:
            ctx["metadata"]["bad"] = Boom()

        # The row was dropped (redaction raised, was caught + logged) rather than
        # written with the unredacted value.
        assert not jsonl_file.exists() or jsonl_file.read_text(encoding="utf-8") == ""

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


class TestConfigSaveValidationAndRollback:
    @pytest.fixture
    def runner(self) -> CliRunner:
        return CliRunner()

    def test_save_config_validation_rejection(self, override_path: Path):
        cfg = Mem2MemConfig()
        cfg.session_trace.enabled = True
        cfg.session_trace.langfuse_enabled = True
        cfg.session_trace.langfuse_public_key = ""
        cfg.session_trace.langfuse_secret_key = ""

        from memtomem.config import save_config_overrides

        with pytest.raises(
            ValueError, match="requires both langfuse_public_key and langfuse_secret_key"
        ):
            save_config_overrides(cfg)

    def test_cli_config_set_validation_error(self, runner, override_path: Path, monkeypatch):
        import memtomem.config as _cfg

        monkeypatch.setattr(_cfg, "_canonical_provider_dirs", lambda: [])

        override_path.write_text("{}", encoding="utf-8")

        result = runner.invoke(cli, ["config", "set", "session_trace.enabled", "true"])
        assert result.exit_code == 0

        result = runner.invoke(cli, ["config", "set", "session_trace.langfuse_enabled", "true"])
        assert result.exit_code != 0
        assert "requires both langfuse_public_key and langfuse_secret_key" in result.output

        with open(override_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved.get("session_trace", {}).get("langfuse_enabled") is not True

    @pytest.mark.anyio
    async def test_web_api_patch_rollback(self, monkeypatch):
        app_mock = MagicMock()
        app_mock.state.config = Mem2MemConfig()
        app_mock.state.config.session_trace.enabled = True
        app_mock.state.config.session_trace.langfuse_enabled = False

        request_mock = MagicMock()
        request_mock.app = app_mock

        from fastapi import HTTPException
        from memtomem.web.routes.system import ConfigPatchRequest, patch_config

        req = ConfigPatchRequest(session_trace={"langfuse_enabled": True})

        def mock_build_fresh():
            cfg = Mem2MemConfig()
            cfg.session_trace.enabled = True
            cfg.session_trace.langfuse_enabled = False
            return cfg

        from memtomem.web import hot_reload as _hr

        monkeypatch.setattr(_hr, "_build_fresh_config", mock_build_fresh)
        monkeypatch.setattr(_hr, "current_signature", lambda: ())
        monkeypatch.setattr(_hr, "_set_last_signature", lambda app, sig: None)
        monkeypatch.setattr(_hr, "commit_writer_signature", lambda app: None)

        with pytest.raises(HTTPException) as excinfo:
            await patch_config(
                request=request_mock,
                req=req,
                persist=True,
                storage=MagicMock(),
                search_pipeline=MagicMock(),
            )
        assert excinfo.value.status_code == 400
        assert "requires both langfuse_public_key and langfuse_secret_key" in str(
            excinfo.value.detail
        )

        assert app_mock.state.config.session_trace.langfuse_enabled is False

    @pytest.mark.anyio
    async def test_mcp_config_rollback(self, monkeypatch, tmp_path):
        # Isolate HOME: on a persist failure the MCP rollback rebuilds a fresh
        # config via load_config_d + load_config_overrides, both of which read
        # ~/.memtomem (config.d/ and config.json). Without isolation a developer
        # whose real config.json sets session_trace.langfuse_enabled=true leaks
        # into the rollback baseline and this assertion flips to True (#1249).
        set_home(monkeypatch, tmp_path)
        app_mock = MagicMock()
        app_mock.config = Mem2MemConfig()
        app_mock.config.session_trace.enabled = True
        app_mock.config.session_trace.langfuse_enabled = False

        from memtomem.server.tools import status_config

        monkeypatch.setattr(status_config, "_get_app_initialized", AsyncMock(return_value=app_mock))

        from memtomem.server.tools.status_config import mem_config

        res = await mem_config(
            key="session_trace.langfuse_enabled", value="true", persist=True, ctx=MagicMock()
        )

        assert "Failed to persist config" in res
        assert "requires both langfuse_public_key and langfuse_secret_key" in res

        assert app_mock.config.session_trace.langfuse_enabled is False


class TestConfigMaskingAndSecretsSecurity:
    @pytest.mark.anyio
    async def test_mcp_config_secret_masking(self, monkeypatch):
        app_mock = MagicMock()
        app_mock.config = Mem2MemConfig()
        app_mock.config.embedding.api_key = "my-openai-api-key"
        app_mock.config.session_trace.langfuse_secret_key = "my-langfuse-secret"

        from memtomem.server.tools import status_config

        monkeypatch.setattr(status_config, "_get_app_initialized", AsyncMock(return_value=app_mock))

        from memtomem.server.tools.status_config import mem_config

        # Test full config dump masks secrets
        full_res = await mem_config(ctx=MagicMock())
        assert "my-openai-api-key" not in full_res
        assert "my-langfuse-secret" not in full_res
        assert '"api_key": "***"' in full_res
        assert '"langfuse_secret_key": "***"' in full_res

        # Test single key read masks secrets
        key_res1 = await mem_config(key="session_trace.langfuse_secret_key", ctx=MagicMock())
        assert "my-langfuse-secret" not in key_res1
        assert key_res1 == "session_trace.langfuse_secret_key = ***"

        key_res2 = await mem_config(key="embedding.api_key", ctx=MagicMock())
        assert "my-openai-api-key" not in key_res2
        assert key_res2 == "embedding.api_key = ***"

        # Test setting config masks the return confirmation value
        set_res = await mem_config(
            key="session_trace.langfuse_secret_key",
            value="new-secret",
            persist=False,
            ctx=MagicMock(),
        )
        assert "new-secret" not in set_res
        assert (
            set_res
            == "Set session_trace.langfuse_secret_key = '***' (runtime only — not persisted)"
        )

    @pytest.mark.anyio
    async def test_web_patch_config_secret_masking(self, monkeypatch):
        app_mock = MagicMock()
        app_mock.state.config = Mem2MemConfig()
        app_mock.state.config.session_trace.langfuse_secret_key = "old-secret"

        request_mock = MagicMock()
        request_mock.app = app_mock

        from memtomem.web.routes.system import ConfigPatchRequest, patch_config

        req = ConfigPatchRequest(session_trace={"langfuse_secret_key": "new-secret"})

        # Mock reload_if_stale to be noop
        from memtomem.web import hot_reload as _hr

        monkeypatch.setattr(_hr, "reload_if_stale", AsyncMock(return_value=False))
        # Mock save_config_overrides to be noop
        monkeypatch.setattr("memtomem.web.routes.system.save_config_overrides", lambda cfg: None)

        res = await patch_config(
            request=request_mock,
            req=req,
            persist=True,
            storage=MagicMock(),
            search_pipeline=MagicMock(),
        )

        # Verify old_value and new_value are masked in response
        assert len(res.applied) == 1
        assert res.applied[0].field == "session_trace.langfuse_secret_key"
        assert res.applied[0].old_value == "***"
        assert res.applied[0].new_value == "***"
