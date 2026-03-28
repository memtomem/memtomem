"""Entity extraction from unstructured text — regex + heuristic approach."""

from __future__ import annotations

import re
from dataclasses import dataclass

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_MONTHS_PATTERN = "|".join(_MONTHS)

_KNOWN_TECH = {
    "python",
    "javascript",
    "typescript",
    "rust",
    "golang",
    "java",
    "ruby",
    "swift",
    "kotlin",
    "react",
    "vue",
    "angular",
    "nextjs",
    "django",
    "flask",
    "fastapi",
    "express",
    "docker",
    "kubernetes",
    "k8s",
    "terraform",
    "aws",
    "gcp",
    "azure",
    "postgresql",
    "mysql",
    "sqlite",
    "mongodb",
    "redis",
    "elasticsearch",
    "graphql",
    "grpc",
    "rest",
    "kafka",
    "rabbitmq",
    "nginx",
    "linux",
    "git",
    "github",
    "gitlab",
    "jenkins",
    "circleci",
    "webpack",
    "vite",
    "tailwind",
    "prisma",
    "supabase",
    "vercel",
    "netlify",
    "heroku",
    "langchain",
    "langgraph",
    "openai",
    "anthropic",
    "ollama",
    "llm",
    "mcp",
    "rag",
    "embeddings",
    "pytorch",
    "tensorflow",
}

# ── Regex patterns ────────────────────────────────────────────────────

_PERSON_CONTEXT_RE = re.compile(
    r"(?:(?:by|from|cc|to|with|author|assigned|reviewer)\s*[:\s]+)"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})",
    re.MULTILINE,
)
_MENTION_RE = re.compile(r"@([a-zA-Z]\w{2,})")

_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_NATURAL_DATE_RE = re.compile(
    rf"\b((?:{_MONTHS_PATTERN})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?)(?:\b|$)",
    re.IGNORECASE,
)

_DECISION_RE = re.compile(
    r"^[\s*-]*(?:Decision|Decided|We\s+will|Agreed|Resolved|Conclusion)[:\s]+(.+)$",
    re.MULTILINE | re.IGNORECASE,
)

_ACTION_RE = re.compile(
    r"(?:^|\n)\s*(?:"
    r"(?:TODO|FIXME|HACK|XXX|ACTION)[:\s]+(.+)|"
    r"-\s*\[\s*\]\s+(.+)|"
    r"(?:Action\s+item)[:\s]+(.+)"
    r")",
    re.MULTILINE | re.IGNORECASE,
)

_PASCAL_CASE_RE = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b")

_QUOTED_TERM_RE = re.compile(r'"([^"]{3,50})"')


@dataclass(frozen=True)
class ExtractedEntity:
    entity_type: str
    entity_value: str
    confidence: float
    position: int


def extract_entities(
    text: str,
    entity_types: list[str] | None = None,
) -> list[ExtractedEntity]:
    """Extract structured entities from unstructured text."""
    if not text:
        return []

    types = (
        set(entity_types)
        if entity_types
        else {
            "person",
            "date",
            "decision",
            "action_item",
            "technology",
            "concept",
        }
    )

    results: list[ExtractedEntity] = []
    if "person" in types:
        results.extend(_extract_persons(text))
    if "date" in types:
        results.extend(_extract_dates(text))
    if "decision" in types:
        results.extend(_extract_decisions(text))
    if "action_item" in types:
        results.extend(_extract_action_items(text))
    if "technology" in types:
        results.extend(_extract_technologies(text))
    if "concept" in types:
        results.extend(_extract_concepts(text))

    # Deduplicate by (type, value)
    seen: set[tuple[str, str]] = set()
    unique: list[ExtractedEntity] = []
    for e in results:
        key = (e.entity_type, e.entity_value.lower())
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


def _extract_persons(text: str) -> list[ExtractedEntity]:
    results: list[ExtractedEntity] = []
    for m in _PERSON_CONTEXT_RE.finditer(text):
        name = m.group(1).strip()
        if len(name) > 3:
            results.append(ExtractedEntity("person", name, 0.8, m.start(1)))
    for m in _MENTION_RE.finditer(text):
        results.append(ExtractedEntity("person", f"@{m.group(1)}", 0.7, m.start()))
    return results


def _extract_dates(text: str) -> list[ExtractedEntity]:
    results: list[ExtractedEntity] = []
    for m in _ISO_DATE_RE.finditer(text):
        results.append(ExtractedEntity("date", m.group(1), 0.95, m.start()))
    for m in _NATURAL_DATE_RE.finditer(text):
        results.append(ExtractedEntity("date", m.group(1).strip(), 0.8, m.start()))
    return results


def _extract_decisions(text: str) -> list[ExtractedEntity]:
    results: list[ExtractedEntity] = []
    for m in _DECISION_RE.finditer(text):
        value = m.group(1).strip()
        if len(value) > 5:
            results.append(ExtractedEntity("decision", value[:200], 0.85, m.start()))
    return results


def _extract_action_items(text: str) -> list[ExtractedEntity]:
    results: list[ExtractedEntity] = []
    for m in _ACTION_RE.finditer(text):
        value = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if len(value) > 3:
            results.append(ExtractedEntity("action_item", value[:200], 0.9, m.start()))
    return results


def _extract_technologies(text: str) -> list[ExtractedEntity]:
    results: list[ExtractedEntity] = []
    text_lower = text.lower()

    # Known tech terms
    for tech in _KNOWN_TECH:
        idx = text_lower.find(tech)
        if idx >= 0:
            # Find the original-case version
            original = text[idx : idx + len(tech)]
            results.append(ExtractedEntity("technology", original, 0.9, idx))

    # PascalCase words (potential tech names)
    for m in _PASCAL_CASE_RE.finditer(text):
        word = m.group(1)
        if word.lower() not in _KNOWN_TECH and len(word) > 4:
            results.append(ExtractedEntity("technology", word, 0.5, m.start()))

    return results


def _extract_concepts(text: str) -> list[ExtractedEntity]:
    results: list[ExtractedEntity] = []
    # Quoted terms
    for m in _QUOTED_TERM_RE.finditer(text):
        term = m.group(1).strip()
        if len(term) > 3:
            results.append(ExtractedEntity("concept", term, 0.7, m.start()))
    return results
