from unittest.mock import AsyncMock

import pytest

from memtomem import privacy
from memtomem.config import IndexingConfig
from memtomem.indexing.engine import IndexEngine
from memtomem.indexing import privacy_projection
from memtomem.indexing.privacy_projection import MARKER, prepare_index_content


#: Value shapes whose end this module must refuse to name. Assembled rather
#: than written inline so the nested quoting stays readable.
_S = "hunter2-very-secret"
QUOTED_KEY_TRIPLE = '{"password": """' + _S + '"""}\n'
QUOTED_KEY_TRIPLE_SINGLE = "{'password': '''" + _S + "'''}\n"
ADJACENT_CONCAT = 'password = "" "' + _S + '"\n'
YAML_DOUBLED_QUOTE = "password: 'first''" + _S + "'\n"
PYTHON_PLUS_CONCAT = '{"password": "a" + "' + _S + '"}\n'
BLOCK_SCALAR_COMMENT = "password: | # production\n  " + _S + "\n"
PLAIN_CONTINUATION = "password: first\n  " + _S + "\n"
# A shell word does not end at a quote: all three of these are one argument.
SHELL_COMMA = 'password="first",' + _S + "\n"
SHELL_HASH = 'password="first"#' + _S + "\n"
SHELL_BACKTICK = 'password="first"`printf ' + _S + "`\n"
# A blank line folds into a YAML plain scalar as a newline; it does not end it.
BLANK_LINE_FOLD = "password: first\n\n  " + _S + "\n"
BLANK_LINE_FOLD_CRLF = "password: first\r\n\r\n  " + _S + "\r\n"
# Valid Python whose value is on the next line.
PYTHON_PAREN = 'password = (\n"' + _S + '"\n)\n'
PYTHON_LIST = 'password = [\n"' + _S + '"\n]\n'
# Python joins adjacent string literals across lines with no operator at all,
# so this dict's value is "first" + the secret. Indentation says nothing here.
PYTHON_IMPLICIT_CONCAT = '{"password": "first"\n"' + _S + '"}\n'
# A YAML *flow* mapping has no indentation rule: the plain scalar continues on
# the next line at equal indentation.
YAML_FLOW_CONTINUATION = "{password: first\n" + _S + "}\n"
# A code span delimited by three backticks contains single backticks as literal
# text, so backtick parity does not find its end.
MULTI_BACKTICK_SPAN = "Example: ```password=first`" + _S + "```\n"


@pytest.fixture
def projecting(monkeypatch):
    """Turn the index-only projection on for one test.

    It ships disabled (see ``privacy_projection``'s module docstring: the
    value-boundary question is not answerable without the enclosing grammar).
    The grammar below it is still worth keeping under test — it is what a
    parser-backed version would reuse — so the tests that exercise it say so
    explicitly rather than depending on a default that is deliberately off.
    """
    monkeypatch.setattr(privacy_projection, "PROJECTION_ENABLED", True)


@pytest.mark.parametrize(
    "text",
    [
        '# Example: `"password": "secret"` and `password: required`\n',
        'API_KEY="a private value"\n',
        "password: some private words\n",
        "{'password': 'value-with-escaped-\\'quote'}\n",
    ],
)
def test_masked_values_never_reach_guard(text, projecting):
    projection = prepare_index_content(text)
    assert projection.redaction_count > 0
    assert "[REDACTED]" in projection.content
    assert not privacy.scan(projection.guard_content)
    assert projection.content.count("\n") == text.count("\n")
    assert privacy.scan(text)  # raw scanner contract remains strict
    repeated = prepare_index_content(projection.content)
    assert repeated.content == projection.content
    assert not privacy.scan(repeated.guard_content)


@pytest.mark.parametrize(
    "text",
    [
        "`env -u OPENAI_API_KEY OPENAI_API_KEY= nbconvert`",
        "`password:` is a label",
        'api_key=""\n',
    ],
)
def test_empty_values_and_bare_examples(text, projecting):
    projection = prepare_index_content(text)
    assert projection.content == text
    assert projection.redaction_count == 0
    assert not privacy.scan(projection.guard_content)


@pytest.mark.parametrize(
    "text",
    [
        '"password": "unterminated\n',
        "password: `unknown syntax`",
        '"password": "nested password: other"',
        "password=demo\n-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----\n",
    ],
)
def test_ambiguous_or_specific_material_remains_blocked(text, projecting):
    projection = prepare_index_content(text)
    assert projection.content == projection.guard_content == text
    assert privacy.scan(projection.guard_content)


def test_shared_scope_never_acquires_an_exception(projecting):
    text = '"password": "secret"'
    projection = prepare_index_content(text, scope="project_shared")
    assert projection.guard_content == text
    assert (
        privacy.enforce_write_guard(
            projection.guard_content, surface="test", scope="project_shared", record_outcome=False
        ).decision
        == "blocked"
    )


@pytest.mark.asyncio
async def test_file_projection_persists_but_source_and_ingress_guard_stay_strict(
    storage, tmp_path, projecting
):
    path = tmp_path / "example.md"
    original = '# Example\n\n`"password": "a private value"`\n'
    path.write_text(original)
    embedder = AsyncMock()
    embedder.dimension = 0
    embedder.model_name = "none"
    engine = IndexEngine(storage, embedder, IndexingConfig(memory_dirs=[tmp_path]))
    # ``pass``, and that is the problem this feature has to solve before it can
    # ship enabled: the guard is handed text with the accounted hits already
    # erased, so it cannot tell a masked file from a clean one. Nothing on this
    # branch reports the difference — a distinct decision value and the counters
    # that hang off it belong with the parser-backed version, since neither can
    # fire while ``PROJECTION_ENABLED`` is False.
    assert engine.preview_redaction_decision(path, original) == "pass"
    result = await engine.index_file(path)
    assert not result.errors
    chunks = await storage.list_chunks_by_source(path, limit=None)
    assert chunks and all(c.metadata.redaction_count for c in chunks)
    assert all("a private value" not in c.retrieval_content for c in chunks)
    assert path.read_text() == original
    assert (
        privacy.enforce_write_guard(original, surface="mem_add", record_outcome=False).decision
        == "blocked"
    )


@pytest.mark.asyncio
async def test_masked_projection_cannot_overwrite_original_via_mem_edit(
    bm25_only_components, projecting
):
    from helpers import StubCtx
    from memtomem.server.context import AppContext
    from memtomem.server.tools.memory_crud import mem_edit

    comp, directory = bm25_only_components
    source = directory / "masked.md"
    original = '# Example\n\n`"password": "original private value"`\n'
    source.write_text(original)
    await comp.index_engine.index_file(source)
    chunks = await comp.storage.list_chunks_by_source(source, limit=None)
    assert chunks[0].metadata.redaction_count > 0
    response = await mem_edit(
        chunk_id=str(chunks[0].id),
        new_content="replacement",
        ctx=StubCtx(AppContext.from_components(comp)),
    )
    assert "masked_projection_read_only" in response
    assert source.read_text() == original


@pytest.mark.parametrize(
    ("name", "text"),
    [
        # The label ends its line and the value is on the next one. Answering
        # "empty assignment" here erased the label from the guard text, the
        # guard returned ``pass``, and the secret indexed verbatim.
        ("yaml_next_line", "password:\n  hunter2-very-secret\n"),
        ("yaml_block_scalar", "password: |\n  hunter2-very-secret\n"),
        ("yaml_folded_scalar", "api_key: >-\n  hunter2-very-secret\n"),
        ("toml_triple_quote", 'password = """\nhunter2-very-secret\n"""\n'),
        ("toml_triple_single", "password = '''\nhunter2-very-secret\n'''\n"),
        ("label_at_end_of_file", "password:"),
        # The quoted-key rule matched through the value's opening quote, so
        # the triple-quote guard on the unquoted branch never saw these.
        ("quoted_key_triple", QUOTED_KEY_TRIPLE),
        ("quoted_key_triple_single", QUOTED_KEY_TRIPLE_SINGLE),
        # A quote can close one fragment and open the next one of the same
        # value: shell and TOML concatenate adjacent strings, YAML writes an
        # embedded quote as a doubled one, Python joins with ``+``. Masking
        # to the first closing quote leaves the rest in the indexed body.
        ("adjacent_string_concat", ADJACENT_CONCAT),
        ("yaml_doubled_quote", YAML_DOUBLED_QUOTE),
        ("python_plus_concat", PYTHON_PLUS_CONCAT),
        # A block scalar opener with a trailing comment is still an opener.
        ("block_scalar_with_comment", BLOCK_SCALAR_COMMENT),
        # A plain YAML scalar folds the next more-indented line into it —
        # across blank lines too, which do not terminate it.
        ("plain_scalar_continuation", PLAIN_CONTINUATION),
        ("blank_line_folds_into_the_scalar", BLANK_LINE_FOLD),
        ("blank_line_folds_crlf", BLANK_LINE_FOLD_CRLF),
        # A closing quote ends a string, not necessarily the value: a shell
        # word continues straight through one.
        ("shell_word_continues_past_comma", SHELL_COMMA),
        ("shell_word_continues_past_hash", SHELL_HASH),
        ("shell_command_substitution", SHELL_BACKTICK),
        # An opener whose value is on a later line, in a language with no
        # indentation rule at all.
        ("python_parenthesised_value", PYTHON_PAREN),
        ("python_list_value", PYTHON_LIST),
    ],
)
def test_a_value_this_module_cannot_account_for_stays_blocked(name, text, projecting):
    """A label whose value is not on the label's line is not an empty value.

    ``prepare_index_content`` erases *accounted* hits from ``guard_content``.
    An unaccounted hit must survive that erasure, or the guard is handed text
    with nothing left to match and admits a file it used to block.
    """
    projection = prepare_index_content(text, scope="user")

    assert projection.redaction_count == 0
    # Falling back to the original is what puts the label back in front of the
    # scanner, which is what makes the guard block.
    assert projection.content == text
    assert projection.guard_content == text
    # What the fallback is *for*: the label is back in front of the scanner, so
    # the write guard refuses the file instead of admitting it.
    assert privacy.scan(projection.guard_content)
    assert (
        privacy.enforce_write_guard(
            projection.guard_content, surface="test", record_outcome=False
        ).decision
        == "blocked"
    )


def test_a_value_on_the_label_s_own_line_is_still_masked(projecting):
    """The carve-outs above must not have disabled the feature itself."""
    projection = prepare_index_content("password: hunter2-very-secret\n", scope="user")

    assert projection.redaction_count == 1
    assert projection.content == "password: [REDACTED]\n"
    assert not privacy.scan(projection.guard_content)


def test_projectable_rules_resolve_by_literal_not_by_ordinal(projecting):
    """A reordering STM sync must not re-point "maskable" at another rule.

    ``DEFAULT_PATTERNS`` is synced from memtomem-stm and its docstring says a
    resync may reorder it. The projection names the rules it adjudicates by
    their regex literal; this pins that the names still resolve, and that they
    resolve to the label rules rather than to whatever sits at slots 0-2.
    """
    from memtomem.indexing import privacy_projection as pp

    assert pp._RESOLVED
    assert len(pp._PROJECTABLE) == 3
    assert len(pp._QUOTED) == 1
    for index in pp._PROJECTABLE:
        assert privacy.DEFAULT_PATTERNS[index] in privacy.PROJECTABLE_LABEL_PATTERNS
    for index in pp._QUOTED:
        assert privacy.DEFAULT_PATTERNS[index] in privacy.QUOTED_LABEL_PATTERNS
    # Every other rule matches the secret itself, so it has no separable value.
    others = set(range(len(privacy.DEFAULT_PATTERNS))) - pp._PROJECTABLE
    assert others, "the pattern set must hold more than the label rules"


def test_a_provider_token_is_never_masked_even_beside_a_label(projecting):
    """The all-or-nothing rule: one unprojectable hit refuses the whole file."""
    text = 'password: hunter2\napi_key: "sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD"\n'
    projection = prepare_index_content(text, scope="user")

    assert projection.redaction_count == 0
    assert projection.guard_content == text
    assert privacy.scan(projection.guard_content)


@pytest.mark.asyncio
async def test_a_masked_chunk_keeps_its_headings_in_the_retrieval_text(
    storage, tmp_path, projecting
):
    """The masking note must not evict the heading hierarchy.

    ``Chunk.retrieval_content`` short-circuits on a non-empty
    ``retrieval_context``, so assigning a bare note dropped the headings from
    the embedded and BM25 text — and every masked chunk in the store indexed
    the same sentence instead of its own section.
    """
    path = tmp_path / "deploy.md"
    path.write_text("# Deploy notes\n\n## Staging box\n\npassword: hunter2-very-secret\n")
    embedder = AsyncMock()
    embedder.dimension = 0
    embedder.model_name = "none"
    engine = IndexEngine(storage, embedder, IndexingConfig(memory_dirs=[tmp_path]))

    await engine.index_file(path)

    chunks = await storage.list_chunks_by_source(path, limit=None)
    masked = [c for c in chunks if c.metadata.redaction_count]
    assert masked
    for chunk in masked:
        text = chunk.retrieval_content
        assert "Staging box" in text, "the section heading must still be searchable"
        assert "hunter2-very-secret" not in text
        assert "[REDACTED]" in text


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("json_object_member", '{"password": "' + _S + '", "port": 5432}\n'),
        ("dict_repr_member", "{'password': '" + _S + "', 'port': 5432}\n"),
        ("trailing_comment", 'password = "' + _S + '"  # staging\n'),
        ("markdown_inline_code", '`"password": "' + _S + '"`\n'),
        ("unindented_next_section", "password: " + _S + "\n\n# Next section\n"),
    ],
)
def test_a_value_that_ends_where_it_looks_like_it_ends_is_masked(name, text, projecting):
    """The stricter boundary must not have refused the ordinary shapes.

    A whitelist that refuses everything is trivially safe and useless. These are
    the value endings the projection is *supposed* to recognise: a closing quote
    followed by container punctuation, a comment or a Markdown code-span close,
    and a plain scalar terminated by a blank line.
    """
    projection = prepare_index_content(text, scope="user")

    assert projection.redaction_count == 1
    assert _S not in projection.content
    assert MARKER in projection.content
    assert not privacy.scan(projection.guard_content)


#: The shapes no character-level rule in this module can close, each from a
#: different grammar, each verified against that grammar's own parser during
#: review. They are why ``PROJECTION_ENABLED`` is ``False``.
UNCLOSEABLE = [
    ("python_implicit_concatenation", PYTHON_IMPLICIT_CONCAT),
    ("yaml_flow_mapping_continuation", YAML_FLOW_CONTINUATION),
    ("multi_backtick_code_span", MULTI_BACKTICK_SPAN),
]


@pytest.mark.parametrize(("name", "text"), UNCLOSEABLE)
def test_the_shipped_default_blocks_what_the_grammar_cannot_close(name, text):
    """With the feature as shipped, these files block like any other.

    ``PROJECTION_ENABLED`` is off, so ``prepare_index_content`` returns the text
    unchanged and the write guard sees the label the scanner matched.
    """
    projection = prepare_index_content(text, scope="user")

    assert projection.redaction_count == 0
    assert projection.guard_content == text
    assert (
        privacy.enforce_write_guard(
            projection.guard_content, surface="test", record_outcome=False
        ).decision
        == "blocked"
    )


@pytest.mark.parametrize(("name", "text"), UNCLOSEABLE)
def test_enabling_the_projection_reopens_these_bypasses(name, text, projecting):
    """A tripwire, not a wish: this is what turning the flag on costs today.

    Each of these is a valid document in some grammar whose value continues past
    where a character-level rule can see it — Python joins adjacent string
    literals across lines, a YAML flow mapping has no indentation rule, a
    three-backtick code span contains single backticks as text. The projection
    masks the first fragment, erases the label from ``guard_content``, and the
    guard then answers ``pass`` over a body that still holds the secret.

    If a future change closes these — the parser-backed version the module
    docstring describes — this test fails, which is the point: it forces whoever
    does that to come back here and re-read why the flag was off.
    """
    projection = prepare_index_content(text, scope="user")

    assert projection.redaction_count == 1
    assert _S in projection.content, "the secret survives the masking"
    assert not privacy.scan(projection.guard_content), "and the guard is blind to it"
