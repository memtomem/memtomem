"""
Unit tests for llm_utils.py functions.
"""

from memtomem.llm.utils import strip_llm_response


def test_strip_llm_response_plain_text():
    """Test that plain text is returned unchanged."""
    text = "Hello, world!"
    assert strip_llm_response(text) == "Hello, world!"


def test_strip_llm_response_code_block_without_lang():
    """Test that a code block without language tag is stripped."""
    text = "```\nprint('Hello')\n```"
    assert strip_llm_response(text) == "print('Hello')"


def test_strip_llm_response_code_block_with_lang():
    """Test that a code block with language tag is stripped."""
    text = "```python\nprint('Hello')\n```"
    assert strip_llm_response(text) == "print('Hello')"


def test_strip_llm_response_empty_string():
    """Test that an empty string returns an empty string."""
    assert strip_llm_response("") == ""


def test_strip_llm_response_whitespace_only():
    """Test that whitespace-only input returns an empty string."""
    assert strip_llm_response("   \n\t  ") == ""


def test_strip_llm_response_no_closing_fence():
    """Test that a code block without closing fence strips only the first line."""
    text = "```python\nprint('Hello')\nThis is still content"
    assert strip_llm_response(text) == "print('Hello')\nThis is still content"


def test_strip_llm_response_backticks_in_middle():
    """Test that backticks in the middle of text are preserved."""
    text = "```python\nprint('Hello')\n```\nThen run it."
    assert strip_llm_response(text) == "print('Hello')\n```\nThen run it."


def test_strip_llm_response_multiple_code_blocks():
    """Test that only the first code block's opening fence and final closing fence are stripped."""
    text = '```python\nprint(\'Hello\')\n```\n```json\n{"key": "value"}\n```'
    assert strip_llm_response(text) == 'print("Hello")\n```\n```json\n{"key": "value"}'
