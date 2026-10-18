"""Unified embed + rerank interface.

Routes to Cohere or local fastembed based on EMBED_PROVIDER / RERANK_PROVIDER.
Never mixes vectors from different models in the same index.
Index metadata tracks which model was used so we can detect mismatches.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import NamedTuple

from app.core.logging import get_logger

log = get_logger(__name__)

# ── Model registry ─────────────────────────────────────────────────────────────

class EmbedModelInfo(NamedTuple):
    provider: str   # "local" | "cohere"
    model_name: str
    dim: int


_BGE_INFO = EmbedModelInfo("local", "BAAI/bge-small-en-v1.5", 384)
_COHERE_INFO = EmbedModelInfo("cohere", "embed-english-v3.0", 1024)

_local_embedder = None
_local_reranker = None
_local_lock = threading.Lock()
_BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def _load_local_embedder():
    global _local_embedder
    if _local_embedder is None:
        with _local_lock:
            if _local_embedder is None:
                from fastembed import TextEmbedding
                log.info("Loading local embedding model: BAAI/bge-small-en-v1.5")
                _local_embedder = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
                log.info("Local embedding model loaded")
    return _local_embedder


def _load_local_reranker():
    global _local_reranker
    if _local_reranker is None:
        with _local_lock:
            if _local_reranker is None:
                try:
                    from fastembed import TextCrossEncoder
                    log.info("Loading local reranker: Xenova/ms-marco-MiniLM-L-6-v2")
                    _local_reranker = TextCrossEncoder(model_name="Xenova/ms-marco-MiniLM-L-6-v2")
                    log.info("Local reranker loaded")
                except Exception as e:
                    log.warning(f"Local reranker unavailable: {e}")
                    _local_reranker = None
    return _local_reranker


def get_active_embed_info() -> EmbedModelInfo:
    """Return which embedding model is active (for index metadata)."""
    from app.config import get_settings
    cfg = get_settings()
    if cfg.use_cohere_embed:
        from app.core.cohere_client import get_quota
        if not get_quota().degraded:
            return _COHERE_INFO
    return _BGE_INFO


# ── Document embedding ─────────────────────────────────────────────────────────

def embed_documents_local(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    embedder = _load_local_embedder()
    return [r.tolist() for r in embedder.embed(texts)]


async def embed_documents(
    texts: list[str],
    content_hashes: list[str] | None = None,
    index_dir: str = "./data/index",
) -> tuple[list[list[float]], EmbedModelInfo]:
    """Embed documents. Returns (embeddings, model_info).

    Falls back from Cohere→local automatically.
    content_hashes enables disk cache for Cohere calls.
    """
    from app.config import get_settings
    cfg = get_settings()

    if cfg.use_cohere_embed:
        from app.core.cohere_client import cohere_embed_texts, get_quota
        if not get_quota().degraded:
            result = await cohere_embed_texts(
                texts,
                input_type="search_document",
                content_hashes=content_hashes,
                index_dir=index_dir,
                api_key=cfg.cohere_api_key,
            )
            if result is not None:
                return result, _COHERE_INFO
            log.info("Cohere embed unavailable — falling back to local")

    # Local fastembed
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, embed_documents_local, texts)
    return result, _BGE_INFO


# ── Query embedding ───────────────────────────────────────────────────────────

def embed_query_local(text: str) -> list[float]:
    embedder = _load_local_embedder()
    prefixed = _BGE_QUERY_PREFIX + text
    return list(embedder.embed([prefixed]))[0].tolist()


async def embed_query(text: str, index_dir: str = "./data/index") -> tuple[list[float], EmbedModelInfo]:
    """Embed a query. Returns (embedding, model_info).

    Must use the same provider as the indexed documents.
    """
    from app.config import get_settings
    cfg = get_settings()

    # Load index metadata to check which model was used for documents
    meta = _load_index_metadata(index_dir)
    index_provider = meta.get("provider", "local")

    # If index was built with Cohere, query must also use Cohere
    if index_provider == "cohere" and cfg.cohere_api_key:
        from app.core.cohere_client import cohere_embed_query, get_quota
        if not get_quota().degraded:
            result = await cohere_embed_query(text, api_key=cfg.cohere_api_key)
            if result is not None:
                return result, _COHERE_INFO
        # Cohere unavailable but index is Cohere-embedded → BM25 only, return zeros
        log.warning("Cohere unavailable but index is Cohere-embedded — vector search degraded")
        return [0.0] * _COHERE_INFO.dim, _COHERE_INFO

    # Local
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, embed_query_local, text)
    return result, _BGE_INFO


# ── Reranking ─────────────────────────────────────────────────────────────────

def rerank_local(query: str, docs: list[str]) -> list[float]:
    reranker = _load_local_reranker()
    if reranker is None:
        return [0.0] * len(docs)
    pairs = [(query, doc) for doc in docs]
    return [float(s) for s in reranker.rerank(pairs)]


async def rerank(query: str, docs: list[str], top_n: int = 8) -> list[float]:
    """Rerank docs. Returns scores in original order."""
    from app.config import get_settings
    cfg = get_settings()

    if cfg.use_cohere_rerank:
        from app.core.cohere_client import cohere_rerank, get_quota
        if not get_quota().degraded:
            result = await cohere_rerank(query, docs, top_n=top_n)
            if result is not None:
                return result
            log.info("Cohere rerank unavailable — falling back to local")

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, rerank_local, query, docs)


# ── Index metadata ────────────────────────────────────────────────────────────

def _index_meta_path(index_dir: str) -> str:
    return os.path.join(index_dir, "index_meta.json")


def _load_index_metadata(index_dir: str) -> dict:
    path = _index_meta_path(index_dir)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_index_metadata(index_dir: str, provider: str, model_name: str, dim: int, chunk_count: int) -> None:
    import time
    meta = {
        "provider": provider,
        "model_name": model_name,
        "dim": dim,
        "chunk_count": chunk_count,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    os.makedirs(index_dir, exist_ok=True)
    with open(_index_meta_path(index_dir), "w") as f:
        json.dump(meta, f, indent=2)
    log.info(f"Index metadata saved: {meta}")


def load_index_metadata(index_dir: str) -> dict:
    return _load_index_metadata(index_dir)


# ── Preload for startup ───────────────────────────────────────────────────────

def preload_models() -> None:
    """Warm up local models at startup."""
    _load_local_embedder()
    _load_local_reranker()
