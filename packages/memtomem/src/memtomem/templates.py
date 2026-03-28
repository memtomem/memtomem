"""Built-in memory templates for structured entries."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

TEMPLATES: dict[str, str] = {
    "adr": (
        "## ADR: {title}\n\n"
        "**Status**: {status}\n"
        "**Context**: {context}\n"
        "**Decision**: {decision}\n"
        "**Consequences**: {consequences}"
    ),
    "meeting": (
        "## Meeting: {title}\n\n"
        "**Date**: {date}\n"
        "**Attendees**: {attendees}\n"
        "**Agenda**: {agenda}\n"
        "**Decisions**: {decisions}\n"
        "**Action Items**: {action_items}"
    ),
    "debug": (
        "## Debug: {title}\n\n"
        "**Symptom**: {symptom}\n"
        "**Root Cause**: {root_cause}\n"
        "**Fix**: {fix}\n"
        "**Prevention**: {prevention}"
    ),
    "decision": (
        "## Decision: {title}\n\n"
        "**Options**: {options}\n"
        "**Chosen**: {chosen}\n"
        "**Rationale**: {rationale}"
    ),
    "procedure": (
        "## Procedure: {title}\n\n**Trigger**: {trigger}\n**Steps**:\n{steps}\n**Tags**: {tags}"
    ),
}

# Default values for optional template fields
_DEFAULTS: dict[str, dict[str, str]] = {
    "adr": {"status": "proposed"},
    "meeting": {"date": "today"},
}

# All available template names
TEMPLATE_NAMES = sorted(TEMPLATES.keys())


def list_templates() -> str:
    """Return a formatted list of available templates with their fields."""
    lines = []
    for name in TEMPLATE_NAMES:
        fields = re.findall(r"\{(\w+)\}", TEMPLATES[name])
        lines.append(f"  {name}: {', '.join(fields)}")
    return "\n".join(lines)


def render_template(name: str, content: str, title: str | None = None) -> str:
    """Render a template with the given content.

    Content can be:
    - JSON object: keys map to template fields
    - Plain text: used as the main body field
    """
    if name not in TEMPLATES:
        raise ValueError(f"Unknown template '{name}'. Available: {', '.join(TEMPLATE_NAMES)}")

    template = TEMPLATES[name]
    fields = re.findall(r"\{(\w+)\}", template)

    # Try to parse content as JSON for field mapping
    values: dict[str, str] = {}
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            values = {k: str(v) for k, v in parsed.items()}
    except (json.JSONDecodeError, TypeError):
        # Plain text: use as the primary content field
        # Find the most likely "body" field (last non-title field)
        body_fields = [f for f in fields if f != "title"]
        if body_fields:
            values[body_fields[0]] = content

    # Apply defaults
    defaults = _DEFAULTS.get(name, {})
    for field in fields:
        if field not in values:
            if field == "title" and title:
                values[field] = title
            elif field == "date" and defaults.get("date") == "today":
                values[field] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            elif field in defaults:
                values[field] = defaults[field]
            else:
                values[field] = f"(fill: {field})"

    # Render
    result = template
    for field in fields:
        result = result.replace(f"{{{field}}}", values.get(field, f"(fill: {field})"))

    # Remove field lines with unfilled placeholders, but keep headings
    result = "\n".join(
        line for line in result.splitlines()
        if "(fill: " not in line or line.lstrip().startswith("#")
    )

    return result
