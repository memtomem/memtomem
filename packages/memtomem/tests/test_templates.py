"""Tests for template rendering."""

import json
import pytest
from memtomem.templates import render_template, list_templates, TEMPLATE_NAMES


class TestRenderTemplate:
    def test_adr_with_json(self):
        result = render_template("adr", json.dumps({
            "title": "Use Redis",
            "status": "accepted",
            "context": "Need caching",
            "decision": "Redis cluster",
            "consequences": "Ops overhead",
        }))
        assert "ADR: Use Redis" in result
        assert "**Status**: accepted" in result
        assert "**Decision**: Redis cluster" in result

    def test_meeting_auto_date(self):
        result = render_template("meeting", json.dumps({
            "title": "Standup",
            "attendees": "Team",
            "agenda": "Updates",
            "decisions": "None",
            "action_items": "None",
        }))
        assert "Meeting: Standup" in result
        assert "2026-" in result  # auto-filled date

    def test_debug_plain_text(self):
        result = render_template("debug", "Server crashed on startup")
        assert "Server crashed" in result
        assert "Debug:" in result

    def test_procedure_template(self):
        result = render_template("procedure", json.dumps({
            "title": "Deploy",
            "trigger": "new release",
            "steps": "1. Build\n2. Test",
            "tags": "deploy",
        }))
        assert "Procedure: Deploy" in result
        assert "Trigger" in result

    def test_invalid_template_raises(self):
        with pytest.raises(ValueError, match="Unknown template"):
            render_template("nonexistent", "test")

    def test_default_values_applied(self):
        result = render_template("adr", json.dumps({"title": "Test"}))
        assert "**Status**: proposed" in result  # default

    def test_title_from_param(self):
        result = render_template("adr", "{}", title="From Param")
        assert "ADR: From Param" in result


class TestListTemplates:
    def test_lists_all_templates(self):
        result = list_templates()
        for name in TEMPLATE_NAMES:
            assert name in result

    def test_template_names_sorted(self):
        assert TEMPLATE_NAMES == sorted(TEMPLATE_NAMES)
