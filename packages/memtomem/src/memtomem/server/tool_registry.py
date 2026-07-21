"""Action registry for mem_do meta-tool routing.

Each non-core tool registers itself here via the @register decorator.
The mem_do tool uses this registry to dispatch actions by name.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

ActionFn = Callable[..., Coroutine[Any, Any, str]]


@dataclass
class ActionInfo:
    """Metadata for a registered action."""

    fn: ActionFn
    category: str
    description: str
    params: dict[str, str] = field(default_factory=dict)
    param_docs: dict[str, str] = field(default_factory=dict)


ACTIONS: dict[str, ActionInfo] = {}


def _parse_arg_docs(docstring: str) -> dict[str, str]:
    """Extract per-parameter descriptions from a Google-style Args section.

    Only lines at the indent of the *first* parameter start a new entry.
    Anything indented deeper is continuation text for the parameter above it.
    A docstring is free to nest ``key: value`` blocks under a parameter — JSON
    schemas in ``mem_policy_add``, bullet lists elsewhere — and those keys are
    not parameters. Matching any ``\\s{4,}(\\w+):`` line, as this did before,
    turned ``auto_expire:`` inside a config example into a phantom parameter
    that ``mem_do(action="help")`` then advertised as callable.
    """
    import re

    result: dict[str, str] = {}
    in_args = False
    args_indent = 0
    param_indent: int | None = None
    current_name = ""
    current_desc = ""

    for line in docstring.splitlines():
        stripped = line.strip()
        if not in_args:
            if stripped in ("Args:", "Arguments:"):
                in_args = True
                args_indent = len(line) - len(line.lstrip())
            continue
        if not stripped:
            continue  # blank lines are allowed inside the block
        indent = len(line) - len(line.lstrip())
        # Dedenting back to (or past) the ``Args:`` header ends the section.
        # Docstrings are not uniformly indented at runtime — some arrive
        # dedented, some keep the source indentation — so compare against the
        # header/params we actually saw rather than a fixed column.
        if indent <= (param_indent - 1 if param_indent is not None else args_indent):
            break
        match = re.match(r"^\s+(\w+):\s*(.+)$", line)
        if match and (param_indent is None or indent == param_indent):
            param_indent = indent
            if current_name:
                result[current_name] = current_desc.strip()
            current_name = match.group(1)
            current_desc = match.group(2)
        elif current_name:
            current_desc += " " + stripped
    if current_name:
        result[current_name] = current_desc.strip()
    return result


def register(category: str):
    """Decorator: register an async tool function as a mem_do action.

    The action name is derived from the function name by stripping the
    ``mem_`` prefix (e.g. ``mem_session_start`` → ``session_start``).
    """

    def decorator(fn: ActionFn) -> ActionFn:
        sig = inspect.signature(fn)
        params: dict[str, str] = {}
        for name, p in sig.parameters.items():
            if name == "ctx":
                continue
            ann = p.annotation
            type_str = str(ann) if ann != inspect.Parameter.empty else "Any"
            # Clean up forward-ref representations
            type_str = type_str.replace("typing.", "").replace("__future__.", "")
            # ``StrictBool`` is a validation detail of the FastMCP boundary (it
            # stops "true"/1 coercing into a real bool); to an agent reading
            # ``mem_do(action="help")`` the parameter is simply a boolean.
            type_str = type_str.replace("StrictBool", "bool")
            default = f" = {p.default!r}" if p.default != inspect.Parameter.empty else ""
            params[name] = f"{type_str}{default}"

        # Second line of defense behind the parser: only real parameters may
        # reach the help catalog. A docstring shape the parser mis-reads can
        # still produce a stray key; dropping it here means the worst outcome
        # is a missing description, never an invented parameter an agent then
        # tries to pass.
        param_docs = {k: v for k, v in _parse_arg_docs(fn.__doc__ or "").items() if k in params}

        action_name = fn.__name__.removeprefix("mem_")
        ACTIONS[action_name] = ActionInfo(
            fn=fn,
            category=category,
            description=(fn.__doc__ or "").split("\n")[0].strip(),
            params=params,
            param_docs=param_docs,
        )
        return fn

    return decorator
