"""Importers for Notion and Obsidian exports."""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)


async def import_notion(export_path: Path, output_dir: Path) -> list[Path]:
    """Import a Notion export (ZIP or directory) into markdown files.

    Notion exports come as a ZIP with markdown files + nested folders.
    File names contain UUIDs that we strip for cleaner names.

    Args:
        export_path: Path to Notion export ZIP or extracted directory.
        output_dir: Directory to write cleaned markdown files.

    Returns:
        List of imported file paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    source_dir = export_path

    # Extract ZIP if needed
    if export_path.suffix == ".zip":
        extract_dir = output_dir / "_notion_extract"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(export_path, "r") as zf:
            zf.extractall(extract_dir)
        source_dir = extract_dir

    imported: list[Path] = []

    for md_file in sorted(source_dir.rglob("*.md")):
        content = md_file.read_text(encoding="utf-8", errors="replace")

        # Clean Notion-specific artifacts
        content = _clean_notion_markdown(content)

        # Clean filename (remove Notion UUID suffix)
        clean_name = _clean_notion_filename(md_file.stem) + ".md"

        # Preserve directory structure
        rel = md_file.relative_to(source_dir)
        target = output_dir / rel.parent / clean_name
        target.parent.mkdir(parents=True, exist_ok=True)

        # Add source metadata
        header = f"---\nimported_from: notion\noriginal_file: {md_file.name}\n---\n\n"
        target.write_text(header + content, encoding="utf-8")
        imported.append(target)

    logger.info("Imported %d files from Notion export", len(imported))
    return imported


async def import_obsidian(vault_path: Path, output_dir: Path) -> list[Path]:
    """Import an Obsidian vault into memtomem-compatible markdown.

    Converts Obsidian-specific syntax:
    - [[wikilinks]] → [wikilinks](wikilinks.md)
    - ![[embeds]] → [embeds](embeds.md)
    - Callouts (> [!note]) preserved as blockquotes
    - Tags (#tag) preserved

    Args:
        vault_path: Path to Obsidian vault root directory.
        output_dir: Directory to write converted files.

    Returns:
        List of imported file paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    imported: list[Path] = []

    for md_file in sorted(vault_path.rglob("*.md")):
        # Skip Obsidian config files
        rel = md_file.relative_to(vault_path)
        if str(rel).startswith(".obsidian"):
            continue

        content = md_file.read_text(encoding="utf-8", errors="replace")

        # Convert Obsidian syntax
        content = _convert_obsidian_syntax(content)

        target = output_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)

        # Add source metadata
        header = f"---\nimported_from: obsidian\noriginal_file: {rel}\n---\n\n"
        target.write_text(header + content, encoding="utf-8")
        imported.append(target)

    logger.info("Imported %d files from Obsidian vault", len(imported))
    return imported


# ── Notion helpers ───────────────────────────────────────────────────────


def _clean_notion_filename(stem: str) -> str:
    """Remove Notion's UUID suffix from filenames. 'Page Name abc123def456' → 'Page Name'."""
    # Notion appends a 32-char hex UUID at the end
    cleaned = re.sub(r"\s+[0-9a-f]{32}$", "", stem)
    return cleaned or stem


def _clean_notion_markdown(content: str) -> str:
    """Clean Notion-specific markdown artifacts."""
    # Remove Notion's property tables at the top
    content = re.sub(r"^(\|[^\n]+\|\n)+\n", "", content)

    # Fix Notion's broken link format: [text](Page%20Name%20uuid.md) → [text](Page Name.md)
    def _fix_link(m):
        text = m.group(1)
        href = m.group(2)
        # URL-decode and strip UUID
        from urllib.parse import unquote

        decoded = unquote(href)
        if decoded.endswith(".md"):
            decoded = _clean_notion_filename(decoded[:-3]) + ".md"
        return f"[{text}]({decoded})"

    content = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _fix_link, content)

    # Remove empty toggle blocks
    content = re.sub(r"<details>\s*<summary></summary>\s*</details>", "", content)

    return content.strip()


# ── Obsidian helpers ─────────────────────────────────────────────────────


def _convert_obsidian_syntax(content: str) -> str:
    """Convert Obsidian-specific syntax to standard markdown."""
    # [[wikilink]] → [wikilink](wikilink.md)
    content = re.sub(
        r"!\[\[([^\]|]+?)(?:\|([^\]]*))?\]\]",
        lambda m: f"[{m.group(2) or m.group(1)}]({m.group(1).replace(' ', '%20')}.md)",
        content,
    )
    content = re.sub(
        r"\[\[([^\]|]+?)(?:\|([^\]]*))?\]\]",
        lambda m: f"[{m.group(2) or m.group(1)}]({m.group(1).replace(' ', '%20')}.md)",
        content,
    )

    # Obsidian callouts: > [!note] Title → > **Note**: Title
    content = re.sub(
        r"^(>\s*)\[!(\w+)\]\s*(.*)",
        lambda m: f"{m.group(1)}**{m.group(2).capitalize()}**: {m.group(3)}",
        content,
        flags=re.MULTILINE,
    )

    return content
