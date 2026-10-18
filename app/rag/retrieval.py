"""Hybrid retrieval: BM25 + FAISS vector search, fused with RRF.

Pipeline:
1. BM25 via SQLite FTS5 → top-30
2. Vector search via FAISS flat index → top-30
3. Reciprocal Rank Fusion → top-20
4. Metadata boost (connector/product match)
5. Dedup near-identical chunks
6. Optional cross-encoder rerank → top-6
7. Token budget trim
8. Confidence gate
"""
from __future__ import annotations

import asyncio
import os
import pickle
import time
from typing import NamedTuple

from app.config import get_settings
from app.core.logging import get_logger
from app.core.store import Chunk, ChunkStore, get_store
from app.core import embeddings as emb_mod

log = get_logger(__name__)

_faiss_index = None
_faiss_ids: list[str] = []
_np_vecs = None
_np_ids: list[str] = []


# ── FAISS index management ────────────────────────────────────────────────────

def load_vector_index(store: ChunkStore, force: bool = False) -> bool:
    global _faiss_index, _faiss_ids, _np_vecs, _np_ids
    cfg = get_settings()
    idx_path = os.path.join(cfg.index_path, "faiss.index")
    ids_path = os.path.join(cfg.index_path, "faiss.ids")

    try:
        import faiss
        has_faiss = True
    except ImportError:
        has_faiss = False

    if has_faiss:
        if not force and os.path.exists(idx_path) and os.path.exists(ids_path):
            try:
                _faiss_index = faiss.read_index(idx_path)
                with open(ids_path, "rb") as f:
                    _faiss_ids = pickle.load(f)
                log.info(f"FAISS index loaded: {_faiss_index.ntotal} vectors")
                return True
            except Exception as e:
                log.warning(f"FAISS load failed, rebuilding: {e}")

        # Rebuild
        ids, vecs = store.get_all_embeddings()
        if not ids:
            log.warning("No embeddings — vector index empty")
            return False

        import numpy as np
        mat = np.array(vecs, dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        mat = mat / (norms + 1e-9)
        dim = mat.shape[1]

        idx = faiss.IndexFlatIP(dim)
        idx.add(mat)
        _faiss_index = idx
        _faiss_ids = ids

        os.makedirs(cfg.index_path, exist_ok=True)
        faiss.write_index(idx, idx_path)
        with open(ids_path, "wb") as f:
            pickle.dump(ids, f)
        log.info(f"FAISS index built: {idx.ntotal} vectors, dim={dim}")
        return True
    else:
        # NumPy fallback
        ids, vecs = store.get_all_embeddings()
        if not ids:
            return False
        import numpy as np
        _np_ids = ids
        mat = np.array(vecs, dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        _np_vecs = mat / (norms + 1e-9)
        log.info(f"NumPy index loaded: {len(ids)} vectors")
        return True


def _vector_search_sync(query_vec: list[float], top_k: int) -> list[tuple[str, float]]:
    global _faiss_index, _faiss_ids, _np_vecs, _np_ids
    if _faiss_index is None and _np_vecs is None:
        load_vector_index(get_store())

    try:
        import faiss
        if _faiss_index is not None and _faiss_index.ntotal > 0:
            import numpy as np
            q = np.array([query_vec], dtype=np.float32)
            q = q / (np.linalg.norm(q) + 1e-9)
            k = min(top_k, _faiss_index.ntotal)
            scores, indices = _faiss_index.search(q, k)
            return [
                (_faiss_ids[i], float(s))
                for s, i in zip(scores[0], indices[0])
                if i >= 0 and i < len(_faiss_ids)
            ]
    except ImportError:
        pass

    # NumPy fallback
    if _np_vecs is not None and len(_np_ids) > 0:
        import numpy as np
        q = np.array(query_vec, dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-9)
        sims = _np_vecs @ q
        top_idx = sims.argsort()[-top_k:][::-1]
        return [(_np_ids[i], float(sims[i])) for i in top_idx]

    return []


# ── Reciprocal Rank Fusion ────────────────────────────────────────────────────

def reciprocal_rank_fusion(
    *ranked_lists: list[tuple[str, float]],
    k: int = 60,
) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, (doc_id, _) in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


# ── Metadata boosting ─────────────────────────────────────────────────────────

def _boost(
    fused: list[tuple[str, float]],
    chunk_map: dict[str, Chunk],
    query_meta: dict[str, str],
    boost: float = 0.15,
) -> list[tuple[str, float]]:
    result = []
    for cid, score in fused:
        chunk = chunk_map.get(cid)
        if chunk:
            extra = 0.0
            if query_meta.get("connector") and chunk.connector == query_meta["connector"]:
                extra += boost
            if query_meta.get("product") and chunk.product == query_meta["product"]:
                extra += boost * 0.5
            if query_meta.get("catalog_type") and chunk.catalog_type == query_meta["catalog_type"]:
                extra += boost * 0.3
            score += extra
        result.append((cid, score))
    return sorted(result, key=lambda x: x[1], reverse=True)


# ── Dedup + budget ────────────────────────────────────────────────────────────

def _dedup(chunks: list[Chunk]) -> list[Chunk]:
    seen_hashes: set[str] = set()
    seen_prefix: set[str] = set()
    out = []
    for c in chunks:
        if c.content_hash in seen_hashes:
            continue
        prefix = c.content[:80].strip()
        if prefix in seen_prefix:
            continue
        seen_hashes.add(c.content_hash)
        seen_prefix.add(prefix)
        out.append(c)
    return out


def _trim_to_budget(chunks: list[Chunk], max_tokens: int) -> list[Chunk]:
    out, total = [], 0
    for c in chunks:
        t = max(1, len(c.content) // 4)
        if total + t > max_tokens:
            break
        out.append(c)
        total += t
    return out


# ── Main retrieval ────────────────────────────────────────────────────────────

class RetrievalResult(NamedTuple):
    chunks: list[Chunk]
    top_score: float
    bm25_count: int
    vector_count: int


async def retrieve(
    query: str,
    query_meta: dict[str, str] | None = None,
    store: ChunkStore | None = None,
) -> RetrievalResult:
    cfg = get_settings()
    if store is None:
        store = get_store()

    # BM25 (always available via SQLite FTS5)
    bm25_results = store.bm25_search(query, top_k=cfg.bm25_top_k)

    # Vector search (optional — gracefully skip if embeddings/faiss unavailable)
    vector_results: list[tuple[str, float]] = []
    try:
        loop = asyncio.get_event_loop()
        query_vec, _ = await emb_mod.embed_query(query, index_dir=cfg.index_path)
        vector_results = await loop.run_in_executor(
            None, _vector_search_sync, query_vec, cfg.vector_top_k
        )
    except Exception as e:
        log.info(f"Vector search skipped (degraded to BM25-only): {e}")

    # RRF fusion
    fused = reciprocal_rank_fusion(bm25_results, vector_results)
    top_ids = [cid for cid, _ in fused[:20]]
    chunk_map = {c.id: c for c in store.get_chunks_by_ids(top_ids)}
    ordered = [chunk_map[cid] for cid in top_ids if cid in chunk_map]

    # Metadata boost
    if query_meta:
        boosted = _boost(
            [(c.id, s) for c, (_, s) in zip(ordered, fused[:20])],
            chunk_map, query_meta,
        )
        ordered = [chunk_map[cid] for cid, _ in boosted if cid in chunk_map]

    # Dedup
    ordered = _dedup(ordered)

    # Rerank
    if cfg.rerank_enabled and len(ordered) > 1:
        texts = [c.content[:1000] for c in ordered]
        try:
            scores = await emb_mod.rerank(query, texts, top_n=cfg.rerank_top_k)
            paired = sorted(zip(scores, ordered), key=lambda x: x[0], reverse=True)
            ordered = [c for _, c in paired[:cfg.rerank_top_k]]
        except Exception as e:
            log.warning(f"Rerank failed: {e}")
            ordered = ordered[:cfg.rerank_top_k]
    else:
        ordered = ordered[:cfg.rerank_top_k]

    # Token budget
    ordered = _trim_to_budget(ordered, cfg.max_context_tokens)

    # Normalize RRF score to 0-1
    top_score = min(1.0, (fused[0][1] if fused else 0.0) / 0.033)

    return RetrievalResult(
        chunks=ordered,
        top_score=top_score,
        bm25_count=len(bm25_results),
        vector_count=len(vector_results),
    )
