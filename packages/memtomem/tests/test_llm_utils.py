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
