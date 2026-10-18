"""Stats tracker for /admin/stats endpoint."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock

_lock = Lock()


@dataclass
class RequestStats:
    total_requests: int = 0
    cache_hits_exact: int = 0
    cache_hits_semantic: int = 0
    cache_misses: int = 0
    llm_calls: int = 0
    cohere_embed_calls: int = 0
    cohere_rerank_calls: int = 0
    extractive_degraded: int = 0
    errors: int = 0
    # Latency tracking (last 1000 requests)
    _latencies: deque = field(default_factory=lambda: deque(maxlen=1000))
    model_usage: dict = field(default_factory=dict)


_stats = RequestStats()


def record_request(
    *,
    cache_hit_exact: bool = False,
    cache_hit_semantic: bool = False,
    latency_ms: float = 0,
    model_used: str = "",
    extractive: bool = False,
    error: bool = False,
    cohere_embed: bool = False,
    cohere_rerank: bool = False,
) -> None:
    with _lock:
        _stats.total_requests += 1
        if cache_hit_exact:
            _stats.cache_hits_exact += 1
        elif cache_hit_semantic:
            _stats.cache_hits_semantic += 1
        else:
            _stats.cache_misses += 1
        if not cache_hit_exact and not cache_hit_semantic and not extractive and not error:
            _stats.llm_calls += 1
        if cohere_embed:
            _stats.cohere_embed_calls += 1
        if cohere_rerank:
            _stats.cohere_rerank_calls += 1
        if extractive:
            _stats.extractive_degraded += 1
        if error:
            _stats.errors += 1
        if latency_ms:
            _stats._latencies.append(latency_ms)
        if model_used:
            _stats.model_usage[model_used] = _stats.model_usage.get(model_used, 0) + 1


def get_stats() -> dict:
    from app.core.llm import get_spend, is_degraded, get_limit_remaining
    from app.core.cohere_client import get_quota

    with _lock:
        lats = sorted(_stats._latencies)
        p50 = lats[len(lats) // 2] if lats else 0
        p95 = lats[int(len(lats) * 0.95)] if lats else 0
        total = _stats.total_requests
        cache_hits = _stats.cache_hits_exact + _stats.cache_hits_semantic

        return {
            "total_requests": total,
            "cache_hit_rate": round(cache_hits / max(total, 1), 3),
            "cache_hits_exact": _stats.cache_hits_exact,
            "cache_hits_semantic": _stats.cache_hits_semantic,
            "llm_calls": _stats.llm_calls,
            "extractive_degraded_responses": _stats.extractive_degraded,
            "errors": _stats.errors,
            "latency_p50_ms": round(p50, 1),
            "latency_p95_ms": round(p95, 1),
            "openrouter_spend_usd": round(get_spend(), 6),
            "openrouter_limit_remaining": get_limit_remaining(),
            "openrouter_budget_degraded": is_degraded(),
            "cohere_embed_calls": _stats.cohere_embed_calls,
            "cohere_rerank_calls": _stats.cohere_rerank_calls,
            "cohere_quota": get_quota().to_dict(),
            "model_usage": dict(_stats.model_usage),
        }
