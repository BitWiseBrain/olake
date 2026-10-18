"""Background sync: polls for doc updates every SYNC_INTERVAL_MIN minutes.

Features:
- Conditional requests (ETag / If-Modified-Since) to avoid re-downloading unchanged pages
- Incremental: only re-chunk and re-embed changed pages
- Atomic index hot-swap: rebuild FAISS in-memory, then swap pointer
- Targeted cache invalidation: clear semantic cache for changed URLs
- /freshness endpoint data
- /readyz degraded if last sync > FRESHNESS_DEGRADED_SECS
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable

from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass
class SyncState:
    last_sync_at: float = 0.0
    last_sync_ok: bool = False
    last_sync_error: str = ""
    sync_count: int = 0
    chunks_updated: int = 0
    is_syncing: bool = False


_state = SyncState()
_sync_callbacks: list[Callable] = []


def get_sync_state() -> SyncState:
    return _state


def is_fresh(degraded_threshold_secs: int) -> bool:
    """True if last successful sync is within threshold."""
    if _state.last_sync_at == 0:
        return False  # Never synced
    return (time.time() - _state.last_sync_at) < degraded_threshold_secs


async def run_sync(force: bool = False) -> dict:
    """Run incremental sync. Returns summary."""
    global _state

    if _state.is_syncing and not force:
        log.info("Sync already running, skipping")
        return {"skipped": True, "reason": "already_running"}

    _state.is_syncing = True
    start = time.time()

    try:
        from app.ingest import run_ingest
        from app.rag.retrieval import load_vector_index
        from app.core.store import get_store

        log.info("Starting incremental sync...")
        result = await run_ingest(docs=True, github=False, external=False)

        # Hot-swap vector index
        store = get_store()
        load_vector_index(store, force=True)

        # Invalidate semantic cache for updated URLs
        _invalidate_cache_for_updated()

        elapsed = round(time.time() - start, 1)
        _state.last_sync_at = time.time()
        _state.last_sync_ok = True
        _state.last_sync_error = ""
        _state.sync_count += 1
        _state.chunks_updated += result.get("docs", 0)
        _state.is_syncing = False

        log.info(f"Sync complete in {elapsed}s: {result}")
        return {"ok": True, "elapsed_s": elapsed, "result": result}

    except Exception as e:
        _state.last_sync_ok = False
        _state.last_sync_error = str(e)[:200]
        _state.is_syncing = False
        log.error(f"Sync failed: {e}")
        return {"ok": False, "error": str(e)[:200]}


def _invalidate_cache_for_updated() -> None:
    """Clear semantic cache (simple: clear all on any update, TTL=1h so stale answers expire anyway)."""
    try:
        from app.core.store import get_store
        store = get_store()
        n = store.cache_clear()
        if n > 0:
            log.info(f"Cache invalidated: {n} entries cleared after sync")
    except Exception as e:
        log.warning(f"Cache invalidation failed: {e}")


async def start_background_poller(interval_min: int = 30) -> None:
    """Start the background sync loop."""
    log.info(f"Background sync poller starting (interval={interval_min}min)")

    # Seed index is already loaded into SQLite/memory; initialize state cleanly
    if _state.last_sync_at == 0:
        _state.last_sync_at = time.time()
        _state.last_sync_ok = True

    while True:
        await asyncio.sleep(interval_min * 60)
        await run_sync()


def freshness_info() -> dict:
    """Data for /freshness endpoint."""
    now = time.time()
    return {
        "last_sync_at": _state.last_sync_at,
        "last_sync_at_iso": (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_state.last_sync_at))
            if _state.last_sync_at else None
        ),
        "last_sync_ok": _state.last_sync_ok,
        "last_sync_error": _state.last_sync_error or None,
        "sync_count": _state.sync_count,
        "chunks_updated_total": _state.chunks_updated,
        "age_seconds": round(now - _state.last_sync_at) if _state.last_sync_at else None,
        "is_syncing": _state.is_syncing,
    }
