"""Direct boundary tests for is_secret_key and mask_secrets.

Pins the behavior of configuration secret masking across dicts, lists,
tuples, empty values, and non-mutation contracts.

See https://github.com/memtomem/memtomem/issues/2983
"""

from copy import deepcopy

import pytest

from memtomem.secret_masking import is_secret_key, mask_secrets


@pytest.mark.parametrize(
    "key",
    [
        "API_KEY",
        "api_key",
        "Api_Key",
        "Secret",
        "secret",
        "SECRET",
        "langfuse_secret_key",
        "LANGFUSE_SECRET_KEY",
        "openai_secret_key",
    ],
)
def test_is_secret_key_positive(key: str) -> None:
    """Secret keys are detected case-insensitively and via suffix."""
    assert is_secret_key(key) is True


@pytest.mark.parametrize(
    "key",
    [
        "api_keys",
        "API_KEYS",
        "password",
        "PASSWORD",
        "token",
        "username",
        "secret_token",
    ],
)
def test_is_secret_key_negative(key: str) -> None:
    """Non-matching field names return False."""
    assert is_secret_key(key) is False


def test_mask_secrets_empty_values_not_masked() -> None:
    """Empty string and None secret fields remain unchanged."""
    data = {"n": {"secret": ""}}
    assert mask_secrets(data) == {"n": {"secret": ""}}

    data_none = {"n": {"secret": None}}
    assert mask_secrets(data_none) == {"n": {"secret": None}}


def test_mask_secrets_none_vs_zero_operand() -> None:
    """None is left as-is, while zero (0) is considered non-empty and masked."""
    input_data = [{"API_KEY": None}, {"api_key": 0}]
    expected = [{"API_KEY": None}, {"api_key": "***"}]
    assert mask_secrets(input_data) == expected


def test_mask_secrets_tuple_shape_preserved() -> None:
    """Tuples are walked recursively without turning into lists."""
    input_data = {"tup": ({"secret": "s"},)}
    result = mask_secrets(input_data)
    assert result == {"tup": ({"secret": "***"},)}
    assert isinstance(result["tup"], tuple)


def test_mask_secrets_does_not_mutate_input() -> None:
    """Input objects are never modified in place."""
    test_cases = [
        {"n": {"secret": ""}},
        [{"API_KEY": None}, {"api_key": 0}],
        {"tup": ({"secret": "s"},)},
        {"api_key": "val", "nested": [{"secret": "s"}], "tags": ("a", "b")},
    ]

    for original in test_cases:
        snapshot = deepcopy(original)
        _ = mask_secrets(original)
        assert original == snapshot


def test_mask_secrets_primitives_and_empty_containers() -> None:
    """Scalar primitives and empty collections pass through unaltered."""
    assert mask_secrets("plain-string") == "plain-string"
    assert mask_secrets(42) == 42
    assert mask_secrets(True) is True
    assert mask_secrets(None) is None
    assert mask_secrets({}) == {}
    assert mask_secrets([]) == []
    assert mask_secrets(()) == ()
