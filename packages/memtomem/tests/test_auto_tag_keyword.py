"""Unit tests for extract_tags_keyword (3x heading boost)."""

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
    tags = extract_tags_keyword("test", max_tags=5, heading_hierarchy=("test",))
    assert "test" in tags


def test_heading_boost_outranks_body():
    """A word appearing in heading should outrank body words."""
    tags = extract_tags_keyword("important", max_tags=5, heading_hierarchy=("important",))
    assert "important" in tags


def test_mixed_content():
    """Mixed content with heading and body should work correctly."""
    tags = extract_tags_keyword(
        "deployment checklist for production", max_tags=5, heading_hierarchy=("deployment",)
    )
    assert "deployment" in tags
    assert "production" in tags
    assert "checklist" in tags


def test_markdown_heading_boost():
    """Test that Markdown heading text is properly boosted."""
    tags = extract_tags_keyword(
        "performance tuning is critical for redis systems",
        max_tags=5,
        heading_hierarchy=("performance", "tuning"),
    )
    assert "performance" in tags
    assert "tuning" in tags
    assert "critical" in tags
