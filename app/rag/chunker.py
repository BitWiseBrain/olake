"""Text chunking by heading structure.

Rules:
- Split on markdown headings (#, ##, ###)
- Target ~400 tokens per chunk, overlap ~50 tokens
- Never split inside code blocks (```...```) or tables (| ... |)
- Prepend title + heading_path as context to each chunk before embedding
- Extract metadata hints: connector, sync_mode, catalog_type, product
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Iterator

from app.core.logging import get_logger
from app.core.store import Chunk, compute_hash, make_embed_text

log = get_logger(__name__)

# ── Token counting (rough: 1 token ≈ 4 chars) ────────────────────────────────
def count_tokens(text: str) -> int:
    return max(1, len(text) // 4)


TARGET_TOKENS = 400
MAX_TOKENS = 600
OVERLAP_TOKENS = 50

# ── Connector/product detection ───────────────────────────────────────────────
CONNECTOR_PATTERNS = {
    "postgres": r"\bpostgres(?:ql)?\b",
    "mysql": r"\bmysql\b",
    "mongodb": r"\bmongodb\b|\bmongo\b",
    "oracle": r"\boracle\b",
    "mssql": r"\bmssql\b|sql\s*server",
    "db2": r"\bdb2\b",
    "kafka": r"\bkafka\b",
    "s3": r"\bs3\b|object\s*storage",
}

SYNC_MODE_PATTERNS = {
    "cdc": r"\bcdc\b|change\s*data\s*capture",
    "full_refresh": r"\bfull[\s_-]refresh\b",
    "incremental": r"\bincremental\b",
}

CATALOG_PATTERNS = {
    "glue": r"\bglue\b",
    "rest": r"\brest\s*catalog\b",
    "hive": r"\bhive\b",
    "jdbc": r"\bjdbc\b",
    "nessie": r"\bnessie\b",
    "polaris": r"\bpolaris\b",
    "unity": r"\bunity\s*catalog\b",
    "lakekeeper": r"\blakekeeper\b",
}

PRODUCT_PATTERNS = {
    "ingestion": r"\bolake[\s-]ingestion\b|\bingestion\b",
    "fusion": r"\bfusion\b|\btable\s*maintenance\b",
    "ui": r"\bolake[\s-]ui\b",
    "helm": r"\bhelm\b|kubernetes|k8s",
}


def _detect(text: str, patterns: dict[str, str]) -> str:
    lower = text.lower()
    for name, pat in patterns.items():
        if re.search(pat, lower):
            return name
    return ""


def detect_metadata(text: str) -> dict[str, str]:
    return {
        "connector": _detect(text, CONNECTOR_PATTERNS),
        "sync_mode": _detect(text, SYNC_MODE_PATTERNS),
        "catalog_type": _detect(text, CATALOG_PATTERNS),
        "product": _detect(text, PRODUCT_PATTERNS),
    }


# ── Heading parser ────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^\|.*\|$")


def _in_code_block(lines: list[str], idx: int) -> bool:
    """Check if line idx is inside a fenced code block."""
    fence_count = 0
    for i, line in enumerate(lines):
        if i == idx:
            break
        if line.strip().startswith("```"):
            fence_count += 1
    return fence_count % 2 == 1


def _split_by_headings(md: str) -> list[tuple[str, str, str]]:
    """Split markdown into sections. Returns [(heading_text, level, body)]."""
    lines = md.split("\n")
    sections: list[tuple[str, str, str]] = []
    current_heading = ""
    current_level = ""
    current_lines: list[str] = []
    in_fence = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence

        if not in_fence:
            m = re.match(r"^(#{1,3})\s+(.+)$", line)
            if m:
                if current_lines:
                    sections.append((current_heading, current_level, "\n".join(current_lines).strip()))
                current_heading = m.group(2).strip()
                current_level = m.group(1)
                current_lines = []
                continue

        current_lines.append(line)

    if current_lines:
        sections.append((current_heading, current_level, "\n".join(current_lines).strip()))

    return sections


def chunk_markdown(
    md: str,
    url: str,
    title: str,
    source_type: str = "docs",
    repo: str = "",
    path: str = "",
    updated_at: str = "",
) -> list[Chunk]:
    """Chunk a markdown document into retrieval-ready Chunk objects."""
    if not updated_at:
        updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ")

    sections = _split_by_headings(md)
    if not sections:
        return []

    chunks: list[Chunk] = []
    heading_stack: list[str] = []  # maintain heading hierarchy

    def build_heading_path(level: str, heading: str) -> str:
        depth = len(level)  # #=1, ##=2, ###=3
        # Trim stack to appropriate depth
        while len(heading_stack) >= depth:
            heading_stack.pop()
        heading_stack.append(heading)
        return " > ".join(heading_stack)

    def make_chunk(heading: str, heading_path: str, body: str, idx: int) -> Chunk:
        chunk_id = hashlib.md5(f"{url}#{heading_path}#{idx}".encode()).hexdigest()[:16]
        anchor = "#" + re.sub(r"[^a-z0-9-]", "", heading.lower().replace(" ", "-"))
        meta = detect_metadata(f"{title} {heading_path} {body}")
        c = Chunk(
            id=chunk_id,
            url=url,
            anchor=anchor,
            title=title,
            heading_path=heading_path,
            source_type=source_type,
            repo=repo,
            path=path,
            content_hash=compute_hash(body),
            updated_at=updated_at,
            content=body,
            connector=meta["connector"],
            sync_mode=meta["sync_mode"],
            catalog_type=meta["catalog_type"],
            product=meta["product"],
        )
        c.embed_text = make_embed_text(c)
        return c

    for i, (heading, level, body) in enumerate(sections):
        if heading and level:
            heading_path = build_heading_path(level, heading)
        else:
            heading_path = ""

        if not body.strip():
            continue

        # If body fits in one chunk, emit it directly
        if count_tokens(body) <= MAX_TOKENS:
            chunks.append(make_chunk(heading, heading_path, body, i))
            continue

        # Split large sections into sub-chunks, preserving code blocks
        sub_chunks = _split_large_section(body)
        for j, sub in enumerate(sub_chunks):
            if sub.strip():
                chunks.append(make_chunk(heading, heading_path, sub, i * 1000 + j))

    return chunks


def _split_large_section(text: str) -> list[str]:
    """Split a large section respecting code blocks and tables."""
    # Find code block positions to avoid splitting inside them
    in_fence = False
    paragraphs: list[str] = []
    current: list[str] = []
    lines = text.split("\n")

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence

        if not in_fence and not stripped and current:
            # Paragraph break
            block = "\n".join(current).strip()
            if block:
                paragraphs.append(block)
            current = []
        else:
            current.append(line)

    if current:
        paragraphs.append("\n".join(current).strip())

    # Combine paragraphs to hit TARGET_TOKENS with OVERLAP_TOKENS
    result: list[str] = []
    current_parts: list[str] = []
    current_tokens = 0

    for para in paragraphs:
        ptokens = count_tokens(para)
        if current_tokens + ptokens > TARGET_TOKENS and current_parts:
            result.append("\n\n".join(current_parts))
            # Keep last paragraph as overlap
            overlap = current_parts[-1] if current_parts else ""
            current_parts = [overlap, para] if overlap else [para]
            current_tokens = count_tokens(overlap) + ptokens
        else:
            current_parts.append(para)
            current_tokens += ptokens

    if current_parts:
        result.append("\n\n".join(current_parts))

    return result if result else [text]
