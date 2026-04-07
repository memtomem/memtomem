"""Response compression strategies."""

from __future__ import annotations

import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass
from typing import Protocol

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

from memtomem_stm.proxy.config import (
    CompressionStrategy,
    LLMCompressorConfig,
    LLMProvider,
    TailMode,
)
from memtomem_stm.utils.circuit_breaker import CircuitBreaker as _CircuitBreaker

logger = logging.getLogger(__name__)


# ── Query-Aware Relevance Scoring ─────────────────────────────────────


class QueryRelevanceScorer:
    """BM25-like section relevance scoring with heading weighting.

    Headings are weighted 3× over body text (headings use vocabulary closer
    to user queries). Case-insensitive with basic suffix stemming.
    """

    _TOKEN_RE = re.compile(r"[a-zA-Z0-9\uac00-\ud7a3\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff_]+")
    _SUFFIX_RE = re.compile(r"(ing|ed|ly|tion|ness|ment|ies|es|s)$")
    _HEADING_WEIGHT = 3.0

    def __init__(
        self,
        query: str,
        sections: list[tuple[str, str]],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self._k1 = k1
        self._b = b
        self._query_terms = self._tokenize(query)
        self._sections = sections
        # Pre-compute per-section token counts (heading-weighted)
        self._doc_tfs: list[dict[str, float]] = []
        self._doc_lens: list[float] = []
        for title, body in sections:
            heading_tokens = self._tokenize(title)
            body_tokens = self._tokenize(body)
            tf: dict[str, float] = {}
            for t in heading_tokens:
                tf[t] = tf.get(t, 0.0) + self._HEADING_WEIGHT
            for t in body_tokens:
                tf[t] = tf.get(t, 0.0) + 1.0
            self._doc_tfs.append(tf)
            self._doc_lens.append(len(heading_tokens) * self._HEADING_WEIGHT + len(body_tokens))
        self._avgdl = sum(self._doc_lens) / len(self._doc_lens) if self._doc_lens else 1.0
        # IDF per query term
        n = len(sections)
        self._idfs: dict[str, float] = {}
        for t in set(self._query_terms):
            df = sum(1 for tfs in self._doc_tfs if t in tfs)
            self._idfs[t] = math.log((n - df + 0.5) / (df + 0.5) + 1.0)

    def _tokenize(self, text: str) -> list[str]:
        tokens = self._TOKEN_RE.findall(text.lower())
        return [self._stem(t) for t in tokens]

    def _stem(self, token: str) -> str:
        if len(token) > 4:
            return self._SUFFIX_RE.sub("", token)
        return token

    def score(self, section_idx: int) -> float:
        if not self._query_terms:
            return 0.0
        tf_map = self._doc_tfs[section_idx]
        dl = self._doc_lens[section_idx]
        total = 0.0
        for t in self._query_terms:
            tf = tf_map.get(t, 0.0)
            idf = self._idfs.get(t, 0.0)
            num = tf * (self._k1 + 1.0)
            den = tf + self._k1 * (1.0 - self._b + self._b * dl / self._avgdl)
            total += idf * num / den if den > 0 else 0.0
        return total

    def score_all(self) -> list[float]:
        return [self.score(i) for i in range(len(self._sections))]


def _content_summary(text: str) -> str:
    """Count structural elements in the original text for truncation metadata."""
    counts: list[str] = []
    headings = len(re.findall(r"(?:^|\n)#{1,6}\s", text))
    code_blocks = len(re.findall(r"```", text)) // 2
    list_items = len(re.findall(r"(?:^|\n)\s*[-*]\s", text))
    links = len(re.findall(r"\[.*?\]\(.*?\)", text))
    if headings:
        counts.append(f"{headings} headings")
    if code_blocks:
        counts.append(f"{code_blocks} code blocks")
    if list_items:
        counts.append(f"{list_items} list items")
    if links:
        counts.append(f"{links} links")
    return f" [{', '.join(counts)}]" if counts else ""


class Compressor(Protocol):
    def compress(self, text: str, *, max_chars: int) -> str: ...


class NoopCompressor:
    """No compression — passthrough."""

    def compress(self, text: str, *, max_chars: int) -> str:
        return text


class TruncateCompressor:
    """Character limit with sentence/word boundary awareness.

    For text with markdown headings, prefers to cut at heading boundaries
    and appends a list of remaining section titles. For plain text, cuts
    at the nearest sentence or word boundary.

    Note: minimum retention is enforced at the pipeline level
    (ProxyManager / BenchHarness), not in the compressor. The compressor
    trusts the max_chars budget it receives.
    """

    _HEADING_RE = re.compile(r"(?:^|\n)(#{1,6}\s+.+)")

    # Patterns for code structure boundaries (function/class/method definitions)
    _CODE_BOUNDARY_RE = re.compile(
        r"(?:^|\n)"
        r"(\s*(?:def |class |async def |function |func |export |pub fn )\S.*)",
    )
    # SQL top-level statement boundaries (non-indented only)
    _SQL_BOUNDARY_RE = re.compile(
        r"(?:^|\n)((?:SELECT|WITH|CREATE|INSERT|UPDATE|DELETE)\s)", re.IGNORECASE
    )
    # Comment-section boundaries (-- Section Header)
    _COMMENT_SECTION_RE = re.compile(r"(?:^|\n)(--\s+\S.+)")

    def compress(self, text: str, *, max_chars: int, context_query: str | None = None) -> str:
        if not text or len(text) <= max_chars:
            return text

        # Try JSON key-aware truncation — only for config-like dicts (all values are dicts)
        stripped = text.strip()
        if stripped and stripped[0] == "{":
            try:
                data = json.loads(stripped)
                if (
                    isinstance(data, dict)
                    and len(data) >= 2
                    and all(isinstance(v, dict) for v in data.values())
                ):
                    return self._json_key_truncate(data, max_chars, context_query)
            except (json.JSONDecodeError, ValueError):
                pass

        # Try section-aware truncation for markdown with headings
        headings = list(self._HEADING_RE.finditer(text))
        if len(headings) >= 2:
            return self._section_aware_truncate(text, max_chars, headings, context_query)

        # Try code-structure-aware truncation (function/class/SQL boundaries)
        code_boundaries = list(self._CODE_BOUNDARY_RE.finditer(text))
        if len(code_boundaries) >= 2:
            return self._code_aware_truncate(text, max_chars, code_boundaries)

        # Try SQL/comment-section boundaries
        sql_boundaries = list(self._COMMENT_SECTION_RE.finditer(text))
        if len(sql_boundaries) < 2:
            sql_boundaries = list(self._SQL_BOUNDARY_RE.finditer(text))
        if len(sql_boundaries) >= 2:
            return self._code_aware_truncate(text, max_chars, sql_boundaries)

        # Repetitive content: preserve tail anomaly
        result = self._tail_anomaly_truncate(text, max_chars)
        if result:
            return result

        # Fallback: position-based truncation
        break_at = self._find_break(text, max_chars)
        summary = _content_summary(text)
        return text[:break_at] + f"\n... (truncated, original: {len(text)} chars){summary}"

    _SUMMARY_RE = re.compile(
        r"summary|conclusion|결론|요약|security|root\s*cause|remediation"
        r"|troubleshoot|보안|원인|조치",
        re.IGNORECASE,
    )

    def _json_key_truncate(
        self, data: dict, max_chars: int, context_query: str | None = None
    ) -> str:
        """Distribute budget across all top-level JSON keys.

        Each key gets a proportional share of the budget based on its
        serialized size. When context_query is provided, budget is weighted
        by BM25 relevance (blended with size-proportional allocation).
        """
        # Serialize each top-level key separately to measure sizes
        key_sizes: list[tuple[str, str, int]] = []
        for k, v in data.items():
            serialized = json.dumps({k: v}, ensure_ascii=False, indent=2)
            key_sizes.append((k, serialized, len(serialized)))

        total_size = sum(s for _, _, s in key_sizes)
        overhead = 10  # {}, commas, newlines
        available = max_chars - overhead

        # Compute query-aware weights if applicable
        relevance_weights: list[float] | None = None
        if context_query and context_query.strip():
            sections = [(k, ser) for k, ser, _ in key_sizes]
            scorer = QueryRelevanceScorer(context_query, sections)
            raw = scorer.score_all()
            if any(s > 0 for s in raw):
                relevance_weights = raw

        # Build output: each key gets budget allocation
        parts: list[str] = []
        for idx, (k, serialized, size) in enumerate(key_sizes):
            # Size-proportional base
            size_share = available * size / total_size if total_size else available / len(key_sizes)
            if relevance_weights is not None:
                total_rel = sum(relevance_weights)
                rel_share = available * relevance_weights[idx] / total_rel if total_rel else 0
                # Blend: 40% size-proportional + 60% relevance
                key_budget = max(40, int(0.4 * size_share + 0.6 * rel_share))
            else:
                key_budget = max(40, int(size_share))

            if size <= key_budget:
                inner = serialized.strip()[1:-1].strip()
                parts.append(inner)
            else:
                v = data[k]
                truncated = self._truncate_json_value(v, key_budget - len(k) - 6)
                part = json.dumps({k: truncated}, ensure_ascii=False, indent=2)
                inner = part.strip()[1:-1].strip()
                parts.append(inner)

        result = "{\n" + ",\n".join(parts) + "\n}"
        if len(result) > max_chars:
            result = result[:max_chars] + "\n... (truncated)"
        return result

    def _truncate_json_value(self, value: object, budget: int) -> object:
        """Truncate a JSON value to fit within character budget."""
        if isinstance(value, str):
            if len(value) > budget:
                return value[:budget] + "..."
            return value
        if isinstance(value, dict):
            preview: dict = {}
            per_key = max(20, budget // max(1, len(value)))
            for k, v in value.items():
                preview[k] = self._truncate_json_value(v, per_key)
            return preview
        if isinstance(value, list):
            if not value:
                return value
            n = min(3, len(value))
            items = [self._truncate_json_value(item, budget // max(1, n)) for item in value[:n]]
            if len(value) > n:
                items.append(f"... ({len(value) - n} more)")
            return items
        return value

    # Pattern to strip timestamps/IDs for repetitive content detection
    _TIMESTAMP_RE = re.compile(
        r"\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}[:\d.]*\S*"
        r"|\b\d{10,13}\b"  # unix timestamps
    )

    @classmethod
    def _tail_anomaly_truncate(cls, text: str, max_chars: int) -> str | None:
        """Detect highly repetitive content and preserve tail anomaly.

        If >50% of lines match a repeated pattern (after stripping timestamps)
        and the tail differs, keep a sample + full tail anomaly.
        Returns None if content is not repetitive.
        """
        lines = text.split("\n")
        if len(lines) < 10:
            return None

        # Fingerprint: strip timestamps/numbers, keep structure
        def _fp(line: str) -> str:
            stripped = cls._TIMESTAMP_RE.sub("", line.strip())
            # Also normalize varying numbers (latency, counts)
            stripped = re.sub(r"\d+", "#", stripped)
            return stripped[:40]

        fingerprints: dict[str, int] = {}
        for line in lines:
            fp = _fp(line)
            if fp:
                fingerprints[fp] = fingerprints.get(fp, 0) + 1

        if not fingerprints:
            return None

        top_fp, top_count = max(fingerprints.items(), key=lambda x: x[1])
        if top_count < len(lines) * 0.5:
            return None  # Not repetitive enough

        # Find non-matching tail lines (anomalies)
        tail_lines: list[str] = []
        for line in reversed(lines):
            if _fp(line) != top_fp:
                tail_lines.insert(0, line)
            else:
                break

        if not tail_lines:
            return None

        # Build: first few lines + count + tail anomaly
        tail_text = "\n".join(tail_lines)
        sample_count = 3
        head_lines = lines[:sample_count]
        head_text = "\n".join(head_lines)
        omitted = len(lines) - sample_count - len(tail_lines)

        result = f"{head_text}\n... ({omitted} similar lines omitted)\n{tail_text}"
        if len(result) > max_chars:
            result = result[:max_chars]
        return result

    def _section_aware_truncate(
        self,
        text: str,
        max_chars: int,
        headings: list[re.Match],
        context_query: str | None = None,
    ) -> str:
        """Preserve information from ALL sections — minimum representation first.

        Strategy (eliminates head bias):
        1. Reserve minimum space: heading + first content line for EVERY section
        2. Distribute remaining budget:
           - With context_query: proportional to BM25 relevance scores
           - Without context_query: top-down sequential (original behavior)
        3. Detect and preserve summary/conclusion sections
        """
        # Parse sections: (title, body_text) pairs
        sections: list[tuple[str, str]] = []
        for i, m in enumerate(headings):
            start = m.start()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            title = m.group(1).strip()
            body = text[start:end].rstrip()
            sections.append((title, body))

        # Text before the first heading (preamble)
        preamble = text[: headings[0].start()].rstrip() if headings[0].start() > 0 else ""

        # Detect summary/conclusion as last section
        summary_idx = -1
        if sections:
            last_title = sections[-1][0]
            if self._SUMMARY_RE.search(last_title):
                summary_idx = len(sections) - 1

        # ── Phase 1: Minimum representation for EVERY section ──
        # Heading + first meaningful content line (guarantees tail section visibility)
        minimums: list[str] = []  # one per section
        min_used = 0
        for i, (title, body) in enumerate(sections):
            lines = body.split("\n")
            # Skip leading empty lines (match position may include \n before heading)
            while lines and not lines[0].strip():
                lines = lines[1:]
            kept = [lines[0]] if lines else [title]  # heading
            for line in lines[1:]:
                if line.strip() and not line.strip().startswith("#"):
                    kept.append(line)
                    break
            snippet = "\n".join(kept)
            minimums.append(snippet)
            min_used += len(snippet) + 2

        if preamble:
            min_used += len(preamble) + 2

        # ── Phase 2: Enrich sections with remaining budget ──
        footer_reserve = 80
        enrich_budget = max_chars - min_used - footer_reserve
        enriched: list[str] = list(minimums)  # start from minimums

        if enrich_budget > 0:
            # Compute per-section relevance scores if query is provided
            scores: list[float] | None = None
            if context_query and context_query.strip():
                scorer = QueryRelevanceScorer(context_query, sections)
                raw_scores = scorer.score_all()
                if any(s > 0 for s in raw_scores):
                    scores = raw_scores

            if scores is not None:
                # ── Query-aware: allocate budget proportional to relevance ──
                self._enrich_by_relevance(
                    sections, minimums, enriched, scores, enrich_budget, summary_idx
                )
            else:
                # ── Original: enrich sections top-down ──
                for i, (_title, body) in enumerate(sections):
                    if i == summary_idx:
                        continue
                    extra = len(body) - len(minimums[i])
                    if extra <= 0:
                        continue
                    if extra <= enrich_budget:
                        enriched[i] = body
                        enrich_budget -= extra
                    else:
                        lines = body.split("\n")
                        kept = enriched[i].split("\n")
                        kept_set = set(kept)
                        for line in lines:
                            if line in kept_set:
                                continue
                            if len(line) + 1 > enrich_budget:
                                break
                            kept.append(line)
                            enrich_budget -= len(line) + 1
                        enriched[i] = "\n".join(kept)
                        break

        # ── Phase 3: Summary section enrichment ──
        if summary_idx >= 0:
            remaining = max_chars - sum(len(e) + 2 for e in enriched) - footer_reserve
            if remaining > 50:
                _, summary_body = sections[summary_idx]
                extra = len(summary_body) - len(enriched[summary_idx])
                if extra > 0:
                    if extra <= remaining:
                        enriched[summary_idx] = summary_body
                    else:
                        enriched[summary_idx] = summary_body[
                            : len(enriched[summary_idx]) + remaining
                        ]

        # ── Assemble ──
        parts: list[str] = []
        if preamble:
            parts.append(preamble)
        for i, section_text in enumerate(enriched):
            parts.append(section_text)

        result = "\n\n".join(parts)
        footer = f"\n(original: {len(text)} chars)"
        result += footer

        if len(result) > max_chars:
            result = result[:max_chars]
        return result

    def _enrich_by_relevance(
        self,
        sections: list[tuple[str, str]],
        minimums: list[str],
        enriched: list[str],
        scores: list[float],
        budget: int,
        summary_idx: int,
    ) -> None:
        """Distribute enrichment budget proportional to relevance scores.

        Modifies *enriched* in-place. Higher-scored sections get more budget.
        Each section's "extra" is capped by (full body - minimum).
        """
        # Compute per-section extras and weights
        extras: list[int] = []
        weights: list[float] = []
        for i, (_title, body) in enumerate(sections):
            extra = max(0, len(body) - len(minimums[i]))
            extras.append(extra)
            # summary gets enriched in Phase 3; exclude from relevance allocation
            weights.append(scores[i] if i != summary_idx and extra > 0 else 0.0)

        total_weight = sum(weights)
        if total_weight <= 0:
            return  # nothing to distribute

        # Allocate budget proportionally, capped by each section's extra
        allocations = [0] * len(sections)
        remaining = budget
        for i in range(len(sections)):
            if weights[i] <= 0:
                continue
            share = int(budget * weights[i] / total_weight)
            allocations[i] = min(share, extras[i])
            remaining -= allocations[i]

        # Distribute leftover to highest-scored sections with remaining capacity
        if remaining > 0:
            ranked = sorted(range(len(sections)), key=lambda j: -scores[j])
            for i in ranked:
                if remaining <= 0:
                    break
                gap = extras[i] - allocations[i]
                if gap <= 0:
                    continue
                give = min(gap, remaining)
                allocations[i] += give
                remaining -= give

        # Apply allocations: enrich each section up to its allocation
        for i, (_title, body) in enumerate(sections):
            if allocations[i] <= 0:
                continue
            if allocations[i] >= extras[i]:
                enriched[i] = body
            else:
                # Partial: add lines until allocation exhausted
                lines = body.split("\n")
                kept = enriched[i].split("\n")
                kept_set = set(kept)
                alloc_left = allocations[i]
                for line in lines:
                    if line in kept_set:
                        continue
                    if len(line) + 1 > alloc_left:
                        break
                    kept.append(line)
                    alloc_left -= len(line) + 1
                enriched[i] = "\n".join(kept)

    def _code_aware_truncate(self, text: str, max_chars: int, boundaries: list[re.Match]) -> str:
        """Preserve signatures/names from ALL code blocks, not just the first ones.

        For code files: keeps full body of top functions + signature lines of rest.
        For SQL: keeps first query full + signature of remaining queries.
        """
        # Parse blocks: each boundary starts a logical block
        blocks: list[tuple[str, str]] = []  # (signature_line, full_body)
        for i, m in enumerate(boundaries):
            start = m.start()
            end = boundaries[i + 1].start() if i + 1 < len(boundaries) else len(text)
            sig = m.group(1).strip()
            body = text[start:end].rstrip()
            blocks.append((sig, body))

        # Preamble (imports, docstrings before first boundary)
        preamble = text[: boundaries[0].start()].rstrip() if boundaries[0].start() > 0 else ""

        # Phase 1: full blocks from top
        ratio = max_chars / len(text) if text else 1.0
        full_pct = min(0.80, 0.45 + ratio * 0.4)
        full_budget = int(max_chars * full_pct)

        parts: list[str] = []
        used = 0
        full_count = 0

        if preamble:
            # Keep preamble but cap it
            preamble_budget = min(len(preamble), full_budget // 3)
            if len(preamble) > preamble_budget:
                preamble = preamble[:preamble_budget] + "\n..."
            parts.append(preamble)
            used += len(preamble) + 1

        for i, (sig, body) in enumerate(blocks):
            if used + len(body) + 2 <= full_budget:
                parts.append(body)
                used += len(body) + 2
                full_count = i + 1
            else:
                break

        # Phase 2: signatures of remaining blocks
        remaining = [(sig, body) for i, (sig, body) in enumerate(blocks) if i >= full_count]
        if remaining:
            sig_budget = max_chars - used - 60
            sig_parts: list[str] = []
            sig_used = 0
            for sig, body in remaining:
                # Show signature + first non-empty body line
                body_lines = body.split("\n")
                sig_lines = [body_lines[0]]
                for line in body_lines[1:4]:  # up to 3 more lines
                    stripped = line.strip()
                    if stripped:
                        sig_lines.append(line)
                snippet = "\n".join(sig_lines)
                if sig_used + len(snippet) + 2 > sig_budget:
                    if sig_used + len(sig) + 4 <= sig_budget:
                        sig_parts.append(f"# {sig}")
                        sig_used += len(sig) + 4
                else:
                    sig_parts.append(snippet)
                    sig_used += len(snippet) + 2

            if sig_parts:
                parts.append(f"\n... ({len(remaining)} more blocks)\n")
                parts.extend(sig_parts)

        result = "\n\n".join(parts)
        result += f"\n(original: {len(text)} chars)"
        if len(result) > max_chars:
            result = result[:max_chars]
        return result

    @staticmethod
    def _find_break(text: str, max_chars: int) -> int:
        end = min(max_chars, len(text) - 1)
        floor = int(max_chars * 0.8)
        for i in range(end, floor - 1, -1):
            if text[i - 1] in ".!?\n" and (i >= len(text) or text[i] in " \n\t"):
                return i
        for i in range(end, floor - 1, -1):
            if i < len(text) and text[i] in " \n\t":
                return i
        return max_chars


@dataclass
class PendingSelection:
    """Stores original chunks while waiting for section selection."""

    chunks: dict[str, str]
    format: str
    created_at: float
    total_chars: int


class SelectiveCompressor:
    """2-phase compression: Phase 1 returns a TOC, Phase 2 returns selected sections."""

    def __init__(
        self,
        max_pending: int = 100,
        pending_ttl_seconds: float = 300.0,
        json_depth: int = 1,
        min_section_chars: int = 50,
        store: object | None = None,
    ) -> None:
        self._max_pending = max_pending
        self._ttl = pending_ttl_seconds
        self._json_depth = json_depth
        self._min_section_chars = min_section_chars
        if store is not None:
            self._store = store
        else:
            from memtomem_stm.proxy.pending_store import InMemoryPendingStore

            self._store = InMemoryPendingStore()

    def compress(self, text: str, *, max_chars: int) -> str:
        if not text or len(text) <= max_chars:
            return text

        fmt, chunks = self._detect_and_parse(text)

        if len(chunks) <= 1:
            chunks = self._decompose_single_chunk(chunks, fmt)
            if len(chunks) <= 1:
                return TruncateCompressor().compress(text, max_chars=max_chars)

        return self._store_and_build_toc(text, fmt, chunks)

    def compress_full_toc(self, text: str, *, max_chars: int) -> str | None:
        fmt, chunks = self._detect_and_parse(text)
        if len(chunks) <= 1:
            chunks = self._decompose_single_chunk(chunks, fmt)
            if len(chunks) <= 1:
                return None
        return self._store_and_build_toc(text, fmt, chunks)

    def _store_and_build_toc(self, text: str, fmt: str, chunks: dict[str, str]) -> str:
        selection_key = uuid.uuid4().hex[:12]

        self._store.put(
            selection_key,
            PendingSelection(
                chunks=chunks,
                format=fmt,
                created_at=time.monotonic(),
                total_chars=len(text),
            ),
        )
        self._evict()

        entries = []
        for key, content in chunks.items():
            size = len(content)
            is_inline = size < self._min_section_chars
            preview = content if is_inline else content[:80].replace("\n", " ")
            content_type = self._infer_type(key, content, fmt)
            entries.append(
                {
                    "key": key,
                    "type": content_type,
                    "size": size,
                    "preview": preview,
                    "inline": is_inline,
                }
            )

        toc = {
            "type": "toc",
            "selection_key": selection_key,
            "format": fmt,
            "total_chars": len(text),
            "entries": entries,
            "hint": f"Call stm_proxy_select_chunks(key='{selection_key}', sections=[...]) to retrieve.",
        }
        return json.dumps(toc, ensure_ascii=False)

    def select(self, key: str, sections: list[str]) -> str:
        self._store.evict_expired(self._ttl)

        pending = self._store.get(key)
        if pending is None:
            return f"Selection key '{key}' not found or expired."

        self._store.touch(key)

        selected_parts: list[str] = []
        for section in sections:
            if section in pending.chunks:
                selected_parts.append(pending.chunks[section])

        if not selected_parts:
            available = list(pending.chunks.keys())
            return f"No matching sections found. Available: {available}"

        return "\n\n".join(selected_parts)

    def _detect_and_parse(self, text: str) -> tuple[str, dict[str, str]]:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return "json", self._parse_json_dict(data, text)
            if isinstance(data, list):
                return "json", self._parse_json_array(data)
        except (json.JSONDecodeError, ValueError):
            pass

        if re.search(r"(?:^|\n)#{1,6}\s", text):
            return "markdown", self._parse_markdown(text)

        return "text", self._parse_text(text)

    def _parse_json_dict(
        self, data: dict[str, object], raw_text: str, prefix: str = "", depth: int = 0
    ) -> dict[str, str]:
        chunks: dict[str, str] = {}
        for key, value in data.items():
            full_key = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
            if isinstance(value, dict) and depth < self._json_depth:
                nested = self._parse_json_dict(value, "", prefix=full_key, depth=depth + 1)
                chunks.update(nested)
            else:
                chunks[full_key] = json.dumps(value, ensure_ascii=False, indent=2)
        return chunks

    def _parse_json_array(self, data: list[object]) -> dict[str, str]:
        chunks: dict[str, str] = {}
        for i, item in enumerate(data):
            chunks[f"[{i}]"] = json.dumps(item, ensure_ascii=False, indent=2)
        return chunks

    def _parse_markdown(self, text: str) -> dict[str, str]:
        chunks: dict[str, str] = {}
        parts = re.split(r"(?:^|\n)(#{1,6}\s+.+)", text)
        current_heading = ""
        current_content: list[str] = []

        for part in parts:
            heading_match = re.match(r"^(#{1,6})\s+(.+)$", part.strip())
            if heading_match:
                if current_heading or current_content:
                    content = "\n".join(current_content).strip()
                    if content:
                        chunks[current_heading or "Preamble"] = content
                current_heading = heading_match.group(2).strip()
                current_content = []
            else:
                current_content.append(part)

        if current_heading or current_content:
            content = "\n".join(current_content).strip()
            if content:
                chunks[current_heading or "Preamble"] = content

        return chunks

    def _parse_text(self, text: str) -> dict[str, str]:
        paragraphs = re.split(r"\n\n+", text)
        chunks: dict[str, str] = {}
        for i, para in enumerate(paragraphs):
            stripped = para.strip()
            if stripped:
                chunks[f"Paragraph {i + 1}"] = stripped
        return chunks

    def _decompose_single_chunk(self, chunks: dict[str, str], fmt: str) -> dict[str, str]:
        if not chunks:
            return chunks
        key, value = next(iter(chunks.items()))
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return chunks
        if isinstance(parsed, dict) and len(parsed) > 1:
            return {
                f"{key}.{k}": json.dumps(v, ensure_ascii=False, indent=2) for k, v in parsed.items()
            }
        if isinstance(parsed, list) and len(parsed) > 1:
            return {
                f"{key}[{i}]": json.dumps(item, ensure_ascii=False, indent=2)
                for i, item in enumerate(parsed)
            }
        return chunks

    def _infer_type(self, key: str, content: str, fmt: str) -> str:
        if fmt == "json":
            if content.startswith("{"):
                return "object"
            if content.startswith("["):
                return "array"
            return "string"
        if fmt == "markdown":
            return "heading"
        return "paragraph"

    def _evict(self) -> None:
        self._store.evict_expired(self._ttl)
        self._store.evict_oldest(self._max_pending)


class FieldExtractCompressor:
    """JSON: preserve key structure + truncate values. Text: head + tail lines."""

    def compress(self, text: str, *, max_chars: int) -> str:
        if not text or len(text) <= max_chars:
            return text
        try:
            data = json.loads(text)
            return self._compress_json(data, max_chars)
        except (json.JSONDecodeError, ValueError):
            pass
        return self._compress_text(text, max_chars)

    def _compress_json(self, data: object, max_chars: int) -> str:
        if isinstance(data, dict):
            summary = self._extract_dict(data, max_chars)
            result = json.dumps(summary, ensure_ascii=False, indent=2)
        elif isinstance(data, list):
            preview_n = min(5, len(data))
            preview: list[object] = []
            for item in data[:preview_n]:
                if isinstance(item, dict):
                    preview.append(self._preview_dict(item))
                else:
                    preview.append(item)
            result = json.dumps(preview, ensure_ascii=False, indent=2)
            if len(data) > preview_n:
                result += f"\n... ({len(data)} items total, showing first {preview_n})"
        else:
            result = json.dumps(data, ensure_ascii=False)
        if len(result) > max_chars:
            result = result[:max_chars] + "\n... (truncated)"
        return result

    def _extract_dict(self, data: dict, budget: int) -> dict:
        """Budget-aware recursive dict extraction — preserves all keys with depth.

        Scalar/small values are placed first so they survive truncation.
        Large arrays/dicts are placed after, allowing them to be cut if needed.
        """
        # Separate scalar (small) values from large collections
        scalar_keys: list[str] = []
        collection_keys: list[str] = []
        for key, value in data.items():
            if isinstance(value, (list, dict)):
                collection_keys.append(key)
            else:
                scalar_keys.append(key)

        # Process scalars first (survive truncation), then collections
        summary: dict = {}
        for key in scalar_keys + collection_keys:
            value = data[key]
            if isinstance(value, str) and len(value) > 80:
                if "```" in value:
                    limit = min(len(value), 500)
                    fence_end = value.rfind("```", 0, limit)
                    summary[key] = (
                        value[: fence_end + 3] if fence_end > 80 else value[:limit] + "..."
                    )
                else:
                    summary[key] = value[:80] + "..."
            elif isinstance(value, list):
                preview_n = min(5, len(value))
                items: list[object] = []
                for item in value[:preview_n]:
                    if isinstance(item, str) and len(item) > 80:
                        items.append(item[:80] + "...")
                    elif isinstance(item, dict):
                        items.append(self._preview_dict(item))
                    elif isinstance(item, list):
                        items.append(f"[{len(item)} items]")
                    else:
                        items.append(item)
                remaining = len(value) - preview_n
                if remaining > 0:
                    items.append(f"... ({remaining} more)")
                summary[key] = items
            elif isinstance(value, dict):
                # Recurse one level — preserve all keys of nested dicts
                summary[key] = self._preview_dict(value)
            else:
                summary[key] = value
        return summary

    @staticmethod
    def _preview_dict(d: dict, max_keys: int = 6, max_value_len: int = 80) -> dict:
        """Show first N key-value pairs with truncated values, hint at rest."""
        preview: dict = {}
        keys = list(d.keys())
        for k in keys[:max_keys]:
            v = d[k]
            if isinstance(v, str) and len(v) > max_value_len:
                preview[k] = v[:max_value_len] + "..."
            elif isinstance(v, dict):
                # One more level of preview for nested dicts
                inner: dict = {}
                inner_keys = list(v.keys())
                for ik in inner_keys[:4]:
                    iv = v[ik]
                    if isinstance(iv, str) and len(iv) > 40:
                        inner[ik] = iv[:40] + "..."
                    elif isinstance(iv, (dict, list)):
                        inner[ik] = f"({type(iv).__name__}, {len(iv)})"
                    else:
                        inner[ik] = iv
                if len(inner_keys) > 4:
                    inner[f"...{len(inner_keys) - 4} more"] = "..."
                preview[k] = inner
            elif isinstance(v, list):
                if len(v) <= 3:
                    preview[k] = v
                else:
                    preview[k] = v[:3] + [f"... ({len(v) - 3} more)"]
            else:
                preview[k] = v
        remaining = len(keys) - max_keys
        if remaining > 0:
            preview[f"...{remaining} more"] = "..."
        return preview

    def _compress_text(self, text: str, max_chars: int) -> str:
        lines = text.split("\n")
        if len(lines) <= 10:
            return text[:max_chars] + "\n... (truncated)" if len(text) > max_chars else text
        head_count = max(3, len(lines) // 10)
        tail_count = max(3, len(lines) // 10)
        head = "\n".join(lines[:head_count])
        tail = "\n".join(lines[-tail_count:])
        omitted = len(lines) - head_count - tail_count
        summary = _content_summary(text)
        result = f"{head}\n... ({omitted} lines omitted){summary} ...\n{tail}"
        if len(result) > max_chars:
            result = result[:max_chars] + "\n... (truncated)"
        return result


class SchemaPruningCompressor:
    """JSON schema-preserving pruner — keeps ALL keys, limits values.

    Strategy: recursively walk JSON tree, preserving the full key structure.
    Arrays are sampled (first 2 + last 1 + count), strings are capped.
    This ensures every configuration field, every nested key, and every
    data relationship is represented in the output.
    """

    def __init__(self, max_string: int = 80, max_array_items: int = 3) -> None:
        self._max_string = max_string
        self._max_array = max_array_items

    def compress(self, text: str, *, max_chars: int) -> str:
        if not text or len(text) <= max_chars:
            return text
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return TruncateCompressor().compress(text, max_chars=max_chars)

        # Iteratively reduce detail until output fits budget
        for max_str in (self._max_string, 40, 20):
            pruned = self._prune(data, max_str=max_str)
            result = json.dumps(pruned, ensure_ascii=False, indent=2)
            if len(result) <= max_chars:
                return result

        # Final: minimal detail
        pruned = self._prune(data, max_str=10, max_array=2)
        result = json.dumps(pruned, ensure_ascii=False, indent=2)
        if len(result) > max_chars:
            result = result[:max_chars] + "\n... (pruned)"
        return result

    def _prune(self, data: object, max_str: int = 80, max_array: int | None = None) -> object:
        ma = max_array if max_array is not None else self._max_array
        if isinstance(data, dict):
            return {k: self._prune(v, max_str, ma) for k, v in data.items()}
        if isinstance(data, list):
            n = len(data)
            if n <= ma:
                return [self._prune(item, max_str, ma) for item in data]
            # First 2 + last 1 + count (preserves head and tail anomalies)
            head = [self._prune(data[i], max_str, ma) for i in range(min(2, n))]
            tail = [self._prune(data[-1], max_str, ma)]
            omitted = n - min(2, n) - 1
            return head + [f"... ({omitted} items omitted)"] + tail
        if isinstance(data, str) and len(data) > max_str:
            return data[:max_str] + "..."
        return data


class SkeletonCompressor:
    """Markdown skeleton — preserves ALL headings + structural lines.

    For documents with many parallel sections (API docs, changelogs),
    keeps the full document skeleton so no section is completely lost.
    Body content is aggressively trimmed to heading + first key line only.
    """

    _HEADING_RE = re.compile(r"^(#{1,6}\s.+)$", re.MULTILINE)

    def compress(self, text: str, *, max_chars: int) -> str:
        """Keep all headings + first content line per section."""
        if not text or len(text) <= max_chars:
            return text

        headings = list(self._HEADING_RE.finditer(text))
        if len(headings) < 2:
            return TruncateCompressor().compress(text, max_chars=max_chars)

        # Build sections: heading + first few meaningful content lines
        parts: list[str] = []
        budget_per_section = max(60, (max_chars - 80) // len(headings))

        for i, m in enumerate(headings):
            sec_start = m.start()
            sec_end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            section = text[sec_start:sec_end].rstrip()
            lines = section.split("\n")

            # Always keep the heading
            kept = [lines[0]]
            kept_chars = len(lines[0])

            # Add content lines until per-section budget
            for line in lines[1:]:
                stripped = line.strip()
                if not stripped:
                    continue
                # Prioritize: list items, table rows, HTTP methods, code fences
                if kept_chars + len(line) + 1 > budget_per_section:
                    break
                kept.append(line)
                kept_chars += len(line) + 1

            parts.append("\n".join(kept))

        result = "\n\n".join(parts)
        result += f"\n(skeleton — {len(text)} chars original)"

        if len(result) > max_chars:
            result = result[:max_chars]
        return result


class LLMCompressor:
    """Compress by asking an LLM to summarize the text."""

    _OPENAI_URL = "https://api.openai.com/v1/chat/completions"
    _ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
    _ANTHROPIC_VERSION = "2023-06-01"

    def __init__(self, config: LLMCompressorConfig) -> None:
        self._cfg = config
        self._cb = _CircuitBreaker(
            max_failures=3, reset_timeout=60.0, name=f"llm-{config.provider.value}"
        )
        self._client: httpx.AsyncClient | None = httpx.AsyncClient(timeout=30) if httpx else None

    async def compress(
        self, text: str, *, max_chars: int, privacy_patterns: list[str] | None = None
    ) -> str:
        if not text or len(text) <= max_chars:
            return text
        if privacy_patterns:
            from memtomem_stm.proxy.privacy import contains_sensitive_content

            if contains_sensitive_content(text, privacy_patterns):
                logger.info("Sensitive content detected, skipping LLM compression")
                return TruncateCompressor().compress(text, max_chars=max_chars)
        if self._cb.is_open:
            return TruncateCompressor().compress(text, max_chars=max_chars)
        try:
            result = await self._call_api(text, max_chars=max_chars)
            self._cb.success()
            return result
        except Exception as exc:
            self._cb.failure()
            logger.warning(
                "LLM compression failed (%s), falling back to truncate: %s", type(exc).__name__, exc
            )
            return TruncateCompressor().compress(text, max_chars=max_chars)

    async def _call_api(self, text: str, *, max_chars: int) -> str:
        system_prompt = self._cfg.system_prompt.format(max_chars=max_chars)
        match self._cfg.provider:
            case LLMProvider.OPENAI:
                return await self._openai(text, system_prompt)
            case LLMProvider.ANTHROPIC:
                return await self._anthropic(text, system_prompt)
            case LLMProvider.OLLAMA:
                return await self._ollama(text, system_prompt)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _openai(self, text: str, system_prompt: str) -> str:
        url = (
            self._cfg.base_url.rstrip("/") + "/v1/chat/completions"
            if self._cfg.base_url
            else self._OPENAI_URL
        )
        resp = await self._client.post(
            url,
            headers={"Authorization": f"Bearer {self._cfg.api_key}"},
            json={
                "model": self._cfg.model,
                "max_tokens": self._cfg.max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    async def _anthropic(self, text: str, system_prompt: str) -> str:
        url = (
            self._cfg.base_url.rstrip("/") + "/v1/messages"
            if self._cfg.base_url
            else self._ANTHROPIC_URL
        )
        resp = await self._client.post(
            url,
            headers={
                "x-api-key": self._cfg.api_key,
                "anthropic-version": self._ANTHROPIC_VERSION,
            },
            json={
                "model": self._cfg.model,
                "max_tokens": self._cfg.max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": text}],
            },
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"]

    async def _ollama(self, text: str, system_prompt: str) -> str:
        base = self._cfg.base_url or "http://localhost:11434"
        url = base.rstrip("/") + "/api/chat"
        resp = await self._client.post(
            url,
            json={
                "model": self._cfg.model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]


class HybridCompressor:
    """Head preserve + tail compress (TOC or truncate)."""

    _SEPARATOR_TEMPLATE = "\n---\nRemaining content ({remaining} chars) — Table of Contents:\n\n"
    _SEPARATOR_TRUNC_TEMPLATE = "\n---\nRemaining content ({remaining} chars, truncated):\n\n"

    def __init__(
        self,
        head_chars: int = 5000,
        tail_mode: TailMode = TailMode.TOC,
        min_toc_budget: int = 200,
        min_head_chars: int = 100,
        head_ratio: float = 0.6,
        selective_compressor: SelectiveCompressor | None = None,
    ) -> None:
        self._head_chars = head_chars
        self._tail_mode = tail_mode
        self._min_toc_budget = min_toc_budget
        self._min_head_chars = min_head_chars
        self._head_ratio = head_ratio
        self._selective = selective_compressor or SelectiveCompressor()

    @property
    def selective_compressor(self) -> SelectiveCompressor:
        return self._selective

    def compress(self, text: str, *, max_chars: int) -> str:
        if not text or len(text) <= max_chars:
            return text
        if len(text) <= self._head_chars:
            return text

        separator_overhead = 80
        _MIN_TAIL = 50
        available = max_chars - separator_overhead

        if available <= 0:
            return TruncateCompressor().compress(text, max_chars=max_chars)

        if self._head_chars + self._min_toc_budget <= available:
            head_budget = self._head_chars
        else:
            head_budget = max(self._min_head_chars, int(available * self._head_ratio))

        if head_budget > available or head_budget < self._min_head_chars:
            return TruncateCompressor().compress(text, max_chars=max_chars)

        if available - head_budget < _MIN_TAIL:
            return TruncateCompressor().compress(text, max_chars=max_chars)

        head_end = self._find_head_break(text, head_budget)
        head = text[:head_end]
        tail_text = text[head_end:]
        remaining = len(tail_text)

        if self._tail_mode == TailMode.TOC:
            separator = self._SEPARATOR_TEMPLATE.format(remaining=remaining)
        else:
            separator = self._SEPARATOR_TRUNC_TEMPLATE.format(remaining=remaining)

        toc_budget = max_chars - len(head) - len(separator)
        if toc_budget < _MIN_TAIL:
            return TruncateCompressor().compress(text, max_chars=max_chars)

        if self._tail_mode == TailMode.TOC:
            tail_compressed = self._selective.compress(tail_text, max_chars=toc_budget)
        else:
            tail_compressed = TruncateCompressor().compress(tail_text, max_chars=toc_budget)

        result = head + separator + tail_compressed
        if len(result) > max_chars:
            result = result[:max_chars]
        return result

    def _find_head_break(self, text: str, budget: int) -> int:
        floor = int(budget * 0.85)
        for i in range(budget, floor - 1, -1):
            if i + 1 < len(text) and text[i : i + 2] == "\n\n":
                return i
            if i >= 2 and text[i - 2 : i] == "\n\n":
                return i - 2
        for i in range(budget, floor - 1, -1):
            if text[i - 1] in ".!?\n" and (i >= len(text) or text[i] in " \n\t"):
                return i
        for i in range(budget, floor - 1, -1):
            if i < len(text) and text[i] in " \n\t":
                return i
        return budget


def get_compressor(strategy: CompressionStrategy) -> Compressor:
    """Factory for sync compressor instances (excludes LLM_SUMMARY, SELECTIVE, HYBRID)."""
    match strategy:
        case CompressionStrategy.NONE:
            return NoopCompressor()
        case CompressionStrategy.TRUNCATE:
            return TruncateCompressor()
        case CompressionStrategy.EXTRACT_FIELDS:
            return FieldExtractCompressor()
        case CompressionStrategy.SCHEMA_PRUNING:
            return SchemaPruningCompressor()
        case CompressionStrategy.SKELETON:
            return SkeletonCompressor()
        case _:
            return TruncateCompressor()


def _json_depth(data: object, _current: int = 0) -> int:
    """Measure max nesting depth of a JSON structure."""
    if isinstance(data, dict):
        if not data:
            return _current + 1
        return max(_json_depth(v, _current + 1) for v in data.values())
    if isinstance(data, list):
        if not data:
            return _current + 1
        return max(_json_depth(v, _current + 1) for v in data[:5])  # sample first 5
    return _current


def auto_select_strategy(text: str, *, max_chars: int = 0) -> CompressionStrategy:
    """Detect content type and return the best compression strategy.

    Principle: information preservation > compression ratio.
    If a pattern is not recognized, prefer NONE (passthrough after cleaning)
    over aggressive compression that may destroy information.

    Args:
        text: cleaned content to analyze
        max_chars: budget hint (0 = unknown). When cleaning already fits
                   within budget, returns NONE to skip compression entirely.
    """
    stripped = text.strip()
    if not stripped:
        return CompressionStrategy.NONE

    # If content already fits within budget after cleaning → passthrough
    if max_chars > 0 and len(stripped) <= max_chars:
        return CompressionStrategy.NONE

    # JSON detection
    if stripped[0] in "{[":
        try:
            data = json.loads(stripped)
            if isinstance(data, list) and len(data) >= 20:
                return CompressionStrategy.SCHEMA_PRUNING
            if isinstance(data, dict):
                arrays = [v for v in data.values() if isinstance(v, list) and len(v) >= 20]
                if arrays:
                    return CompressionStrategy.SCHEMA_PRUNING
                # Dict-of-dicts (config-like): many top-level keys with nested structure
                # → extract_fields preserves all keys vs truncate losing bottom half
                nested = sum(1 for v in data.values() if isinstance(v, (dict, list)))
                if nested >= 3:
                    return CompressionStrategy.EXTRACT_FIELDS
            return CompressionStrategy.TRUNCATE
        except (json.JSONDecodeError, ValueError):
            pass

    # Markdown detection
    heading_count = len(re.findall(r"(?:^|\n)#{1,6}\s", stripped))

    if heading_count >= 4:
        # Skeleton for API-docs with HTTP method endpoints
        has_http_methods = bool(re.search(r"(?:POST|GET|PUT|DELETE|PATCH)\s+/", stripped))
        if has_http_methods:
            return CompressionStrategy.SKELETON

        # Large docs with substantial sections → hybrid
        if heading_count >= 5 and len(stripped) >= 5000:
            return CompressionStrategy.HYBRID

    # Code-heavy content — HYBRID only for large code files
    fence_count = stripped.count("```")
    if fence_count >= 6 and len(stripped) >= 5000:
        return CompressionStrategy.HYBRID

    return CompressionStrategy.TRUNCATE
