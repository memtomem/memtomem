"""Interpret recorded embedding identities without guessing missing fields."""

from __future__ import annotations


def embedding_identity_complete(provider: str | None, model: str | None) -> bool:
    """A real provider needs a model; ``none`` deliberately needs none."""
    return bool(provider) and ((provider or "").lower() == "none" or bool(model))


def require_complete_embedding_identity(provider: str | None, model: str | None) -> None:
    """Refuse recovery to an identity whose missing half cannot be inferred."""
    if not embedding_identity_complete(provider, model):
        missing = ", ".join(
            name for name, value in (("provider", provider), ("model", model)) if not value
        )
        raise ValueError(
            f"Cannot revert to stored embedding settings: {missing} is unknown. "
            "Restore the recorded identity from a known-good backup, or run "
            "'mm embedding-reset --mode apply-current' (CLI) / "
            'mem_embedding_reset(mode="apply_current") (MCP) to reset the DB '
            "(deletes all vectors), then run 'mm index --force <memory_dir>' to re-index."
        )


def embedding_identity_label(provider: str | None, model: str | None) -> str:
    """Display an unknown field explicitly, preserving ``none``'s empty model."""
    model_label = model or ("" if (provider or "").lower() == "none" else "unknown")
    return f"{provider or 'unknown'}/{model_label}"
