"""Flat frontmatter values without YAML constructors or blanket reformatting."""

from __future__ import annotations

import logging
import re
from bisect import bisect_left

import yaml

_KEY = re.compile(r"[A-Za-z_][\w-]*\Z")
_KEY_LINE = re.compile(r"([A-Za-z_][\w-]*)\s*:")
logger = logging.getLogger(__name__)


def _warn_comment(text: str, node: yaml.ScalarNode, token_starts: list[int]) -> None:
    if node.style is not None:
        return
    # YAML tokens exclude comments. Inspect only the gap following this value;
    # a quoted sibling's literal hash is part of its token, never this gap.
    start = node.end_mark.index
    next_token = bisect_left(token_starts, start)
    end = token_starts[next_token] if next_token < len(token_starts) else len(text)
    # A standalone comment on a later YAML line did not truncate this value.
    line_break = re.search(r"[\r\n\x85\u2028\u2029]", text[start:end])
    if line_break is not None:
        end = start + line_break.start()
    if re.search(r"(^|\s)#", text[start:end]):
        logger.warning(
            "Unquoted frontmatter contains a YAML comment; quote literal hash characters"
        )


def flat_yaml(text: str) -> dict | None:
    """Read top-level plain keys, keeping scalar spelling and last-key-wins.

    None means invalid YAML: callers with a historical lenient grammar may
    use that grammar as a fallback. Nested objects never become top-level keys.
    Only node values are inspected; constructors/merge expansion are not run.
    """
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return None
    if not isinstance(root, yaml.MappingNode):
        return {}
    token_starts = [token.start_mark.index for token in yaml.scan(text, Loader=yaml.SafeLoader)]
    result = {}
    for key, value in root.value:
        if (
            not isinstance(key, yaml.ScalarNode)
            or key.start_mark.column != 0
            or key.style is not None
            or not _KEY.fullmatch(key.value)
        ):
            continue
        lexical_key = _KEY_LINE.match(text[key.start_mark.index :])
        if lexical_key is None or lexical_key.group(1) != key.value:
            continue
        if isinstance(value, yaml.ScalarNode):
            _warn_comment(text, value, token_starts)
            result[key.value] = (
                None if value.tag == "tag:yaml.org,2002:null" and not value.value else value.value
            )
        elif isinstance(value, yaml.SequenceNode):
            for item in value.value:
                if isinstance(item, yaml.ScalarNode):
                    _warn_comment(text, item, token_starts)
            if all(isinstance(item, yaml.ScalarNode) for item in value.value):
                result[key.value] = [item.value for item in value.value]
            else:
                logger.warning("Unsupported nested frontmatter sequence; quote literal item text")
    return result


def yaml_scalar(value: str, *, flow: bool = False) -> str:
    """Keep ordinary bytes; quote strings that YAML could misread."""
    if value.splitlines() != [value] or (
        flow and (any(c in value for c in ",[]{}") or value.startswith(("?", ":")))
    ):
        return yaml.safe_dump(
            value, default_style='"', allow_unicode=True, width=float("inf")
        ).rstrip("\n")
    rendered = yaml.safe_dump(
        {"value": value}, allow_unicode=True, sort_keys=False, width=float("inf")
    )
    return rendered.removeprefix("value: ").rstrip("\n")


def yaml_list(items: list[str]) -> str:
    return "[" + ", ".join(yaml_scalar(item, flow=True) for item in items) + "]"
