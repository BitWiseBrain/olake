"""Query understanding: rule-based detection + LLM rewrite for multi-turn.

Single-turn: regex-only (zero LLM calls).
Multi-turn: one LLM call (max_tokens=80) to rewrite with context.
Out-of-scope/smalltalk: detected by rules, no retrieval.
Prompt-injection: heuristic detection.
"""
from __future__ import annotations

import re

from app.core.logging import get_logger
from app.rag.chunker import (
    CONNECTOR_PATTERNS,
    SYNC_MODE_PATTERNS,
    CATALOG_PATTERNS,
    PRODUCT_PATTERNS,
    detect_metadata,
)

log = get_logger(__name__)


# ── Scope classification ──────────────────────────────────────────────────────

OLAKE_KEYWORDS = {
    "olake", "ingestion", "iceberg", "parquet", "cdc", "debezium",
    "replication", "connector", "postgres", "mysql", "mongodb", "oracle",
    "mssql", "db2", "kafka", "s3", "glue", "hive", "nessie", "polaris",
    "unity", "fusion", "compaction", "maintenance", "helm", "docker",
    "kubernetes", "k8s", "sync", "stream", "catalog", "lakehouse",
    "datazip", "table", "schema", "binlog", "oplog", "wal",
}

SMALLTALK_PATTERNS = [
    r"^(hi|hello|hey|howdy|sup|yo)\b",
    r"^(how are you|what's up|how do you do)\b",
    r"^(thank(s| you)|bye|goodbye|cya)\b",
    r"^(who are you|what are you|are you an ai)\b",
]

INJECTION_PATTERNS = [
    r"ignore (previous|above|all) instruction",
    r"disregard.*instruction",
    r"forget.*instruction",
    r"you are now",
    r"act as.*admin",
    r"reveal.*system prompt",
    r"show.*api key",
    r"print.*env",
]

OUT_OF_SCOPE_PATTERNS = [
    r"\b(recipe|cook|food|restaurant|movie|music|sport|weather|stock|crypto|game|dating)\b",
    r"\b(politics|election|president|religion|god|allah|jesus)\b",
    r"\b(write.*poem|tell.*joke|story|fiction)\b",
]


def is_smalltalk(question: str) -> bool:
    q = question.lower().strip()
    for pat in SMALLTALK_PATTERNS:
        if re.search(pat, q):
            return True
    return False


def is_injection_attempt(question: str) -> bool:
    q = question.lower()
    for pat in INJECTION_PATTERNS:
        if re.search(pat, q):
            return True
    return False


def is_out_of_scope(question: str) -> bool:
    q = question.lower()
    # Check if any OLake keywords present → in-scope
    words = set(re.findall(r"\b\w+\b", q))
    if words & OLAKE_KEYWORDS:
        return False
    for pat in OUT_OF_SCOPE_PATTERNS:
        if re.search(pat, q):
            return True
    return False


def normalize_question(q: str) -> str:
    """Normalize for cache key."""
    q = q.lower().strip()
    q = re.sub(r"\s+", " ", q)
    q = re.sub(r"[^\w\s?]", "", q)
    return q


class QueryUnderstanding:
    question: str
    standalone_query: str
    detected_meta: dict[str, str]
    is_smalltalk: bool
    is_injection: bool
    is_out_of_scope: bool
    scope_ok: bool

    def __init__(
        self,
        question: str,
        standalone_query: str,
        detected_meta: dict,
        smalltalk: bool,
        injection: bool,
        out_of_scope: bool,
    ) -> None:
        self.question = question
        self.standalone_query = standalone_query
        self.detected_meta = detected_meta
        self.is_smalltalk = smalltalk
        self.is_injection = injection
        self.is_out_of_scope = out_of_scope
        self.scope_ok = not (smalltalk or injection or out_of_scope)


async def understand_query(
    question: str,
    history: list[dict] | None = None,
) -> QueryUnderstanding:
    """Understand query intent. Uses LLM only for multi-turn rewrite."""
    smalltalk = is_smalltalk(question)
    injection = is_injection_attempt(question)
    out_of_scope = not smalltalk and not injection and is_out_of_scope(question)

    meta = detect_metadata(question)
    standalone_query = question

    # LLM rewrite only for multi-turn with history
    if history and len(history) >= 2 and not smalltalk and not injection:
        try:
            standalone_query = await _rewrite_with_history(question, history[-4:])
            meta = detect_metadata(standalone_query)
        except Exception as e:
            log.warning(f"Query rewrite failed, using original: {e}")

    return QueryUnderstanding(
        question=question,
        standalone_query=standalone_query,
        detected_meta=meta,
        smalltalk=smalltalk,
        injection=injection,
        out_of_scope=out_of_scope,
    )


async def _rewrite_with_history(question: str, history: list[dict]) -> str:
    """One cheap LLM call to make the question standalone."""
    from app.core import llm

    history_text = "\n".join(
        f"{h['role'].upper()}: {h['content'][:200]}" for h in history
    )
    system = (
        "You rewrite follow-up questions into standalone search queries. "
        "Output ONLY the rewritten query, nothing else. Max 20 words."
    )
    user = f"Conversation:\n{history_text}\n\nFollow-up: {question}\n\nStandalone query:"

    result = await llm.generate(system, user, max_tokens=80, temperature=0.0)
    return result.strip().strip('"').strip("'") or question
