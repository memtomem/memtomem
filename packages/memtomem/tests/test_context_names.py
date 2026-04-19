"""Tests for memtomem.context._names.validate_name."""

from __future__ import annotations

import pytest

from memtomem.context._names import InvalidNameError, validate_name


@pytest.mark.parametrize(
    "value",
    [
        "hello",
        "a-b_c.d",
        "x",  # single char
        "A" * 64,  # max length
        "123",  # all digits
        "agent.v2",
        "name_with_underscore",
        "name-with-dash",
    ],
)
def test_valid_names_pass_through(value: str) -> None:
    assert validate_name(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "..",
        "../x",
        "a/b",
        "a\\b",
        "",
        "   ",
        ".",
        "a\nb",
        "a\rb",
        "a\x00b",
        "-x",  # leading dash
        "/absolute",
        "\\windows",
        "控",  # non-ASCII / CJK
        "name with space",
    ],
)
def test_invalid_names_are_rejected(value: str) -> None:
    with pytest.raises(InvalidNameError):
        validate_name(value)


def test_name_too_long() -> None:
    with pytest.raises(InvalidNameError, match="exceeds 64"):
        validate_name("A" * 65)


def test_non_string_rejected() -> None:
    with pytest.raises(InvalidNameError, match="expected str"):
        validate_name(123)  # type: ignore[arg-type]


def test_kind_appears_in_error_message() -> None:
    with pytest.raises(InvalidNameError, match="invalid agent name"):
        validate_name("../x", kind="agent name")


def test_dot_and_dotdot_rejected_explicitly() -> None:
    with pytest.raises(InvalidNameError, match="reserved path token"):
        validate_name(".")
    with pytest.raises(InvalidNameError, match="reserved path token"):
        validate_name("..")
