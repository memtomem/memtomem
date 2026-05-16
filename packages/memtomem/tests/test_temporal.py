"""Tests for temporal queries — timeline and activity summary."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.models import Chunk, ChunkMetadata
from memtomem.tools.temporal import (
    ActivityDay,
    TimelineBucket,
    build_timeline,
    format_activity,
    format_timeline,
)


def _make_chunk(content="test", tags=(), namespace="default", source="test.md", created_at=None):
    chunk = Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(f"/tmp/{source}"),
            tags=tuple(tags),
            namespace=namespace,
        ),
        content_hash=f"hash-{uuid4().hex[:8]}",
        embedding=[0.1] * 1024,
    )
    if created_at:
        object.__setattr__(chunk, "created_at", created_at)
    return chunk


# ── build_timeline ───────────────────────────────────────────────────


class TestBuildTimeline:
    def test_empty(self):
        assert build_timeline([]) == []

    def test_weekly_grouping(self):
        chunks = [
            {
                "content": "First",
                "created_at": "2025-01-06T10:00:00+00:00",
                "source_file": "notes.md",
                "tags": ["auth"],
            },
            {
                "content": "Second",
                "created_at": "2025-01-07T10:00:00+00:00",
                "source_file": "notes.md",
                "tags": ["jwt"],
            },
            {
                "content": "Third",
                "created_at": "2025-01-14T10:00:00+00:00",
                "source_file": "design.md",
                "tags": ["auth"],
            },
        ]
        buckets = build_timeline(chunks, granularity="week")
        assert len(buckets) == 2  # two different weeks
        assert buckets[0].chunk_count == 2
        assert buckets[1].chunk_count == 1

    def test_monthly_grouping(self):
        chunks = [
            {
                "content": "Jan work",
                "created_at": "2025-01-15T00:00:00+00:00",
                "source_file": "a.md",
                "tags": [],
            },
            {
                "content": "Feb work",
                "created_at": "2025-02-15T00:00:00+00:00",
                "source_file": "b.md",
                "tags": [],
            },
            {
                "content": "Mar work",
                "created_at": "2025-03-15T00:00:00+00:00",
                "source_file": "c.md",
                "tags": [],
            },
        ]
        buckets = build_timeline(chunks, granularity="month")
        assert len(buckets) == 3
        assert buckets[0].period_label == "2025-01"
        assert buckets[2].period_label == "2025-03"

    def test_auto_granularity_short_span(self):
        # Less than 90 days → weekly
        chunks = [
            {
                "content": "A",
                "created_at": "2025-03-01T00:00:00+00:00",
                "source_file": "a.md",
                "tags": [],
            },
            {
                "content": "B",
                "created_at": "2025-03-15T00:00:00+00:00",
                "source_file": "b.md",
                "tags": [],
            },
        ]
        buckets = build_timeline(chunks, granularity="auto")
        # Should use weekly granularity for short span
        assert all("W" in b.period_label for b in buckets)

    def test_auto_granularity_long_span(self):
        # More than 90 days → monthly
        chunks = [
            {
                "content": "A",
                "created_at": "2025-01-01T00:00:00+00:00",
                "source_file": "a.md",
                "tags": [],
            },
            {
                "content": "B",
                "created_at": "2025-06-01T00:00:00+00:00",
                "source_file": "b.md",
                "tags": [],
            },
        ]
        buckets = build_timeline(chunks, granularity="auto")
        assert all("-" in b.period_label and "W" not in b.period_label for b in buckets)

    def test_sources_and_topics(self):
        chunks = [
            {
                "content": "Auth design",
                "created_at": "2025-01-10T00:00:00+00:00",
                "source_file": "/home/user/notes/auth.md",
                "tags": ["oauth", "security"],
            },
        ]
        buckets = build_timeline(chunks, granularity="week")
        assert "auth.md" in buckets[0].sources
        assert "oauth" in buckets[0].key_topics


# ── format_timeline ──────────────────────────────────────────────────


class TestFormatTimeline:
    def test_no_results(self):
        result = format_timeline("test", [])
        assert "No memories found" in result

    def test_formatting(self):
        buckets = [
            TimelineBucket(
                period_label="2025-01",
                period_start="2025-01-01",
                period_end="2025-01-31",
                chunk_count=3,
                sources=["notes.md"],
                key_topics=["auth"],
                sample_content="Initial auth design",
            ),
        ]
        result = format_timeline("authentication", buckets)
        assert "Timeline" in result
        assert "authentication" in result
        assert "2025-01" in result
        assert "3 memories" in result


# ── format_activity ──────────────────────────────────────────────────


class TestFormatActivity:
    def test_no_data(self):
        result = format_activity([], "2025-03-01", "2025-03-15")
        assert "No activity found" in result

    def test_formatting(self):
        days = [
            ActivityDay(date="2025-03-01", created=3, updated=1, accessed=12),
            ActivityDay(date="2025-03-02", created=0, updated=2, accessed=8),
        ]
        result = format_activity(days, "2025-03-01", "2025-03-02")
        assert "Memory Activity" in result
        assert "2025-03-01" in result
        assert "Totals: 3 created, 3 updated, 20 accessed" in result


# ── Storage: get_activity_summary ────────────────────────────────────


class TestActivitySummaryStorage:
    @pytest.mark.asyncio
    async def test_counts(self, storage):
        now = datetime.now(timezone.utc)
        c1 = _make_chunk("chunk1", source="a.md")
        c2 = _make_chunk("chunk2", source="b.md")
        await storage.upsert_chunks([c1, c2])

        summary = await storage.get_activity_summary()
        today = now.strftime("%Y-%m-%d")
        today_data = [d for d in summary if d["date"] == today]
        assert len(today_data) == 1
        assert today_data[0]["created"] == 2

    @pytest.mark.asyncio
    async def test_empty_range(self, storage):
        summary = await storage.get_activity_summary(since="2020-01-01", until="2020-01-02")
        assert len(summary) == 0
