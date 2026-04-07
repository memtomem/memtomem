"""Format surfaced memories for injection into tool responses."""

from __future__ import annotations

from typing import Any

from memtomem_stm.surfacing.config import SurfacingConfig


class SurfacingFormatter:
    """Inject surfaced memories into a tool response."""

    def __init__(self, config: SurfacingConfig) -> None:
        self._config = config

    def inject(
        self,
        response_text: str,
        results: list[Any],
        query: str,
        surfacing_id: str | None = None,
        scratch_items: list[dict] | None = None,
    ) -> str:
        if not results and not scratch_items:
            return response_text

        lines = [self._config.section_header, ""]

        for r in results:
            chunk = r.chunk
            meta = chunk.metadata
            ns_badge = f" [{meta.namespace}]" if meta.namespace != "default" else ""
            source = str(meta.source_file.name) if meta.source_file else ""

            ctx = getattr(r, "context", None)
            if ctx and (ctx.window_before or ctx.window_after):
                parts = []
                if ctx.window_before:
                    parts.append("..." + ctx.window_before[-1].content[-150:].replace("\n", " "))
                parts.append(chunk.content[:300].replace("\n", " "))
                if ctx.window_after:
                    parts.append(ctx.window_after[0].content[:150].replace("\n", " ") + "...")
                preview = " | ".join(parts)
            else:
                preview = chunk.content[:300].replace("\n", " ")

            lines.append(f"- **{source}**{ns_badge} (score={r.score:.2f}): {preview}")

        if scratch_items:
            lines.append("")
            lines.append("**Working Memory:**")
            for item in scratch_items[:3]:
                key = item.get("key", "")
                value = str(item.get("value", ""))[:200].replace("\n", " ")
                lines.append(f"- `{key}`: {value}")

        if surfacing_id:
            lines.append(
                f"\n_Surfacing ID: {surfacing_id} — call `stm_surfacing_feedback` to rate_"
            )

        memory_block = "\n".join(lines)

        # Enforce injection size limit to prevent context bloat
        max_chars = self._config.max_injection_chars
        if max_chars and len(memory_block) > max_chars:
            memory_block = memory_block[:max_chars] + "\n... (memory block truncated)"

        match self._config.injection_mode:
            case "prepend":
                return f"{memory_block}\n\n---\n\n{response_text}"
            case "append" | "section" | _:
                return f"{response_text}\n\n---\n\n{memory_block}"
