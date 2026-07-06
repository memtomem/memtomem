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


def test_heading_boost_is_at_least_3x():
    """The heading boost is 3x, not merely +1.

    Body carries "gamma" twice (frequency 2). A heading-only word "delta"
    scores purely from the boost, so at 3x it reaches 3 and must outrank
    "gamma". A boost of 1x (score 1) or 2x (score 2, tied) would leave
    "delta" behind or tied, so ranking it strictly first pins the
    multiplier at >= 3. No tie is relied on (3 vs 2).
    """
    tags = extract_tags_keyword("gamma gamma", max_tags=5, heading_hierarchy=("delta",))
    assert tags.index("delta") < tags.index("gamma")


def test_heading_boost_outranks_more_frequent_body_word():
    """A heading word must outrank a body word that occurs MORE often.

    Body carries "alpha" twice (frequency 2) and "beta" once (frequency 1).
    With the 3x boost "beta" scores 1 + 3 = 4 and ranks ahead of "alpha" (2);
    without the boost the order would reverse. This is the real contract the
    original test only gestured at (it asserted mere presence, which passes
    even with no boost at all). No tie is relied on (4 vs 2).
    """
    tags = extract_tags_keyword("alpha alpha beta", max_tags=5, heading_hierarchy=("beta",))
    assert tags.index("beta") < tags.index("alpha")


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
