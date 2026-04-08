"""Tests for URL fetcher and HTML→Markdown conversion."""

from memtomem.indexing.url_fetcher import _html_to_markdown, _url_to_slug


class TestHtmlToMarkdown:
    def test_headings(self):
        md = _html_to_markdown("<h1>Title</h1><h2>Sub</h2><h3>Deep</h3>")
        assert "# Title" in md
        assert "## Sub" in md
        assert "### Deep" in md

    def test_bold_italic(self):
        md = _html_to_markdown("<strong>bold</strong> and <em>italic</em>")
        assert "**bold**" in md
        assert "*italic*" in md

    def test_links(self):
        md = _html_to_markdown('<a href="https://example.com">Click</a>')
        assert "[Click](https://example.com)" in md

    def test_code_blocks(self):
        md = _html_to_markdown("<pre><code>const x = 1;</code></pre>")
        assert "const x = 1;" in md

    def test_inline_code(self):
        md = _html_to_markdown("Use <code>npm install</code> to install")
        assert "`npm install`" in md

    def test_lists(self):
        md = _html_to_markdown("<ul><li>First</li><li>Second</li></ul>")
        assert "- First" in md
        assert "- Second" in md

    def test_removes_script_style(self):
        md = _html_to_markdown(
            "<script>alert('xss')</script><style>body{}</style><p>Content</p>"
        )
        assert "alert" not in md
        assert "body{}" not in md
        assert "Content" in md

    def test_removes_nav_footer(self):
        md = _html_to_markdown("<nav>Nav</nav><p>Body</p><footer>Foot</footer>")
        assert "Nav" not in md
        assert "Foot" not in md
        assert "Body" in md

    def test_paragraphs(self):
        md = _html_to_markdown("<p>First para</p><p>Second para</p>")
        assert "First para" in md
        assert "Second para" in md


class TestUrlToSlug:
    def test_basic_url(self):
        assert _url_to_slug("https://example.com/page") == "example-com-page"

    def test_strips_protocol(self):
        slug = _url_to_slug("https://docs.example.com/api/v2")
        assert "https" not in slug

    def test_max_length(self):
        long_url = "https://example.com/" + "a" * 200
        assert len(_url_to_slug(long_url)) <= 80

    def test_empty_fallback(self):
        assert _url_to_slug("https://") == "fetched"
