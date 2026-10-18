"""FastAPI application: all endpoints, middleware, startup/shutdown."""

import asyncio
import hashlib
import re
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response, Header, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import os

from app.config import get_settings
from app.core import llm as llm_mod
from app.core.logging import setup_logging, get_logger, new_request_id, LatencyTimer
from app.core.stats import record_request, get_stats
from app.core.store import get_store

log = get_logger(__name__)

# ── Globals ───────────────────────────────────────────────────────────────────

_index_loaded = False
_startup_error = ""
_semaphore: asyncio.Semaphore | None = None

# ── Rate limiter ──────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _index_loaded, _startup_error, _semaphore
    cfg = get_settings()
    setup_logging(cfg.log_level)
    log.info("Ask OLake starting up", extra={"env": cfg.app_env})

    _semaphore = asyncio.Semaphore(cfg.max_concurrent_requests)

    # Preload local embedding models (optional — skip gracefully on Vercel/serverless)
    try:
        from app.core.embeddings import preload_models
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, preload_models)
    except Exception as e:
        log.info(f"Model preload skipped (will use BM25-only or API embeddings): {e}")

    # Load vector index (optional — BM25 works without it)
    try:
        from app.rag.retrieval import load_vector_index
        store = get_store()
        load_vector_index(store)
        _index_loaded = True
        log.info(f"Index loaded: {store.count_chunks()} chunks")
    except Exception as e:
        log.warning(f"Vector index load skipped: {e}")
        # Mark loaded if we have chunks (BM25 is sufficient)
        try:
            _index_loaded = get_store().count_chunks() > 0
        except Exception:
            _index_loaded = False

    # Initial budget check
    asyncio.create_task(llm_mod.check_budget())

    # Start background poller
    asyncio.create_task(
        _run_background_poller(cfg.sync_interval_min)
    )

    yield

    log.info("Ask OLake shutting down")


async def _run_background_poller(interval_min: int) -> None:
    from app.background import start_background_poller
    await start_background_poller(interval_min)


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    cfg = get_settings()

    app = FastAPI(
        title="Ask OLake",
        description="RAG-powered documentation assistant for OLake",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # Rate limiter
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_json_handler)

    # CORS
    cors_list = cfg.cors_origins_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_list,
        allow_credentials="*" not in cors_list,  # credentials only if not wildcard
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Security headers middleware
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if cfg.app_env == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    # Request ID middleware
    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        rid = new_request_id()
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    # Register routes
    app.include_router(router)

    # Serve frontend if built
    frontend_dist = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
    if os.path.exists(frontend_dist):
        app.mount("/static", StaticFiles(directory=frontend_dist), name="static")

        @app.get("/demo")
        async def demo():
            demo_path = os.path.join(frontend_dist, "demo.html")
            if os.path.exists(demo_path):
                return FileResponse(demo_path)
            raise HTTPException(404, "demo.html not found")

    return app


def _rate_limit_json_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"error": "rate_limit_exceeded", "detail": "Too many requests. Please slow down."},
        headers={"Retry-After": "60"},
    )


# ── Input validation ──────────────────────────────────────────────────────────

CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")


def _sanitize_question(q: str) -> str:
    return CONTROL_CHARS_RE.sub("", q).strip()


class AskRequest(BaseModel):
    question: str
    history: list[dict] | None = None  # optional for multi-turn; not stored server-side

    @field_validator("question")
    @classmethod
    def validate_question(cls, v: str) -> str:
        v = CONTROL_CHARS_RE.sub("", v).strip()
        cfg = get_settings()
        if not v:
            raise ValueError("question must not be empty")
        if len(v) > cfg.max_question_length:
            raise ValueError(f"question exceeds {cfg.max_question_length} character limit")
        return v


class AskResponse(BaseModel):
    answer: str
    sources: list[str]


class FeedbackRequest(BaseModel):
    question: str
    answer: str
    rating: int  # 1 = thumbs up, -1 = thumbs down
    comment: str = ""


AskRequest.model_rebuild()
AskResponse.model_rebuild()
FeedbackRequest.model_rebuild()


# ── Router ────────────────────────────────────────────────────────────────────

from fastapi import APIRouter
router = APIRouter()


@router.get("/healthz")
@router.get("/livez")
async def healthz():
    """Liveness probe."""
    return {"status": "ok", "ts": time.time()}


@router.get("/readyz")
async def readyz():
    """Readiness probe: index loaded + LLM reachable."""
    from app.background import get_sync_state, is_fresh
    from app.core.cohere_client import get_quota

    cfg = get_settings()
    store = get_store()
    chunk_count = store.count_chunks()
    sync_state = get_sync_state()
    budget_ok = not llm_mod.is_degraded()
    fresh = is_fresh(cfg.freshness_degraded_secs)

    issues = []
    is_loaded = _index_loaded or (chunk_count > 0)
    if not is_loaded:
        issues.append("index_not_loaded")
    if chunk_count == 0:
        issues.append("empty_index")
    if not budget_ok:
        issues.append("budget_degraded")
    if not fresh and sync_state.sync_count > 0:
        issues.append("stale_index")
    if get_quota().degraded:
        issues.append("cohere_degraded")

    status = "ok" if not issues else "degraded"
    code = 200 if status == "ok" else 503 if "index_not_loaded" in issues else 200

    return JSONResponse(
        status_code=code,
        content={
            "status": status,
            "issues": issues,
            "chunk_count": chunk_count,
            "budget_degraded": not budget_ok,
            "index_fresh": fresh,
            "last_sync_ok": sync_state.last_sync_ok,
        },
    )


@router.get("/freshness")
async def freshness():
    """Freshness info: last sync time, staleness."""
    from app.background import freshness_info
    return freshness_info()


@router.get("/version")
async def version():
    """Return the deployed commit SHA and deployment ID."""
    commit = (
        os.environ.get("VERCEL_GIT_COMMIT_SHA")
        or os.environ.get("RENDER_GIT_COMMIT")
        or os.environ.get("GIT_COMMIT")
        or ""
    )
    if not commit:
        try:
            import subprocess
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
            ).decode().strip()
        except Exception:
            pass

    deployment = (
        os.environ.get("VERCEL_DEPLOYMENT_ID")
        or os.environ.get("RENDER_INSTANCE_ID")
        or os.environ.get("RENDER_SERVICE_ID")
        or (f"dpl_{commit[:12]}" if commit else "")
    )
    return {
        "commit": commit,
        "deployment": deployment,
    }


@router.get("/models")
async def models_route():
    """Return the OpenRouter model IDs available to this API key (live from OpenRouter)."""
    import httpx as _httpx
    cfg = get_settings()
    api_key = cfg.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return JSONResponse(status_code=500, content={"error": "OPENROUTER_API_KEY not set"})
    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/models/user",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return {"models": [m["id"] for m in data]}
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": str(e)})


@router.post("/ask", response_model=AskResponse)
@limiter.limit("120/minute")
async def ask(request: Request, body: AskRequest = Body(...)):
    """Main RAG endpoint. Returns exactly {answer, sources}."""
    global _semaphore
    timer = LatencyTimer()
    cfg = get_settings()

    if _semaphore is None:
        _semaphore = asyncio.Semaphore(cfg.max_concurrent_requests)

    async with _semaphore:
        question = body.question
        history = body.history

        # ── Cache lookup ────────────────────────────────────────────────────
        normalized = _normalize(question)
        q_hash = hashlib.sha256(normalized.encode()).hexdigest()[:16]
        store = get_store()

        if cfg.enable_semantic_cache:
            # Exact match first (free)
            cached = store.cache_get(q_hash)
            if cached:
                timer.mark("cache_exact")
                record_request(cache_hit_exact=True, latency_ms=timer.total_ms())
                log.info("Cache hit (exact)", extra={"latency": timer.to_dict()})
                return AskResponse(answer=cached["answer"], sources=cached["sources"])

        # ── Query understanding ──────────────────────────────────────────────
        from app.rag.query import understand_query
        qu = await understand_query(question, history)
        timer.mark("query_understand")

        # Handle out-of-scope / injection / smalltalk
        if qu.is_injection:
            answer = "I can only answer questions about OLake documentation."
            return AskResponse(answer=answer, sources=[])

        if qu.is_smalltalk:
            answer = (
                "👋 Hi! I'm Ask OLake, your OLake documentation assistant. "
                "Ask me anything about setting up OLake, configuring sources, "
                "CDC modes, catalogs, Fusion table maintenance, or deployment!"
            )
            return AskResponse(answer=answer, sources=[])

        if qu.is_out_of_scope:
            answer = (
                "I'm specialized in OLake documentation and can't help with that topic. "
                "Try asking about OLake sources, sync modes, catalogs, or deployment!"
            )
            return AskResponse(answer=answer, sources=[])

        # ── Semantic cache ──────────────────────────────────────────────────
        q_vec = None
        if cfg.enable_semantic_cache:
            try:
                from app.core.embeddings import embed_query as _embed_query
                q_vec, _ = await _embed_query(qu.standalone_query, index_dir=cfg.index_path)
                if q_vec and any(v != 0.0 for v in q_vec):
                    sem_cached = store.cache_get_semantic(q_vec, threshold=0.95)
                    if sem_cached:
                        timer.mark("cache_semantic")
                        record_request(cache_hit_semantic=True, latency_ms=timer.total_ms())
                        log.info("Cache hit (semantic)", extra={"latency": timer.to_dict()})
                        return AskResponse(answer=sem_cached["answer"], sources=sem_cached["sources"])
            except Exception as e:
                log.info(f"Semantic cache skipped: {e}")
                q_vec = None

        # ── Retrieval ────────────────────────────────────────────────────────
        from app.rag.retrieval import retrieve
        retrieval = await retrieve(qu.standalone_query, qu.detected_meta, store)
        timer.mark("retrieve")

        # ── Confidence gate ─────────────────────────────────────────────────
        # Budget check (async, non-blocking)
        asyncio.create_task(llm_mod.check_budget())

        if retrieval.top_score < cfg.confidence_threshold and not retrieval.chunks:
            # No relevant context — go straight to "I don't know"
            from app.rag.generator import build_extractive_answer
            answer = (
                "I couldn't find this in the OLake docs or repos. "
                "Try the OLake Slack, GitHub Discussions at "
                "github.com/datazip-inc/olake/discussions, or search https://olake.io/docs."
            )
            return AskResponse(answer=answer, sources=[])

        # ── Generation ───────────────────────────────────────────────────────
        from app.rag.generator import generate_answer
        try:
            answer, sources, usage = await generate_answer(
                question,
                retrieval.chunks,
                history=history,
                standalone_query=qu.standalone_query,
            )
        except llm_mod.LLMError as e:
            log.warning(f"LLM error: {e} — falling back to extractive degraded mode")
            from app.rag.generator import build_extractive_answer
            answer, sources = build_extractive_answer(question, retrieval.chunks)
            usage = {"mode": "extractive_degraded", "error": str(e)}
        timer.mark("generate")

        # ── Cache store ──────────────────────────────────────────────────────
        if cfg.enable_semantic_cache and sources and q_vec and any(v != 0.0 for v in q_vec):
            try:
                store.cache_set(
                    normalized, q_hash, q_vec, answer, sources,
                    ttl=cfg.cache_ttl_seconds,
                )
            except Exception as e:
                log.warning(f"Cache store failed: {e}")

        # ── Log + stats ──────────────────────────────────────────────────────
        is_extractive = usage.get("mode") == "extractive_degraded"
        record_request(
            latency_ms=timer.total_ms(),
            model_used=usage.get("model_used", ""),
            extractive=is_extractive,
        )
        log.info(
            "Request complete",
            extra={
                "latency": timer.to_dict(),
                "total_ms": timer.total_ms(),
                "sources_count": len(sources),
                "bm25_hits": retrieval.bm25_count,
                "vector_hits": retrieval.vector_count,
                "top_score": round(retrieval.top_score, 3),
                "usage": usage,
            },
        )

        return AskResponse(answer=answer, sources=sources)


@router.post("/feedback")
async def feedback(body: FeedbackRequest = Body(...)):
    """Thumbs up/down feedback. Logged only, not stored in a hot path."""
    log.info(
        "Feedback received",
        extra={
            "rating": body.rating,
            "question_len": len(body.question),
            "comment_len": len(body.comment),
        },
    )
    return {"ok": True}


@router.post("/admin/reindex")
async def reindex(
    request: Request,
    authorization: str = Header(default=""),
    body: dict = Body(default={}),
):
    """Trigger incremental reindex. Protected by bearer token."""
    cfg = get_settings()
    token = authorization.replace("Bearer ", "").strip()
    if token != cfg.admin_token:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    from app.background import run_sync
    asyncio.create_task(run_sync(force=True))
    return {"ok": True, "message": "Reindex started in background"}


@router.get("/admin/stats")
async def admin_stats(authorization: str = Header(default="")):
    """Admin stats. Bearer-protected."""
    cfg = get_settings()
    token = authorization.replace("Bearer ", "").strip()
    if token != cfg.admin_token:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    return get_stats()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize(q: str) -> str:
    q = q.lower().strip()
    q = re.sub(r"\s+", " ", q)
    q = re.sub(r"[^\w\s?]", "", q)
    return q
