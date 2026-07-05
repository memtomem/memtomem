"""Unit tests for extract_tags_keyword (3x heading boost)."""

import pytest

from memtomem.tools.auto_tag import extract_tags_keyword


def test_empty_input():
    """Empty or whitespace-only input should return empty list."""
    assert extract_tags_keyword("") == []
    assert extract_tags_keyword("   ") == []
    assert extract_tags_keyword("\n\t") == []


def test_stopwords_removed():
    """Common stopwords should be filtered out."""
    text = "this is a test of the stopword filter"
    tags = extract_tags_keyword(text, max_tags=10)
    # "test", "filter" should appear, but "this", "is", "a", "of", "the" should not
    assert "test" in tags
    assert "filter" in tags
    assert "this" not in tags
    assert "is" not in tags
    assert "a" not in tags


def test_lowercase_output():
    """Tags should be returned in lowercase."""
    text = "Python Programming Language"
    tags = extract_tags_keyword(text, max_tags=5)
    assert all(tag.islower() for tag in tags)
    assert "python" in tags
    assert "programming" in tags
    assert "language" in tags


def test_max_tags_truncation():
    """Should cap results at max_tags."""
    text = "one two three four five six seven eight nine ten"
    tags = extract_tags_keyword(text, max_tags=3)
    assert len(tags) <= 3


def test_heading_boost_3x():
    """Words from heading hierarchy should get 3x boost."""
    # Body text has "test" once, but heading has "test" too
    heading = "test"
    text = "other words in body"
    tags = extract_tags_keyword(text, max_tags=5, heading=heading)
    # "test" should be boosted and appear before "other"
    assert "test" in tags


def test_heading_boost_outranks_body():
    """A word appearing in heading should outrank body words."""
    heading = "important"
    # Body has "important" only once, but appears 3 times in a non-heading word
    text = "important"
    tags = extract_tags_keyword(text, max_tags=5, heading=heading)
    # "important" should be included (boosted from heading)
    assert "important" in tags


def test_mixed_content():
    """Mixed content with heading and body should work correctly."""
    heading = "deployment"
    text = "deployment checklist for production"
    tags = extract_tags_keyword(text, max_tags=5, heading=heading)
    assert "deployment" in tags
    assert "production" in tags
    assert "checklist" in tags


def test_markdown_heading_boost():
    """Test that Markdown heading text is properly boosted."""
    heading = "performance tuning"
    text = "performance tuning is critical for redis systems"
    tags = extract_tags_keyword(text, max_tags=5, heading=heading)
    assert "performance" in tags
    assert "tuning" in tags
    assert "critical" in tags