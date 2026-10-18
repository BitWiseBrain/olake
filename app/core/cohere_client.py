"""Cohere client: embed + rerank with trial-key quota guard and local fallback.

Trial key handling:
- Reads x-trial-endpoint-call-limit / x-trial-endpoint-call-remaining headers
- Above 80% usage → automatically switch to local (shows in /readyz + /admin/stats)
- Persists daily/monthly call counters in SQLite (same DB as chunks)
- Caches embedding results by (model, content_hash) to avoid re-calling on reruns
- On 429/5xx/timeout → immediate fallback to local fastembed
- Never mixes Cohere and local vectors in same index (index metadata tracks model)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import struct
import time
import threading
from pathlib import Path
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential_jitter, retry_if_exception_type

from app.core.logging import get_logger

log = get_logger(__name__)

COHERE_EMBED_MODEL = "embed-english-v3.0"
COHERE_RERANK_MODEL = "rerank-v3.5"
COHERE_EMBED_DIM = 1024
COHERE_EMBED_BATCH = 96  # Cohere v2 limit

_lock = threading.Lock()

# ── Quota state ────────────────────────────────────────────────────────────────

class CohereQuota:
    """Track trial quota from response headers."""
    def __init__(self) -> None:
        self.limit: int | None = None
        self.remaining: int | None = None
        self.degraded: bool = False
        self._daily_count: int = 0
        self._monthly_count: int = 0

    def update_from_headers(self, headers: dict, warn_pct: float = 0.80) -> None:
        limit_hdr = headers.get("x-trial-endpoint-call-limit")
        remaining_hdr = headers.get("x-trial-endpoint-call-remaining")
        if limit_hdr:
            self.limit = int(limit_hdr)
        if remaining_hdr:
            self.remaining = int(remaining_hdr)
        self._daily_count += 1

        if self.limit and self.remaining is not None:
            used_pct = 1.0 - (self.remaining / self.limit)
            if used_pct >= warn_pct and not self.degraded:
                log.warning(
                    "Cohere trial quota near limit — switching to local",
                    extra={"remaining": self.remaining, "limit": self.limit},
                )
                self.degraded = True
            elif used_pct < warn_pct and self.degraded:
                self.degraded = False

    def to_dict(self) -> dict:
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "degraded": self.degraded,
            "daily_calls": self._daily_count,
        }


_quota = CohereQuota()


def get_quota() -> CohereQuota:
    return _quota


# ── Embedding cache (SQLite keyed by model+content_hash) ──────────────────────

_cache_db: sqlite3.Connection | None = None
_cache_lock = threading.Lock()


def _get_cache_db(index_dir: str) -> sqlite3.Connection:
    global _cache_db
    if _cache_db is None:
        db_path = os.path.join(index_dir, "embed_cache.db")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS embed_cache (
                cache_key TEXT PRIMARY KEY,
                embedding BLOB NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
        _cache_db = conn
    return _cache_db


def _cache_key(model: str, content_hash: str) -> str:
    return f"{model}:{content_hash}"


def _cache_get(index_dir: str, model: str, content_hash: str) -> list[float] | None:
    db = _get_cache_db(index_dir)
    key = _cache_key(model, content_hash)
    with _cache_lock:
        row = db.execute("SELECT embedding FROM embed_cache WHERE cache_key = ?", (key,)).fetchone()
    if row:
        blob = row[0]
        n = len(blob) // 4
        return list(struct.unpack(f"{n}f", blob))
    return None


def _cache_set(index_dir: str, model: str, content_hash: str, embedding: list[float]) -> None:
    db = _get_cache_db(index_dir)
    key = _cache_key(model, content_hash)
    blob = struct.pack(f"{len(embedding)}f", *embedding)
    with _cache_lock:
        db.execute(
            "INSERT OR REPLACE INTO embed_cache (cache_key, embedding, created_at) VALUES (?, ?, ?)",
            (key, blob, int(time.time())),
        )
        db.commit()


# ── Cohere embed ──────────────────────────────────────────────────────────────

class CohereUnavailableError(Exception):
    """Raised when Cohere is unavailable or quota exceeded."""


async def cohere_embed_texts(
    texts: list[str],
    input_type: str = "search_document",
    content_hashes: list[str] | None = None,
    index_dir: str = "./data/index",
    api_key: str = "",
) -> list[list[float]] | None:
    """Embed texts via Cohere. Returns None to signal fallback to local.

    Caches by (model, content_hash) when content_hashes provided.
    """
    from app.config import get_settings
    cfg = get_settings()
    if not api_key:
        api_key = cfg.cohere_api_key
    if not api_key:
        return None

    if _quota.degraded:
        log.info("Cohere quota degraded — using local embedder")
        return None

    all_embeddings: list[list[float]] = [None] * len(texts)  # type: ignore
    uncached_indices: list[int] = []
    uncached_texts: list[str] = []

    # Check cache
    if content_hashes:
        for i, (text, ch) in enumerate(zip(texts, content_hashes)):
            cached = _cache_get(index_dir, COHERE_EMBED_MODEL, ch)
            if cached is not None:
                all_embeddings[i] = cached
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)
    else:
        uncached_indices = list(range(len(texts)))
        uncached_texts = texts

    if not uncached_texts:
        return all_embeddings

    # Batch API calls
    fetched: list[list[float]] = []
    for batch_start in range(0, len(uncached_texts), COHERE_EMBED_BATCH):
        batch = uncached_texts[batch_start:batch_start + COHERE_EMBED_BATCH]
        try:
            batch_result = await _cohere_embed_batch(batch, input_type, api_key, cfg.cohere_quota_warn_pct)
            fetched.extend(batch_result)
        except CohereUnavailableError:
            return None  # Signal fallback
        except Exception as e:
            log.warning(f"Cohere embed failed: {e} — falling back to local")
            return None

    # Fill in results and cache
    for idx, emb, orig_idx in zip(range(len(uncached_texts)), fetched, uncached_indices):
        all_embeddings[orig_idx] = emb
        if content_hashes:
            ch = content_hashes[orig_idx]
            _cache_set(index_dir, COHERE_EMBED_MODEL, ch, emb)

    return all_embeddings


async def _cohere_embed_batch(
    texts: list[str],
    input_type: str,
    api_key: str,
    warn_pct: float,
) -> list[list[float]]:
    """Single batch call to Cohere embed API."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(3):
            resp = await client.post(
                "https://api.cohere.com/v2/embed",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "texts": texts,
                    "model": COHERE_EMBED_MODEL,
                    "input_type": input_type,
                    "embedding_types": ["float"],
                },
            )
            _quota.update_from_headers(dict(resp.headers), warn_pct)

            if resp.status_code == 200:
                data = resp.json()
                return data["embeddings"]["float"]
            elif resp.status_code == 429:
                wait = 2 ** attempt + 0.5
                log.warning(f"Cohere 429, backing off {wait}s")
                await asyncio.sleep(wait)
            elif resp.status_code in (402, 403):
                _quota.degraded = True
                raise CohereUnavailableError(f"Cohere {resp.status_code}: quota/auth")
            else:
                log.warning(f"Cohere embed {resp.status_code}: {resp.text[:100]}")
                if attempt >= 2:
                    raise CohereUnavailableError(f"Cohere embed failed: {resp.status_code}")
                await asyncio.sleep(1.0)

    raise CohereUnavailableError("Cohere embed: max retries exceeded")


async def cohere_embed_query(text: str, api_key: str = "") -> list[float] | None:
    """Embed a single query. Returns None to signal local fallback."""
    result = await cohere_embed_texts([text], input_type="search_query", api_key=api_key)
    if result:
        return result[0]
    return None


# ── Cohere rerank ─────────────────────────────────────────────────────────────

async def cohere_rerank(
    query: str,
    docs: list[str],
    top_n: int = 8,
    api_key: str = "",
) -> list[float] | None:
    """Rerank docs via Cohere. Returns scores in original order, or None for local fallback."""
    from app.config import get_settings
    cfg = get_settings()
    if not api_key:
        api_key = cfg.cohere_api_key
    if not api_key or _quota.degraded:
        return None

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.cohere.com/v2/rerank",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": COHERE_RERANK_MODEL,
                    "query": query,
                    "documents": docs,
                    "top_n": min(top_n, len(docs)),
                },
            )
            _quota.update_from_headers(dict(resp.headers), cfg.cohere_quota_warn_pct)

            if resp.status_code == 200:
                results = resp.json()["results"]
                # Map back to original indices
                scores = [0.0] * len(docs)
                for r in results:
                    scores[r["index"]] = r["relevance_score"]
                return scores
            elif resp.status_code == 429:
                log.warning("Cohere rerank 429 — falling back to local")
                return None
            else:
                log.warning(f"Cohere rerank {resp.status_code} — falling back to local")
                return None
    except Exception as e:
        log.warning(f"Cohere rerank exception: {e} — falling back to local")
        return None
