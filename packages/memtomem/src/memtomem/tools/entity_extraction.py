"""Entity extraction from unstructured text — regex + heuristic approach.

When an LLM provider is available, ``extract_entities_with_llm`` tries the
LLM first for higher-quality extraction (especially person names and
decisions) and falls back to the regex heuristic.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem.llm.base import LLMProvider

logger = logging.getLogger(__name__)

# ── Confidence thresholds ─────────────────────────────────────────────
CONFIDENCE_PERSON_CONTEXT = 0.8
CONFIDENCE_PERSON_MENTION = 0.7
CONFIDENCE_DATE_ISO = 0.95
CONFIDENCE_DATE_NATURAL = 0.8
CONFIDENCE_DECISION = 0.85
CONFIDENCE_ACTION_ITEM = 0.9
CONFIDENCE_TECH_KNOWN = 0.9
CONFIDENCE_TECH_INFERRED = 0.5
CONFIDENCE_CONCEPT = 0.7
CONFIDENCE_LLM_FALLBACK = 0.7

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
            results.append(ExtractedEntity("person", name, CONFIDENCE_PERSON_CONTEXT, m.start(1)))
    for m in _MENTION_RE.finditer(text):
        results.append(ExtractedEntity("person", f"@{m.group(1)}", CONFIDENCE_PERSON_MENTION, m.start()))
    return results


def _extract_dates(text: str) -> list[ExtractedEntity]:
    results: list[ExtractedEntity] = []
    for m in _ISO_DATE_RE.finditer(text):
        results.append(ExtractedEntity("date", m.group(1), CONFIDENCE_DATE_ISO, m.start()))
    for m in _NATURAL_DATE_RE.finditer(text):
        results.append(ExtractedEntity("date", m.group(1).strip(), CONFIDENCE_DATE_NATURAL, m.start()))
    return results


def _extract_decisions(text: str) -> list[ExtractedEntity]:
    results: list[ExtractedEntity] = []
    for m in _DECISION_RE.finditer(text):
        value = m.group(1).strip()
        if len(value) > 5:
            results.append(ExtractedEntity("decision", value[:200], CONFIDENCE_DECISION, m.start()))
    return results


def _extract_action_items(text: str) -> list[ExtractedEntity]:
    results: list[ExtractedEntity] = []
    for m in _ACTION_RE.finditer(text):
        value = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if len(value) > 3:
            results.append(ExtractedEntity("action_item", value[:200], CONFIDENCE_ACTION_ITEM, m.start()))
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
            results.append(ExtractedEntity("technology", original, CONFIDENCE_TECH_KNOWN, idx))

    # PascalCase words (potential tech names)
    for m in _PASCAL_CASE_RE.finditer(text):
        word = m.group(1)
        if word.lower() not in _KNOWN_TECH and len(word) > 4:
            results.append(ExtractedEntity("technology", word, CONFIDENCE_TECH_INFERRED, m.start()))

    return results


def _extract_concepts(text: str) -> list[ExtractedEntity]:
    results: list[ExtractedEntity] = []
    # Quoted terms
    for m in _QUOTED_TERM_RE.finditer(text):
        term = m.group(1).strip()
        if len(term) > 3:
            results.append(ExtractedEntity("concept", term, CONFIDENCE_CONCEPT, m.start()))
    return results


# ---------------------------------------------------------------------------
# LLM-based entity extraction (optional upgrade over regex)
# ---------------------------------------------------------------------------

_VALID_ENTITY_TYPES = frozenset(
    {"person", "date", "decision", "action_item", "technology", "concept"}
)

_ENTITY_SYSTEM_PROMPT = (
    "You are an entity extraction assistant. Extract structured entities "
    "from the given text. For each entity, output one line in the format:\n"
    "TYPE|VALUE|CONFIDENCE\n"
    "where TYPE is one of: person, date, decision, action_item, technology, "
    "concept. CONFIDENCE is a float 0.0-1.0. Output ONLY the entity lines, "
    "nothing else. If no entities are found, output NONE."
)


def _parse_entity_response(
    raw: str,
    entity_types: set[str] | None = None,
) -> list[ExtractedEntity]:
    """Parse TYPE|VALUE|CONFIDENCE lines from an LLM response."""
    from memtomem.llm.utils import strip_llm_response

    text = strip_llm_response(raw)
    if not text or text.strip().upper() == "NONE":
        return []

    results: list[ExtractedEntity] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.upper() == "NONE":
            continue
        parts = line.split("|")
        if len(parts) < 2:
            continue  # malformed line — skip
        etype = parts[0].strip().lower()
        if etype not in _VALID_ENTITY_TYPES:
            continue
        if entity_types and etype not in entity_types:
            continue
        value = parts[1].strip()
        if not value:
            continue
        try:
            confidence = float(parts[2].strip()) if len(parts) >= 3 else CONFIDENCE_LLM_FALLBACK
            confidence = max(0.0, min(1.0, confidence))
        except (ValueError, IndexError):
            confidence = CONFIDENCE_LLM_FALLBACK
        results.append(ExtractedEntity(etype, value[:200], confidence, 0))
    return results


async def extract_entities_llm(
    text: str,
    llm_provider: LLMProvider,
    entity_types: list[str] | None = None,
) -> list[ExtractedEntity]:
    """Extract entities using an LLM for higher-quality structured extraction.

    Args:
        text: Content to extract entities from (truncated to 3000 chars).
        llm_provider: An initialised LLMProvider instance.
        entity_types: Restrict to these entity types (default: all).

    Returns:
        List of :class:`ExtractedEntity` parsed from the LLM response.
    """
    type_hint = ""
    if entity_types:
        type_hint = f"\nOnly extract these types: {', '.join(entity_types)}\n"

    prompt = f"Extract entities from the following text.{type_hint}\n---\n{text[:3000]}\n---"

    raw = await llm_provider.generate(prompt, system=_ENTITY_SYSTEM_PROMPT, max_tokens=256)
    types_set = set(entity_types) if entity_types else None
    return _parse_entity_response(raw, types_set)


async def extract_entities_with_llm(
    text: str,
    entity_types: list[str] | None = None,
    llm_provider: LLMProvider | None = None,
) -> list[ExtractedEntity]:
    """Extract entities, preferring LLM when available.

    Falls back to the regex heuristic when *llm_provider* is ``None`` or
    when the LLM call fails.
    """
    if llm_provider is not None:
        try:
            return await extract_entities_llm(text, llm_provider, entity_types)
        except Exception:
            logger.warning(
                "LLM entity extraction failed, using heuristic fallback",
                exc_info=True,
            )
    return extract_entities(text, entity_types)
