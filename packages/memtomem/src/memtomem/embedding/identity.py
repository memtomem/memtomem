"""Interpret recorded embedding identities without guessing missing fields."""

from __future__ import annotations

from memtomem.embedding.aliases import resolve_embedder_id


def _is_onnx(provider: str | None) -> bool:
    return (provider or "").strip().lower() == "onnx"


def canonical_embedding_model(provider: str | None, model: str | None) -> str:
    """Spell an ONNX model as the fastembed id it loads (#2463).

    ONNX short aliases (``multilingual-e5-small``, ``bge-m3``) load the same
    model as their full ids, so both spellings name one identity. Other
    providers send the name to their API verbatim, where a short and a full
    name can be different models, so their spelling is kept as written.
    """
    model = model or ""
    return resolve_embedder_id(model) if _is_onnx(provider) else model


def same_embedding_model(
    provider_a: str | None, model_a: str | None, provider_b: str | None, model_b: str | None
) -> bool:
    """Whether two recorded models are one identity, ignoring ONNX alias spelling.

    Alias equivalence applies only when both sides are ONNX; any other pair
    compares the spelling exactly, as before.
    """
    if (model_a or "") == (model_b or ""):
        return True
    return (
        _is_onnx(provider_a)
        and _is_onnx(provider_b)
        and canonical_embedding_model(provider_a, model_a)
        == canonical_embedding_model(provider_b, model_b)
    )


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
