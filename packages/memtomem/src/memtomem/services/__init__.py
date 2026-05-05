"""Cross-surface services shared by Web, MCP, and CLI.

Modules here hold business logic that should not be duplicated across
the three entry-point surfaces. Each service module operates on the
storage protocol (and any explicitly-passed dependencies such as the
search pipeline) so it stays callable from any surface without a hard
import on Web/MCP/CLI internals.
"""
