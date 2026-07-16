from memtomem.llm.utils import strip_llm_response


def test_strip_llm_response_simple():
    text = "```python\nprint('Hello')\n```"
    assert strip_llm_response(text) == "print('Hello')"


def test_strip_llm_response_multiple_code_blocks():
    # strip_llm_response only peels the outer fence; it never rewrites the payload.
    text = '```python\nprint(\'Hello\')\n```\n```json\n{"key": "value"}\n```'
    assert strip_llm_response(text) == 'print(\'Hello\')\n```\n```json\n{"key": "value"}'


def test_strip_llm_response_no_code_block():
    text = "Hello world"
    assert strip_llm_response(text) == "Hello world"


def test_strip_llm_response_empty():
    assert strip_llm_response("") == ""


def test_strip_llm_response_only_backticks():
    # A lone fence is both the first and last line, so nothing is left.
    text = "```"
    assert strip_llm_response(text) == ""


def test_strip_llm_response_no_language_tag():
    text = "```\nhello\n```"
    assert strip_llm_response(text) == "hello"


def test_strip_llm_response_unclosed_fence():
    # No closing fence: only the opening fence line is peeled.
    text = '```json\n{"a": 1}'
    assert strip_llm_response(text) == '{"a": 1}'


def test_strip_llm_response_whitespace_only():
    assert strip_llm_response("   \n\t ") == ""


def test_strip_llm_response_mid_line_backticks():
    # Backticks that don't open a fence are payload, not a wrapper.
    text = "use `foo` and ``bar`` mid-line"
    assert strip_llm_response(text) == text


def test_strip_llm_response_surrounding_whitespace():
    text = '  \n```json\n{"a": 1}\n```\n  '
    assert strip_llm_response(text) == '{"a": 1}'
