"""Runtime publication helpers for optional embedding capabilities."""

from __future__ import annotations


def publish_onnx_batch_size(embedder: object, value: int) -> bool:
    """Publish an ONNX batch update without triggering dynamic Mock attributes.

    ``Mock``/``AsyncMock`` fabricate arbitrary callable attributes, so a plain
    ``getattr`` capability probe can create an un-awaited coroutine and make a
    test double look like an ONNX embedder. A concrete class method or an
    explicitly attached instance method counts as support.
    """
    class_setter = getattr(type(embedder), "set_onnx_batch_size", None)
    instance_dict = getattr(embedder, "__dict__", {})
    instance_setter = (
        instance_dict.get("set_onnx_batch_size") if isinstance(instance_dict, dict) else None
    )
    setter = getattr(embedder, "set_onnx_batch_size", None)
    if not callable(setter) or not (callable(class_setter) or callable(instance_setter)):
        return False
    setter(value)
    return True
