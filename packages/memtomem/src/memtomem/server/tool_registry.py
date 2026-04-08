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
    """Extract per-parameter descriptions from a Google-style Args section."""
    result: dict[str, str] = {}
    in_args = False
    current_name = ""
    current_desc = ""
    import re

    for line in docstring.splitlines():
        stripped = line.strip()
        if stripped in ("Args:", "Arguments:"):
            in_args = True
            continue
        if in_args:
            if stripped and not stripped.startswith("-") and not line.startswith("    "):
                break  # Left the Args section
            match = re.match(r"^\s{4,}(\w+):\s*(.+)$", line)
            if match:
                if current_name:
                    result[current_name] = current_desc.strip()
                current_name = match.group(1)
                current_desc = match.group(2)
            elif current_name and stripped:
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
            default = f" = {p.default!r}" if p.default != inspect.Parameter.empty else ""
            params[name] = f"{type_str}{default}"

        action_name = fn.__name__.removeprefix("mem_")
        ACTIONS[action_name] = ActionInfo(
            fn=fn,
            category=category,
            description=(fn.__doc__ or "").split("\n")[0].strip(),
            params=params,
            param_docs=_parse_arg_docs(fn.__doc__ or ""),
        )
        return fn

    return decorator
