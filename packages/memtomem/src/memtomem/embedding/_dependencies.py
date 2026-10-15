"""Actionable errors for optional ONNX tokenizer dependencies."""

from memtomem import __version__
from memtomem.errors import ConfigError


def missing_onnx_dependency(module: str) -> ConfigError:
    return ConfigError.vetted(
        f"Missing optional module '{module}' required by the configured tokenizer. "
        "Install memtomem[onnx] in the environment that runs this server, then restart it. "
        "For uvx, change the server launch command to: "
        f"uvx --from 'memtomem[onnx]=={__version__}' memtomem-server. "
        "Installing packages or running doctor in a different environment does not repair "
        "this server's environment."
    )
