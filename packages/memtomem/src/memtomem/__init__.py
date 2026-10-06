"""memtomem: Markdown-first memory infrastructure for AI agents."""

import os as _os
from importlib.metadata import version as _pkg_version

# ONNX Runtime 1.29+ uploads telemetry to Microsoft from official Linux and macOS
# builds unless this is truthy before the runtime initializes (#2664). It is set
# here, not at the lazy fastembed / sentence-transformers import sites, because
# every entry point imports this package before any of them. An explicit value
# in the environment wins. Not covered: a host process that initialized
# onnxruntime before importing memtomem, and Windows, where ONNX Runtime emits
# ETW events and does not read this variable.
_os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

__version__ = _pkg_version("memtomem")
