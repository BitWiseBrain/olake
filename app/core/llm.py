"""OpenRouter LLM provider — single implementation using openai SDK.

Supports:
- Primary model with native fallback via extra_body["models"]
- reasoning: {effort: none} optimization (skipped per-model if it errors)
- Budget guard: budget_exhausted flag causes DEGRADED MODE
- Spend tracking from usage metadata
- Circuit breaker on 402/403
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from openai import AsyncOpenAI, APIStatusError, APIConnectionError, RateLimitError

from app.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)


class LLMError(Exception):
    pass


class BudgetExhaustedError(LLMError):
    pass


# Per-model flags: True = model does NOT support reasoning field
_reasoning_unsupported: set[str] = set()

# Global spend tracker
_total_spend_usd: float = 0.0
_limit_remaining: float | None = None
_budget_degraded: bool = False
_last_budget_check: float = 0.0


def get_spend() -> float:
    return _total_spend_usd


def is_degraded() -> bool:
    return _budget_degraded


def get_limit_remaining() -> float | None:
    return _limit_remaining


async def check_budget() -> None:
    """Check OpenRouter key balance. Sets _budget_degraded if low."""
    global _limit_remaining, _budget_degraded, _last_budget_check
    cfg = get_settings()
    now = time.time()
    if now - _last_budget_check < cfg.budget_check_interval_secs:
        return
    _last_budget_check = now
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/key",
                headers={"Authorization": f"Bearer {cfg.openrouter_api_key}"},
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                _limit_remaining = data.get("limit_remaining")
                if _limit_remaining is not None and _limit_remaining < cfg.budget_degraded_threshold:
                    if not _budget_degraded:
                        log.warning(
                            "Budget degraded mode activated",
                            extra={"limit_remaining": _limit_remaining},
                        )
                    _budget_degraded = True
                else:
                    _budget_degraded = False
                log.info(
                    "Budget check",
                    extra={"limit_remaining": _limit_remaining, "degraded": _budget_degraded},
                )
    except Exception as e:
        log.warning(f"Budget check failed: {e}")


def _make_client() -> AsyncOpenAI:
    cfg = get_settings()
    return AsyncOpenAI(
        api_key=cfg.openrouter_api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://olake.io",
            "X-Title": "Ask OLake",
        },
        max_retries=0,  # we handle retries manually
    )


async def _call_generate(
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
    use_reasoning: bool = True,
    attempt: int = 0,
) -> tuple[str, str, dict]:
    """Single generation call. Returns (text, model_used, usage)."""
    cfg = get_settings()
    client = _make_client()

    extra_body: dict[str, Any] = {
        "models": cfg.fallback_models,
    }

    primary = cfg.gen_model
    if use_reasoning and primary not in _reasoning_unsupported:
        extra_body["reasoning"] = {"effort": "none"}

    try:
        resp = await client.chat.completions.create(
            model=primary,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
            extra_body=extra_body,
        )

        text = resp.choices[0].message.content or ""
        model_used = getattr(resp, "model", primary)
        usage = {
            "prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
            "completion_tokens": resp.usage.completion_tokens if resp.usage else 0,
        }
        log.info("LLM response", extra={"model_used": model_used, "usage": usage})
        return text, model_used, usage

    except APIStatusError as e:
        status = e.status_code
        if status == 401:
            raise LLMError("OpenRouter 401: invalid API key — check OPENROUTER_API_KEY") from e
        if status in (402, 403):
            raise BudgetExhaustedError(f"OpenRouter {status}: credit exhausted or bad model ID") from e
        if status == 429:
            wait = 2 ** min(attempt, 4) + 0.5
            log.warning(f"Rate limit hit, backing off {wait}s")
            await asyncio.sleep(wait)
            return await _call_generate(system, user, max_tokens, temperature, use_reasoning, attempt + 1)
        if status in (502, 503):
            if attempt < 2:
                await asyncio.sleep(1.5 ** attempt)
                return await _call_generate(system, user, max_tokens, temperature, use_reasoning, attempt + 1)
            raise LLMError(f"OpenRouter {status}: upstream error after retries") from e
        # Possibly reasoning unsupported
        if use_reasoning and attempt == 0:
            log.info(f"Retrying without reasoning for {primary}")
            _reasoning_unsupported.add(primary)
            return await _call_generate(system, user, max_tokens, temperature, False, 1)
        raise LLMError(f"OpenRouter {status}: {e.message}") from e

    except APIConnectionError as e:
        if attempt < 2:
            await asyncio.sleep(1.5 ** attempt)
            return await _call_generate(system, user, max_tokens, temperature, use_reasoning, attempt + 1)
        raise LLMError(f"OpenRouter connection error: {e}") from e


async def generate(
    system: str,
    user: str,
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    """Generate text. Raises LLMError on failure."""
    global _total_spend_usd
    cfg = get_settings()
    text, model_used, usage = await _call_generate(
        system,
        user,
        max_tokens=max_tokens or cfg.max_output_tokens,
        temperature=temperature if temperature is not None else cfg.llm_temperature,
    )
    # Rough cost estimate: assume $0.0001/1k tokens average for these small models
    tokens = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
    _total_spend_usd += tokens * 0.0001 / 1000
    return text


async def generate_with_usage(
    system: str,
    user: str,
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> tuple[str, dict]:
    """Generate and return (text, usage_dict)."""
    global _total_spend_usd
    cfg = get_settings()
    text, model_used, usage = await _call_generate(
        system,
        user,
        max_tokens=max_tokens or cfg.max_output_tokens,
        temperature=temperature if temperature is not None else cfg.llm_temperature,
    )
    usage["model_used"] = model_used
    tokens = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
    cost_usd = tokens * 0.0001 / 1000
    _total_spend_usd += cost_usd
    usage["cost_usd"] = round(cost_usd, 6)
    return text, usage


async def health_check() -> bool:
    """Verify OpenRouter is reachable."""
    try:
        result = await generate("You are a health check bot.", "Reply with: ok", max_tokens=5)
        return len(result) > 0
    except BudgetExhaustedError:
        return False  # key works but budget gone — still "down" for generation
    except Exception:
        return False
