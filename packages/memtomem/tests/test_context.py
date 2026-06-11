"""Tests for agent context management module."""

from pathlib import Path

import pytest

from memtomem.context.parser import (
    iter_markdown_sections,
    parse_context,
    sections_to_markdown,
    split_preamble,
)
from memtomem.context.detector import detect_agent_files
from memtomem.context.generator import (
    GENERATORS,
    generate_for_agent,
    generate_all,
    extract_sections_from_agent_file,
    preamble_source,
)


SAMPLE_CONTEXT = """# Project Context

## Project
- Name: test-project
- Language: Python 3.12+

## Commands
- Build: pip install -e .
- Test: pytest
- Lint: ruff check .

## Architecture
Monorepo with src/ and tests/.

## Rules
- line-length 100
- pytest-asyncio auto mode

## Style
- English for code
- No emojis
"""


class TestParser:
    def test_parse_sections(self, tmp_path):
        ctx = tmp_path / "context.md"
        ctx.write_text(SAMPLE_CONTEXT, encoding="utf-8")
        sections = parse_context(ctx)

        assert "Project" in sections
        assert "Commands" in sections
        assert "Architecture" in sections
        assert "Rules" in sections
        assert "Style" in sections
        assert "test-project" in sections["Project"]

    def test_parse_nonexistent(self, tmp_path):
        result = parse_context(tmp_path / "nope.md")
        assert result == {}

    def test_roundtrip(self, tmp_path):
        ctx = tmp_path / "context.md"
        ctx.write_text(SAMPLE_CONTEXT, encoding="utf-8")
        sections = parse_context(ctx)
        output = sections_to_markdown(sections)
        reparsed = parse_context(tmp_path / "out.md")
        (tmp_path / "out.md").write_text(output, encoding="utf-8")
        reparsed = parse_context(tmp_path / "out.md")
        assert reparsed.keys() == sections.keys()


class TestDetector:
    def test_detect_claude(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# CLAUDE.md", encoding="utf-8")
        files = detect_agent_files(tmp_path)
        assert len(files) == 1
        assert files[0].agent == "claude"

    def test_detect_multiple(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# CLAUDE.md", encoding="utf-8")
        (tmp_path / ".cursorrules").write_text("rules", encoding="utf-8")
        (tmp_path / "GEMINI.md").write_text("# GEMINI.md", encoding="utf-8")
        files = detect_agent_files(tmp_path)
        agents = {f.agent for f in files}
        assert agents == {"claude", "cursor", "gemini"}

    def test_detect_empty(self, tmp_path):
        files = detect_agent_files(tmp_path)
        assert files == []

    def test_detect_codex(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# AGENTS.md", encoding="utf-8")
        files = detect_agent_files(tmp_path)
        assert files[0].agent == "codex"

    def test_detect_ignores_kimi_runtime_config(self, tmp_path):
        kimi_dir = tmp_path / ".kimi"
        kimi_dir.mkdir()
        (kimi_dir / "config.toml").write_text("[hooks]\n", encoding="utf-8")
        files = detect_agent_files(tmp_path)
        assert files == []

    def test_detect_copilot(self, tmp_path):
        gh = tmp_path / ".github"
        gh.mkdir()
        (gh / "copilot-instructions.md").write_text("instructions", encoding="utf-8")
        files = detect_agent_files(tmp_path)
        assert files[0].agent == "copilot"


class TestGenerator:
    def _sections(self):
        return {
            "Project": "- Name: test\n- Language: Python",
            "Commands": "- Test: pytest",
            "Architecture": "Simple layout.",
            "Rules": "- line-length 100",
            "Style": "- English only",
        }

    def test_all_generators_registered(self):
        assert "claude" in GENERATORS
        assert "cursor" in GENERATORS
        assert "gemini" in GENERATORS
        assert "codex" in GENERATORS
        assert "copilot" in GENERATORS

    def test_claude_generate(self):
        content = generate_for_agent("claude", self._sections())
        assert "CLAUDE.md" in content
        assert "Claude Code" in content
        assert "pytest" in content

    def test_cursor_generate(self):
        content = generate_for_agent("cursor", self._sections())
        assert "line-length 100" in content
        assert "## Style" in content
        assert content.index("## Rules") < content.index("## Style")
        assert "pytest" in content

    def test_gemini_generate(self):
        content = generate_for_agent("gemini", self._sections())
        assert "GEMINI.md" in content
        assert "Gemini CLI" in content

    def test_codex_generate(self):
        content = generate_for_agent("codex", self._sections())
        assert "AGENTS.md" in content
        assert "line-length 100" in content
        assert "## Style" in content

    def test_copilot_generate(self):
        content = generate_for_agent("copilot", self._sections())
        assert "line-length 100" in content
        assert "## Style" in content

    def test_generate_all(self):
        result = generate_all(self._sections())
        assert len(result) == 5
        for name, content in result.items():
            assert len(content) > 0

    @pytest.mark.parametrize("agent", sorted(GENERATORS))
    def test_every_generator_emits_all_canonical_section_bodies(self, agent):
        """No generator may silently drop a canonical section (#1247 id 39).

        Canonical section names are in _RESERVED_SECTION_KEYS, so the
        unknown-section passthrough skips them — a generator missing a branch
        for one (copilot had no Architecture branch) makes that content vanish
        from its output with no warning.
        """
        sections = self._sections()
        content = generate_for_agent(agent, sections)
        for body in sections.values():
            assert body in content

    @pytest.mark.parametrize("agent", ["claude", "cursor", "gemini", "codex", "copilot"])
    def test_generate_preserves_unknown_sections(self, agent):
        sections = {
            "Project": "Intro line.",
            "Deployment": "Ship it.",
            "Rules": "- be terse",
        }

        content = generate_for_agent(agent, sections)

        assert "## Deployment" in content
        assert "Ship it." in content
        reparsed = extract_sections_from_agent_file(content, source=agent)
        assert reparsed["Deployment"] == "Ship it."

    def test_generate_preserves_case_variant_canonical_headings_as_unknown(self):
        content = generate_for_agent(
            "cursor",
            {
                "Project": "Intro line.",
                "rules": "- lower-case hand-authored rules",
            },
        )

        assert "## rules" in content
        assert "- lower-case hand-authored rules" in content

    def test_generate_does_not_emit_other_agent_overrides_as_unknown(self):
        content = generate_for_agent(
            "claude",
            {
                "Project": "Intro line.",
                "Cursor": "Cursor-only override.",
                "Deployment": "Ship it.",
            },
        )

        assert "Cursor-only override." not in content
        assert "## Cursor" not in content
        assert "## Deployment" in content

    @pytest.mark.parametrize("heading", ["Claude-Specific", "claude-specific"])
    def test_generate_does_not_emit_literal_agent_specific_headings_as_unknown(self, heading):
        content = generate_for_agent(
            "cursor",
            {
                "Project": "Intro line.",
                heading: "Claude-only override.",
                "Deployment": "Ship it.",
            },
        )

        assert "Claude-only override." not in content
        assert heading not in content
        assert "## Deployment" in content

    def test_unknown_agent_raises(self):
        with pytest.raises(KeyError):
            generate_for_agent("unknown", self._sections())


class TestExtractFromAgent:
    def test_extract_from_claude(self):
        content = """# CLAUDE.md

## What is this project?

A test project.

## Build & Development Commands

- Test: pytest

## Architecture

Simple.

## Coding Rules

- No magic numbers
"""
        sections = extract_sections_from_agent_file(content)
        assert "Project" in sections
        assert "Commands" in sections
        assert "Architecture" in sections
        assert "Rules" in sections

    def test_extract_preserves_unknown_headings(self):
        content = """## Custom Section

Some content here.
"""
        sections = extract_sections_from_agent_file(content)
        assert "Custom Section" in sections


class TestSpecificSectionRoundTrip:
    """`## <Agent>-Specific` sections must survive extract → generate.

    Regression: prior to alias addition, `extract_sections_from_agent_file`
    stored `## Claude-Specific` content under the literal key
    `Claude-Specific`, but the generator looks for the canonical key
    `Claude` — so the override section was silently dropped on round-trip.
    """

    @pytest.mark.parametrize(
        "agent,heading",
        [
            ("claude", "Claude-Specific"),
            ("cursor", "Cursor-Specific"),
            ("gemini", "Gemini-Specific"),
            ("codex", "Codex-Specific"),
            ("copilot", "Copilot-Specific"),
        ],
    )
    def test_specific_section_roundtrip(self, agent, heading):
        marker = "this content must survive round-trip"
        original = f"""# AGENT_FILE

## What is this project?

- Name: foo

## {heading}

{marker}
"""
        sections = extract_sections_from_agent_file(original)
        # Canonical key is the agent name with capital first letter.
        canonical = heading.split("-", 1)[0]
        assert canonical in sections
        assert marker in sections[canonical]

        regenerated = generate_for_agent(agent, sections)
        assert heading in regenerated
        assert marker in regenerated


class TestCanonicalAgentSpecificAlias:
    """Canonical `## <Agent>-Specific` headings must reach the right generator.

    Sibling of TestSpecificSectionRoundTrip, which covers the reverse-import
    leg (extract_sections_from_agent_file). This is the canonical-parse leg
    (#1247 id 38): parse_context kept the verbatim `Claude-Specific` key, the
    generators look up the short key (`Claude`), and _append_unknown_sections
    suppresses `<agent>-specific` headings — so the section was silently
    dropped from every generated file, including that agent's own. Generated
    files *display* `## Claude-Specific`, so canonical authors copy it.
    """

    @pytest.mark.parametrize(
        "heading,canonical",
        [
            ("Claude-Specific", "Claude"),
            ("Cursor-Specific", "Cursor"),
            ("Gemini-Specific", "Gemini"),
            ("Codex-Specific", "Codex"),
            ("Copilot-Specific", "Copilot"),
        ],
    )
    def test_parse_context_folds_agent_specific_heading(self, tmp_path, heading, canonical):
        ctx = tmp_path / "context.md"
        ctx.write_text(f"# Project Context\n\n## {heading}\n\noverride body\n", encoding="utf-8")
        sections = parse_context(ctx)
        assert canonical in sections
        assert heading not in sections
        assert sections[canonical] == "override body"

    def test_parse_context_folds_case_variants(self, tmp_path):
        ctx = tmp_path / "context.md"
        ctx.write_text("## CLAUDE-SPECIFIC\n\nshout body\n", encoding="utf-8")
        assert parse_context(ctx)["Claude"] == "shout body"

    def test_parse_context_merges_short_key_and_specific_heading(self, tmp_path):
        ctx = tmp_path / "context.md"
        ctx.write_text(
            "## Claude\n\nfirst body\n\n## Claude-Specific\n\nsecond body\n",
            encoding="utf-8",
        )
        sections = parse_context(ctx)
        assert sections["Claude"] == "first body\n\nsecond body"
        assert "Claude-Specific" not in sections

    def test_parse_context_leaves_non_agent_specific_headings_unknown(self, tmp_path):
        # The alias map must not over-match lookalike headings.
        ctx = tmp_path / "context.md"
        ctx.write_text("## Claudette-Specific\n\nnot an agent\n", encoding="utf-8")
        sections = parse_context(ctx)
        assert sections == {"Claudette-Specific": "not an agent"}

    def test_canonical_specific_section_reaches_own_agent_only(self, tmp_path):
        ctx = tmp_path / "context.md"
        ctx.write_text(
            "# Project Context\n\n## Project\n\nIntro.\n\n"
            "## Claude-Specific\n\nclaude override body\n",
            encoding="utf-8",
        )
        sections = parse_context(ctx)
        claude_out = generate_for_agent("claude", sections)
        gemini_out = generate_for_agent("gemini", sections)
        assert "## Claude-Specific" in claude_out
        assert "claude override body" in claude_out
        assert "claude override body" not in gemini_out


class TestParserHardening:
    """Round-trip data-loss guards for the section parser (#1123 B1)."""

    def test_fenced_code_hashes_not_treated_as_headings(self, tmp_path):
        """`##` inside a fenced code block must not split the section (B1-1)."""
        ctx = tmp_path / "context.md"
        ctx.write_text(
            "# Project Context\n\n"
            "## Architecture\n\n"
            "Run the example:\n\n"
            "```python\n"
            "## This looks like a heading but it is code\n"
            "value = 1\n"
            "```\n\n"
            "Trailing prose under Architecture.\n\n"
            "## Rules\n\n"
            "- line-length 100\n",
            encoding="utf-8",
        )
        sections = parse_context(ctx)

        # No spurious section minted from the in-code `##` line.
        assert set(sections) == {"Architecture", "Rules"}
        # Both the code block and the prose after it stay under Architecture.
        assert "## This looks like a heading but it is code" in sections["Architecture"]
        assert "Trailing prose under Architecture." in sections["Architecture"]
        assert sections["Rules"] == "- line-length 100"

    def test_tilde_fence_also_guarded(self):
        """`~~~` fences are guarded the same as backtick fences (B1-1)."""
        text = "## Architecture\n~~~\n## not a heading\n~~~\nafter\n## Rules\n- r\n"
        names = [name for name, _ in iter_markdown_sections(text)]
        assert names == ["Architecture", "Rules"]

    def test_nested_different_marker_fence_does_not_close_early(self):
        """A nested fence of the *other* marker must not close the block (#1132).

        A backtick-fenced markdown sample that itself shows a ``~~~`` snippet
        (and vice versa) only closes on a matching-marker fence. The inner
        marker must stay body so an in-sample ``##`` is not minted as a real
        section, splitting or dropping content on round-trip. This pins the
        opener-tracking fix that replaced the naive single-flag toggle.
        """
        backtick_outer = (
            "## Commands\n"
            "````markdown\n"  # opener: backtick run of 4
            "~~~\n"  # inner tilde fence — must NOT close the backtick block
            "## inside the sample, not a heading\n"
            "~~~\n"  # inner tilde close — still inside the backtick block
            "````\n"  # real close: matching backtick marker
            "trailing prose under Commands\n"
            "## Architecture\n"
            "- a\n"
        )
        sections = dict(iter_markdown_sections(backtick_outer))
        assert list(sections) == ["Commands", "Architecture"]
        assert "## inside the sample, not a heading" in sections["Commands"]
        assert "trailing prose under Commands" in sections["Commands"]

        # Reverse: tilde-fenced block showing a backtick snippet.
        tilde_outer = (
            "## Commands\n"
            "~~~~markdown\n"  # opener: tilde run of 4
            "```\n"  # inner backtick fence — must NOT close the tilde block
            "## inside the sample, not a heading\n"
            "```\n"
            "~~~~\n"  # real close: matching tilde marker
            "trailing prose under Commands\n"
            "## Architecture\n"
            "- a\n"
        )
        sections = dict(iter_markdown_sections(tilde_outer))
        assert list(sections) == ["Commands", "Architecture"]
        assert "## inside the sample, not a heading" in sections["Commands"]
        assert "trailing prose under Commands" in sections["Commands"]

    def test_duplicate_headings_merge_not_overwrite(self, tmp_path):
        """A repeated `## Name` must concatenate, not drop the first (B1-2)."""
        ctx = tmp_path / "context.md"
        ctx.write_text(
            "## Project\nfirst body line\n## Project\nsecond body line\n",
            encoding="utf-8",
        )
        sections = parse_context(ctx)

        assert list(sections) == ["Project"]
        assert "first body line" in sections["Project"]
        assert "second body line" in sections["Project"]

    def test_aliased_duplicate_headings_merge_on_extract(self):
        """Two headings aliasing to one canonical key must merge (B1-2)."""
        content = "## Rules\n- from rules heading\n## Coding Rules\n- from coding-rules heading\n"
        sections = extract_sections_from_agent_file(content)

        assert "from rules heading" in sections["Rules"]
        assert "from coding-rules heading" in sections["Rules"]

    def test_whitespace_only_heading_not_a_section(self, tmp_path):
        """`##` with no name must not create an empty-string key (B1-3)."""
        ctx = tmp_path / "context.md"
        ctx.write_text(
            "## Project\nreal content\n##   \norphan content\n",
            encoding="utf-8",
        )
        sections = parse_context(ctx)

        assert "" not in sections
        assert list(sections) == ["Project"]
        # The malformed heading line and its body fold into the open section.
        assert "orphan content" in sections["Project"]

    def test_sample_context_still_round_trips(self, tmp_path):
        """The happy-path round-trip is unchanged by the hardening."""
        ctx = tmp_path / "context.md"
        ctx.write_text(SAMPLE_CONTEXT, encoding="utf-8")
        sections = parse_context(ctx)
        out = tmp_path / "out.md"
        out.write_text(sections_to_markdown(sections), encoding="utf-8")
        reparsed = parse_context(out)
        assert reparsed == sections


class TestSplitPreamble:
    """`split_preamble` finds the first real heading, fence/whitespace aware."""

    def test_no_heading_is_all_preamble(self):
        preamble, rest = split_preamble("just prose\nmore prose")
        assert preamble == "just prose\nmore prose"
        assert rest == ""

    def test_basic_split(self):
        preamble, rest = split_preamble("intro line\n\n## A\n\nbody")
        assert "intro line" in preamble
        assert rest.startswith("## A")
        assert "body" in rest

    def test_fence_in_preamble_is_not_a_boundary(self):
        # A ``##``-lookalike inside a fenced code block must NOT end the
        # preamble early (B1-1 fence awareness, shared with the iterator).
        text = "intro\n```\n## fake heading\n```\n\n## Real\n\nbody"
        preamble, rest = split_preamble(text)
        assert "## fake heading" in preamble
        assert rest.startswith("## Real")

    def test_whitespace_only_heading_is_not_a_boundary(self):
        # ``##   `` is not a heading (B1-3); the first real boundary is ## Real.
        preamble, rest = split_preamble("intro\n##   \n\n## Real\nbody")
        assert "##   " in preamble
        assert rest.startswith("## Real")


class TestPreambleReverseImport:
    """Source-aware reverse-import captures leading project prose (#1147 B1-3)."""

    @pytest.mark.parametrize("source", ["claude", "gemini", "codex", "cursor", "copilot"])
    def test_generate_import_generate_is_idempotent(self, source):
        # No Rules/Style here so the B1-4 merge lossiness (a separate item)
        # does not confound the B1-3 round-trip.
        original = {"Project": "Proj prose line.", "Commands": "- run: x"}
        gen = generate_for_agent(source, original)
        result = extract_sections_from_agent_file(gen, source=source)

        # The project prose survives reverse-import...
        assert "Project" in result
        assert "Proj prose line." in result["Project"]
        # ...and no generated boilerplate leaks into the captured sections.
        for value in result.values():
            assert "# Project Context" not in value
            assert "# CLAUDE.md" not in value
            assert "# GEMINI.md" not in value
            assert "# AGENTS.md" not in value
            assert "This file provides guidance" not in value
        # Full round-trip is stable.
        assert generate_for_agent(source, result) == gen

    def test_cursor_user_prose_is_captured(self):
        # The exact bug: a hand-authored .cursorrules has leading prose with
        # no H1 — previously dropped, now mapped to Project.
        content = "We build a CLI in Rust.\n\n## Rules\n\n- be fast\n"
        result = extract_sections_from_agent_file(content, source="cursor")
        assert result["Project"] == "We build a CLI in Rust."
        assert "- be fast" in result["Rules"]

    def test_collision_prepends_preamble_to_existing_project(self):
        # codex emits "# AGENTS.md" then "## Project"; extra prose after the H1
        # must prepend to (not clobber) the ## Project body, preserving order.
        content = "# AGENTS.md\n\nExtra note.\n\n## Project\n\nBody text.\n"
        result = extract_sections_from_agent_file(content, source="codex")
        assert result["Project"].startswith("Extra note.")
        assert "Body text." in result["Project"]
        assert "# AGENTS.md" not in result["Project"]

    def test_wrapper_h1_is_stripped_for_known_source(self):
        content = sections_to_markdown({"Project": "P", "Commands": "C"})
        result = extract_sections_from_agent_file(content, source="cursor")
        assert "# Project Context" not in result["Project"]
        assert result["Project"] == "P"

    def test_source_none_preserves_old_drop_behavior(self):
        # Back-compat: with no source, leading prose is dropped exactly as
        # before — the canonical contract is unchanged.
        content = "# CLAUDE.md\n\nThis file...\n\nLeftover prose.\n\n## Project\n\nBody\n"
        result = extract_sections_from_agent_file(content)
        assert "Leftover prose." not in result.get("Project", "")
        assert result["Project"] == "Body"


class TestPreambleSourceGuard:
    """`preamble_source` gates capture to a generator's canonical file (#1147
    B1-3 review): rule fragments detected as agent='cursor' must NOT import as
    Project."""

    def test_canonical_cursorrules_gets_source(self):
        assert preamble_source("cursor", Path("/proj/.cursorrules")) == "cursor"

    @pytest.mark.parametrize(
        "agent,name",
        [
            ("claude", "CLAUDE.md"),
            ("gemini", "GEMINI.md"),
            ("codex", "AGENTS.md"),
            ("copilot", "copilot-instructions.md"),
        ],
    )
    def test_canonical_files_get_source(self, agent, name):
        assert preamble_source(agent, Path(f"/proj/{name}")) == agent

    def test_cursor_rules_fragment_gets_no_source(self):
        # .cursor/rules/*.mdc is detected as agent='cursor' but is a rule
        # fragment, not Project prose — must fall back to source=None.
        assert preamble_source("cursor", Path("/proj/.cursor/rules/style.mdc")) is None
        assert preamble_source("cursor", Path("/proj/.cursor/rules/style.md")) is None

    def test_none_agent_is_none(self):
        assert preamble_source(None, Path("/proj/whatever.md")) is None

    def test_fragment_content_not_imported_as_project(self):
        # End-to-end: a Cursor MDC rule fragment routed with the guarded source
        # (None) drops its prose instead of seeding Project.
        fragment = "---\ndescription: Python style\nglobs: **/*.py\n---\nUse type hints.\n"
        src = preamble_source("cursor", Path("/proj/.cursor/rules/py.mdc"))
        result = extract_sections_from_agent_file(fragment, source=src)
        assert "Use type hints." not in result.get("Project", "")


class TestPreambleProjectSubheadings:
    """Reverse-import contract: every ## starts a section, so a captured
    Project body never embeds a ## that would re-split on the next round-trip;
    keep subheadings inside a section body with ### (#1147 B1-3 review)."""

    def test_h2_in_flat_body_splits_into_its_own_section(self):
        # A hand-authored flat file with a ## heading: that heading starts a
        # section rather than folding into Project as an unsound embedded ##
        # (which would re-split on the next generate->import cycle).
        content = "Intro line.\n\n## Goals\n\nBe fast.\n\n## Rules\n\n- be terse\n"
        result = extract_sections_from_agent_file(content, source="cursor")
        assert result["Project"] == "Intro line."
        assert result["Goals"] == "Be fast."
        assert "- be terse" in result["Rules"]
        # Soundness invariant: no captured section body contains a ## heading.
        assert all("## " not in body for body in result.values())

    def test_persisted_context_md_round_trip_is_idempotent(self, tmp_path):
        # The user-visible contract is the *persisted* round-trip: extracted
        # sections are written to context.md (sections_to_markdown) and re-read
        # (parse_context) on every later `generate`/`sync`. Pin that this is
        # stable — a flat-file `## Deployment` survives as its own section and
        # the persisted form re-parses to the identical dict, with no `## `
        # embedded in any section body (the soundness win of the section model).
        #
        content = "Intro line.\n\n## Deployment\n\nShip it.\n\n## Rules\n\n- be terse\n"
        sections = extract_sections_from_agent_file(content, source="cursor")

        ctx = tmp_path / "context.md"
        ctx.write_text(sections_to_markdown(sections), encoding="utf-8")
        reparsed = parse_context(ctx)

        # The non-canonical heading persists as its own section, not lost.
        assert reparsed["Project"] == "Intro line."
        assert reparsed["Deployment"] == "Ship it."
        assert reparsed["Rules"] == "- be terse"
        # No section body smuggles a ## that would re-split on the next read.
        assert all("## " not in body for body in reparsed.values())
        # Persisted form is a fixed point: re-serialize → re-parse is stable.
        ctx.write_text(sections_to_markdown(reparsed), encoding="utf-8")
        assert parse_context(ctx) == reparsed

        # Runtime generation is also no longer lossy for non-canonical sections.
        generated = generate_for_agent("cursor", reparsed)
        extracted = extract_sections_from_agent_file(generated, source="cursor")
        assert extracted["Deployment"] == "Ship it."

    @pytest.mark.parametrize("source", ["cursor", "copilot"])
    def test_h3_subheading_stays_in_project_and_round_trips(self, source):
        # ### is the documented escape hatch: it is not a section boundary, so a
        # Project body keeps its ### subheadings and round-trips byte-for-byte.
        original = {
            "Project": "Intro line.\n\n### Goals\n\nBe fast.",
            "Rules": "- be terse",
        }
        gen = generate_for_agent(source, original)
        result = extract_sections_from_agent_file(gen, source=source)
        assert "### Goals" in result["Project"]
        assert "Be fast." in result["Project"]
        assert "Goals" not in result  # not a separate section
        assert generate_for_agent(source, result) == gen

    def test_known_section_heading_splits(self):
        # A canonical section heading (Rules) is a boundary; preamble -> Project.
        content = "Project prose.\n\n## Rules\n\n- foo\n"
        result = extract_sections_from_agent_file(content, source="cursor")
        assert result["Project"] == "Project prose."
        assert "- foo" in result["Rules"]
