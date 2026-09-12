"""``mm config show`` and ``mm status`` report a rejected config section.

Against the real loader in an isolated HOME — mocking ``Mem2MemConfig`` and
``load_config_overrides``, as the older ``config show`` tests do, would skip
the very path under test (#2385 item 3).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem import config as _cfg
from memtomem.cli import cli

from .helpers import set_home

STALE_E5 = {
    "embedding": {
        "provider": "onnx",
        "model": "intfloat/multilingual-e5-small",
        "dimension": 1024,
    }
}


@pytest.fixture
def runner() -> CliRunner:
    """Keep stderr separate — the warning must not pollute JSON stdout."""
    return CliRunner()


@pytest.fixture
def rejecting_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    set_home(monkeypatch, tmp_path)
    for name in list(os.environ):
        if name.upper().startswith("MEMTOMEM_"):
            monkeypatch.delenv(name, raising=False)
    cfg = tmp_path / ".memtomem" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(STALE_E5), encoding="utf-8")
    return tmp_path


# The command's own diagnostic, not the loader's WARNING log — which also
# lands on stderr and also names the section and the reason. Asserting a
# substring the log carries too passes with the command's line deleted, so
# every assertion below keys on this wording, which only the CLI emits.
CLI_MARKER = "warning: config section [embedding] in "
CLI_TAIL = "that file's values for the section were ignored"


class TestConfigShowReportsRejectedSections:
    def test_json_stdout_stays_parseable_and_the_warning_goes_to_stderr(
        self, runner: CliRunner, rejecting_home: Path
    ) -> None:
        result = runner.invoke(cli, ["config", "show", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["embedding"]["provider"] == "none"
        assert CLI_MARKER in result.stderr
        assert CLI_TAIL in result.stderr
        assert "dimension=384" in result.stderr
        # stdout is the config document and nothing else.
        assert CLI_MARKER not in result.stdout
        assert "was rejected" not in result.stdout

    def test_table_output_carries_the_same_warning(
        self, runner: CliRunner, rejecting_home: Path
    ) -> None:
        result = runner.invoke(cli, ["config", "show"])

        assert result.exit_code == 0, result.output
        assert CLI_MARKER in result.stderr
        assert CLI_TAIL in result.stderr
        assert "dimension=384" in result.stderr
        assert CLI_MARKER not in result.stdout

    def test_one_line_per_rejected_section(self, runner: CliRunner, rejecting_home: Path) -> None:
        """Exactly one diagnostic for one rejected section — the loader's own
        log line must not be miscounted as a second."""
        result = runner.invoke(cli, ["config", "show", "--json"])

        assert result.stderr.count(CLI_MARKER) == 1

    def test_control_characters_in_a_diagnostic_cannot_forge_a_second_line(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The path and reason are text off disk that reach a terminal.

        A newline in either prints as an extra line that reads like a second
        warning, and an escape reaches the display intact. The diagnostic is
        injected rather than provoked through a filename: NTFS refuses a name
        containing these bytes, so a filesystem reproduction would only ever
        run on POSIX (see the companion test below), while the rendering this
        pins has to hold everywhere.
        """
        set_home(monkeypatch, tmp_path)
        for name in list(os.environ):
            if name.upper().startswith("MEMTOMEM_"):
                monkeypatch.delenv(name, raising=False)

        def _inject(config, **kwargs) -> None:
            config._load_diagnostics.append(
                _cfg.ConfigLoadDiagnostic(
                    section="embedding",
                    path="/x/10-a\nforged\x1b[2J.json",
                    error="bad\x1b[31m",
                    layer="config.d",
                )
            )

        monkeypatch.setattr(_cfg, "load_config_overrides", _inject)

        result = runner.invoke(cli, ["config", "show", "--json"])

        assert result.exit_code == 0, result.output
        cli_lines = [ln for ln in result.stderr.splitlines() if ln.startswith("warning: config")]
        assert len(cli_lines) == 1, f"the newline forged extra lines: {result.stderr!r}"
        assert "\x1b" not in cli_lines[0]
        assert "\\x1b" in cli_lines[0] and "\\x0a" in cli_lines[0]

    def test_config_show_and_the_status_report_spell_one_value_the_same_way(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two surfaces, one fragment name — an operator pastes both (#2410).

        Both neutralise control characters now, and two implementations would
        have done it in different alphabets: this command escaped code points
        while the status report's ``scrub_text`` escapes filesystem bytes, so a
        zero-width space in a fragment name would read ``\\x200b`` here against
        ``\\xe2\\x80\\x8b`` there. The ASCII-control assertions above cannot see
        that — every spelling agrees below ``U+0080``.
        """
        from memtomem.server.tools.status_config import StatusLine

        from memtomem.cli.config_cmd import _one_line

        hostile = "/x/zero\u200bwidth\x85.json"

        assert _one_line(hostile) == StatusLine("kv", value=hostile).value
        # Not vacuously equal because neither escaped anything.
        assert _one_line(hostile) != hostile
        assert "\\xe2\\x80\\x8b" in _one_line(hostile)

    @pytest.mark.skipif(
        os.name == "nt",
        reason="NTFS rejects a filename containing C0 control characters "
        "(OSError: [Errno 22]), so this reach does not exist on Windows",
    )
    def test_a_fragment_filename_really_can_carry_them(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reachability, on the platforms where the filename is legal.

        Without this the test above would be pinning an escape for input that
        nothing can produce.
        """
        set_home(monkeypatch, tmp_path)
        for name in list(os.environ):
            if name.upper().startswith("MEMTOMEM_"):
                monkeypatch.delenv(name, raising=False)
        fragments = tmp_path / ".memtomem" / "config.d"
        fragments.mkdir(parents=True, exist_ok=True)
        (fragments / "10-a\nforged\x1b[2J.json").write_text(json.dumps(STALE_E5), encoding="utf-8")

        result = runner.invoke(cli, ["config", "show", "--json"])

        assert result.exit_code == 0, result.output
        cli_lines = [ln for ln in result.stderr.splitlines() if ln.startswith("warning: config")]
        assert len(cli_lines) == 1
        assert "\x1b[2J" not in cli_lines[0]

    def test_json_and_format_json_agree_on_stdout(
        self, runner: CliRunner, rejecting_home: Path
    ) -> None:
        """The documented alias must not diverge once a warning exists —
        the existing parity test mocks the loader, so it cannot see this."""
        flag = runner.invoke(cli, ["config", "show", "--json"])
        fmt = runner.invoke(cli, ["config", "show", "--format", "json"])

        assert flag.exit_code == fmt.exit_code == 0
        assert flag.stdout == fmt.stdout

    def test_a_clean_config_prints_no_warning(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        set_home(monkeypatch, tmp_path)
        for name in list(os.environ):
            if name.upper().startswith("MEMTOMEM_"):
                monkeypatch.delenv(name, raising=False)
        cfg = tmp_path / ".memtomem" / "config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"search": {"default_top_k": 7}}), encoding="utf-8")

        result = runner.invoke(cli, ["config", "show", "--json"])

        assert result.exit_code == 0, result.output
        assert "warning: config section" not in result.stderr
        assert json.loads(result.stdout)["search"]["default_top_k"] == 7


class TestStatusReportsRejectedSections:
    async def test_collect_status_report_carries_the_warning(self, rejecting_home: Path) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from memtomem.config_signature import build_fresh_config
        from memtomem.server.context import AppContext
        from memtomem.server.tools.status_config import (
            collect_status_report,
            render_status_report,
        )

        config = build_fresh_config(migrate=False, strict_overrides=False)
        comp = SimpleNamespace(
            config=config,
            storage=SimpleNamespace(
                get_stats=AsyncMock(return_value={"total_chunks": 0, "total_sources": 0}),
                get_all_source_files=AsyncMock(return_value=[]),
                stored_embedding_info=None,
                embedding_mismatch=None,
            ),
            embedder=SimpleNamespace(),
        )

        data = await collect_status_report(AppContext.from_components(comp))

        rejected = [w for w in data["warnings"] if w["kind"] == "config_section_rejected"]
        assert len(rejected) == 1
        assert rejected[0]["section"] == "embedding"
        assert all(isinstance(value, str) for value in rejected[0].values())
        # The renderer treats dict values as the embedding provider/model
        # block, so a string-only entry must render without raising.
        assert "config_section_rejected" in render_status_report(data)
