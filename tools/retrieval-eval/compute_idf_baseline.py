#!/usr/bin/env python3
"""B.2 v2 query IDF + body-overlap pre-measurement.

Runs BEFORE `measure_sensitivity.py` for any new topic. Enforces the
Phase 2c-established rule: "target body overlap < 0.5 for all
genre-primary queries; if ≥ 0.5, flag in ledger" (see
`b2-v2-phase1-validation.md` § 11.5).

Outputs for each (language, query_set):
- token count and IDF sum per query (matched against caching baseline)
- body overlap ratio per query against its target-genre fixture body

Usage:
    uv run python tools/retrieval-eval/compute_idf_baseline.py

Reports both caching baseline (reference) and all registered query
sets. Hand-edit the `QUERY_SETS` dict at the top to add new topic
queries for pre-check.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from pathlib import Path


FIXTURE_ROOT = Path("packages/memtomem/tests/fixtures/corpus_v2")
GENRES = ("runbook", "postmortem", "adr", "troubleshooting")

# Caching baseline — produced by running this script against
# caching+postgres (64 chunks per language). These targets anchor all
# subsequent topics' query design.
#   KO: mean tokens 6.8, idf_sum 14.91
#   EN: mean tokens 7.5, idf_sum 14.16
# Target range = baseline ± 15%.
BASELINE_TARGET = {
    "ko": {"tokens": (5.7, 7.8), "idf_sum": (12.67, 17.14)},
    "en": {"tokens": (6.4, 8.6), "idf_sum": (12.04, 16.28)},
}

# Genre anchor vocabulary — tokens excluded from "topic-specific"
# overlap counting. Queries mix genre anchors (these) with
# topic-specific proper nouns; only the topic-specific tokens'
# overlap with body content is a confound.
GENRE_ANCHORS = {
    "ko": {
        # runbook
        "절차",
        "수행",
        "접속",
        "설정",
        "실행",
        # postmortem
        "후속",
        "조치",
        "원인",
        "kst",
        "장애",
        # adr
        "대신",
        "채택",
        "결정",
        "감수",
        "trade",
        # troubleshooting
        "증상",
        "의심",
        "점검",
        "진단",
        "만약",
    },
    "en": {
        # runbook
        "configure",
        "run",
        "verify",
        "steps",
        "inspect",
        "command",
        "set",
        # postmortem
        "at",
        "utc",
        "root",
        "cause",
        "follow",
        "up",
        "was",
        # adr
        "chose",
        "over",
        "accepted",
        "revisit",
        "re",
        "evaluate",
        "our",
        # troubleshooting
        "likely",
        "workaround",
        "symptom",
        "as",
        "or",
    },
}

# Query sets for pre-measurement. Keep structure parallel to
# `measure_sensitivity.py` QUERIES dict. Caching entry is the
# baseline — its measured values define the target range above.
QUERY_SETS: dict[str, dict[str, list[tuple[str, str]]]] = {
    "caching (baseline)": {
        "ko": [
            ("Redis maxmemory-policy allkeys-lru 절차 수행 접속", "runbook"),
            ("Redis eviction 후속 조치 KST 원인", "postmortem"),
            ("Redis Cluster 대신 채택 결정 trade-off", "adr"),
            ("Redis stampede 증상 의심 점검 진단", "troubleshooting"),
        ],
        "en": [
            ("Redis maxmemory-policy configure run verify steps", "runbook"),
            ("Redis eviction at UTC root cause follow-up", "postmortem"),
            ("Redis Cluster chose over accepted revisit re-evaluate", "adr"),
            ("Redis stampede likely root cause workaround symptom", "troubleshooting"),
        ],
    },
    "postgres (simple, canonical)": {
        "ko": [
            ("postgres 절차 접속 CONFIG SET 수행", "runbook"),
            ("postgres KST 원인 후속 조치 장애", "postmortem"),
            ("postgres 대신 채택 결정 trade-off 감수", "adr"),
            ("postgres 증상 의심 만약 점검 진단", "troubleshooting"),
        ],
        "en": [
            ("postgres configure run verify inspect command", "runbook"),
            ("postgres at UTC root cause follow-up", "postmortem"),
            ("postgres chose over accepted re-evaluate revisit", "adr"),
            ("postgres likely root cause workaround symptom", "troubleshooting"),
        ],
    },
    "cost_optimization (simple, canonical)": {
        "ko": [
            ("cost 절차 접속 CONFIG SET 수행", "runbook"),
            ("cost KST 원인 후속 조치 장애", "postmortem"),
            ("cost 대신 채택 결정 trade-off 감수", "adr"),
            ("cost 증상 의심 만약 점검 진단", "troubleshooting"),
        ],
        "en": [
            ("cost configure run verify inspect command", "runbook"),
            ("cost at UTC root cause follow-up", "postmortem"),
            ("cost chose over accepted re-evaluate revisit", "adr"),
            ("cost likely root cause workaround symptom", "troubleshooting"),
        ],
    },
    "security (simple, canonical)": {
        "ko": [
            ("security 절차 접속 CONFIG SET 수행", "runbook"),
            ("security KST 원인 후속 조치 장애", "postmortem"),
            ("security 대신 채택 결정 trade-off 감수", "adr"),
            ("security 증상 의심 만약 점검 진단", "troubleshooting"),
        ],
        "en": [
            ("security configure run verify inspect command", "runbook"),
            ("security at UTC root cause follow-up", "postmortem"),
            ("security chose over accepted re-evaluate revisit", "adr"),
            ("security likely root cause workaround symptom", "troubleshooting"),
        ],
    },
    "observability (simple, canonical)": {
        "ko": [
            ("observability 절차 접속 CONFIG SET 수행", "runbook"),
            ("observability KST 원인 후속 조치 장애", "postmortem"),
            ("observability 대신 채택 결정 trade-off 감수", "adr"),
            ("observability 증상 의심 만약 점검 진단", "troubleshooting"),
        ],
        "en": [
            ("observability configure run verify inspect command", "runbook"),
            ("observability at UTC root cause follow-up", "postmortem"),
            ("observability chose over accepted re-evaluate revisit", "adr"),
            ("observability likely root cause workaround symptom", "troubleshooting"),
        ],
    },
    "k8s (simple, canonical)": {
        # KO uses "kubernetes" instead of "k8s" because kiwi tokenizer
        # drops alphanumeric abbreviations with digits ("k8s" → nothing
        # that passes the len >= 2 filter). Fixture bodies include
        # "Kubernetes" in each KO chunk for body-overlap compatibility.
        # EN keeps "k8s" (regex tokenizer handles alphanumerics fine).
        "ko": [
            ("kubernetes 절차 접속 CONFIG SET 수행", "runbook"),
            ("kubernetes KST 원인 후속 조치 장애", "postmortem"),
            ("kubernetes 대신 채택 결정 trade-off 감수", "adr"),
            ("kubernetes 증상 의심 만약 점검 진단", "troubleshooting"),
        ],
        "en": [
            ("k8s configure run verify inspect command", "runbook"),
            ("k8s at UTC root cause follow-up", "postmortem"),
            ("k8s chose over accepted re-evaluate revisit", "adr"),
            ("k8s likely root cause workaround symptom", "troubleshooting"),
        ],
    },
}


def parse_chunks(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    chunks: list[str] = []
    current: list[str] = []
    in_chunk = False
    for line in text.splitlines():
        if line.startswith("## "):
            if current:
                chunks.append("\n".join(current).strip())
                current = []
            in_chunk = True
            continue
        if in_chunk:
            if line.startswith("<!--"):
                continue
            current.append(line)
    if current:
        chunks.append("\n".join(current).strip())
    return [c for c in chunks if c]


def tokenize_en(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-zA-Z0-9_]+", text.lower()) if len(t) >= 2]


def tokenize_ko(text: str) -> list[str]:
    from memtomem.storage.fts_tokenizer import _kiwi_tokenize

    return [t for t in _kiwi_tokenize(text) if len(t) >= 2]


def build_idf(lang: str, topics: list[str]) -> tuple[dict[str, float], int]:
    tokenize = tokenize_en if lang == "en" else tokenize_ko
    root = FIXTURE_ROOT / lang
    all_chunks: list[set[str]] = []
    for topic in topics:
        for genre in GENRES:
            path = root / topic / f"{genre}.md"
            if not path.exists():
                continue
            for c in parse_chunks(path):
                all_chunks.append(set(tokenize(c)))
    n = len(all_chunks)
    df: dict[str, int] = defaultdict(int)
    for s in all_chunks:
        for t in s:
            df[t] += 1
    return {t: math.log(n / d) for t, d in df.items()}, n


def report_idf(
    label: str,
    queries: list[tuple[str, str]],
    lang: str,
    idf: dict[str, float],
    n_chunks: int,
) -> tuple[float, float]:
    tokenize = tokenize_en if lang == "en" else tokenize_ko
    default_idf = math.log(max(n_chunks, 2))
    toks_list: list[int] = []
    idfs_list: list[float] = []
    print(f"\n  [{label}]")
    for q, genre in queries:
        t = tokenize(q)
        idf_sum = sum(idf.get(x, default_idf) for x in t)
        toks_list.append(len(t))
        idfs_list.append(idf_sum)
        print(f"    {genre:16s} tokens={len(t):2d}  idf_sum={idf_sum:6.2f}  {q}")
    m_t = sum(toks_list) / len(toks_list)
    m_i = sum(idfs_list) / len(idfs_list)
    print(f"    MEAN              tokens={m_t:4.1f}   idf_sum={m_i:6.2f}")
    return m_t, m_i


def report_body_overlap(
    label: str,
    queries: list[tuple[str, str]],
    topic: str,
    lang: str,
) -> None:
    tokenize = tokenize_en if lang == "en" else tokenize_ko
    anchors = GENRE_ANCHORS[lang]
    root = FIXTURE_ROOT / lang / topic
    if not root.exists():
        print(f"  [{label}] skipped — fixture missing at {root}")
        return

    print(f"\n  [{label}] body overlap vs {topic}/{lang}")
    print(
        f"  {'genre':16} {'q_tokens':9} {'topic_tok':10} "
        f"{'target_body':12} {'other_body':11} overlap_ratio"
    )
    flagged = []
    for q, target_genre in queries:
        q_toks = set(tokenize(q))
        topic_toks = q_toks - anchors
        target_body = set(tokenize(" ".join(parse_chunks(root / f"{target_genre}.md"))))
        other_bodies: set[str] = set()
        for g in GENRES:
            if g == target_genre:
                continue
            other_bodies.update(tokenize(" ".join(parse_chunks(root / f"{g}.md"))))
        target_overlap = topic_toks & target_body
        other_overlap = topic_toks & other_bodies
        ratio = len(target_overlap) / max(len(topic_toks), 1)
        marker = "  *FLAG*" if ratio >= 0.5 else ""
        if ratio >= 0.5:
            flagged.append((target_genre, ratio))
        print(
            f"  {target_genre:16} {len(q_toks):9d} {len(topic_toks):10d} "
            f"{len(target_overlap):12d} {len(other_overlap):11d} {ratio:.2f}{marker}"
        )
    if flagged:
        print(
            f"  ⚠ {len(flagged)} queries ≥ 0.5 overlap — flag in ledger "
            "(Phase 2c rule, phase1-validation § 11.5)"
        )


def check_range(value: float, rng: tuple[float, float], label: str) -> None:
    lo, hi = rng
    status = "OK" if lo <= value <= hi else "OUT"
    mid = (lo + hi) / 2
    pct = (value - mid) / mid * 100
    print(f"    {label}: {value:.2f} (range {lo}-{hi}) [{status}, {pct:+.1f}%]")


def main() -> None:
    print("=" * 72)
    print("B.2 v2 — QUERY IDF + BODY OVERLAP PRE-MEASUREMENT")
    print("=" * 72)
    print("Baseline corpus: caching + postgres (64 chunks per language)")
    print("Target fairness range: caching mean ± 15% (see top-of-file constants)")

    for lang in ("ko", "en"):
        idf, n = build_idf(lang, ["caching", "postgres"])
        print(
            f"\n========== {lang.upper()} IDF baseline (N={n} chunks, {len(idf)} tokens) =========="
        )
        for label, qs_by_lang in QUERY_SETS.items():
            queries = qs_by_lang.get(lang)
            if not queries:
                continue
            m_t, m_i = report_idf(label, queries, lang, idf, n)
            target = BASELINE_TARGET[lang]
            print("  Range check (vs caching baseline ± 15%):")
            check_range(m_t, target["tokens"], "mean tokens")
            check_range(m_i, target["idf_sum"], "mean idf_sum")

    print("\n" + "=" * 72)
    print("BODY OVERLAP (topic-token overlap with target-genre body)")
    print("=" * 72)
    print("Rule: overlap < 0.5 target; ≥ 0.5 flagged")

    for lang in ("ko", "en"):
        for label, qs_by_lang in QUERY_SETS.items():
            queries = qs_by_lang.get(lang)
            if not queries:
                continue
            # Derive topic name from label (strip suffix after first space)
            topic = label.split(" ")[0]
            if topic == "caching":
                # caching queries target caching corpus
                pass
            elif topic in ("postgres", "cost_optimization", "security", "observability", "k8s"):
                pass
            else:
                continue
            report_body_overlap(label, queries, topic, lang)


if __name__ == "__main__":
    main()
