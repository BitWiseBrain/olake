#!/usr/bin/env python3
"""Smoke-test script: verify OpenRouter + Cohere keys before building.

Usage:
    python scripts/check_keys.py

Prints PASS/FAIL per service with error codes and remaining credits.
Makes exactly one embed + one rerank call to Cohere if COHERE_API_KEY is set.
Never runs in loops. Safe to run at any time.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Load .env if present (overrides any stale shell environment variables)
env_file = Path(__file__).parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            val = v.strip().strip("'\"")
            if val:
                os.environ[k.strip()] = val

import httpx

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
COHERE_KEY = os.environ.get("COHERE_API_KEY", "")

MODELS = [
    "z-ai/glm-4.7-flash",
    "google/gemini-2.5-flash-lite",
    "deepseek/deepseek-v4.1-flash",
]


def _mask(key: str) -> str:
    if not key:
        return "(not set)"
    return key[:8] + "..." + key[-4:]


async def check_openrouter() -> bool:
    print("\n══ OpenRouter ══════════════════════════════════════════")
    if not OPENROUTER_KEY:
        print("  ❌ OPENROUTER_API_KEY not set")
        return False

    print(f"  Key: {_mask(OPENROUTER_KEY)}")
    all_ok = True

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. Key info
        try:
            resp = await client.get(
                "https://openrouter.ai/api/v1/key",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY}"},
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                limit = data.get("limit")
                remaining = data.get("limit_remaining")
                usage = data.get("usage")
                print(f"  ✅ Key valid — limit=${limit}, remaining=${remaining}, used=${usage}")
                if remaining is not None and remaining < 0.50:
                    print(f"  ⚠️  WARNING: only ${remaining:.3f} remaining — budget degraded mode will activate!")
            else:
                print(f"  ❌ Key check failed: HTTP {resp.status_code}")
                all_ok = False
        except Exception as e:
            print(f"  ❌ Key check error: {e}")
            all_ok = False

        # 2. Each model
        for model in MODELS:
            try:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENROUTER_KEY}",
                        "HTTP-Referer": "https://olake.io",
                        "X-Title": "Ask OLake Key Check",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "Reply: ok"}],
                        "max_tokens": 5,
                        "temperature": 0,
                    },
                )
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"]
                    actual_model = resp.json().get("model", model)
                    print(f"  ✅ {model} → answered as {actual_model!r}: {text!r}")
                elif resp.status_code == 403:
                    print(f"  ❌ {model} → 403 Forbidden (bad model ID or no credits)")
                    all_ok = False
                elif resp.status_code == 402:
                    print(f"  ❌ {model} → 402 Payment Required (credit exhausted)")
                    all_ok = False
                else:
                    print(f"  ⚠️  {model} → HTTP {resp.status_code}: {resp.text[:100]}")
            except Exception as e:
                print(f"  ❌ {model} → Exception: {e}")
                all_ok = False

    return all_ok


async def check_cohere() -> bool:
    print("\n══ Cohere ══════════════════════════════════════════════")
    if not COHERE_KEY:
        print("  ℹ️  COHERE_API_KEY not set — skipping (EMBED_PROVIDER=local will be used)")
        return True  # Not a failure — local is the default

    print(f"  Key: {_mask(COHERE_KEY)}")
    all_ok = True

    async with httpx.AsyncClient(timeout=20.0) as client:
        headers = {
            "Authorization": f"Bearer {COHERE_KEY}",
            "Content-Type": "application/json",
        }

        # 1. Embed (one call)
        try:
            resp = await client.post(
                "https://api.cohere.com/v2/embed",
                headers=headers,
                json={
                    "texts": ["What is OLake?"],
                    "model": "embed-english-v3.0",
                    "input_type": "search_query",
                    "embedding_types": ["float"],
                },
            )
            remaining = resp.headers.get("x-trial-endpoint-call-remaining")
            limit = resp.headers.get("x-trial-endpoint-call-limit")
            if resp.status_code == 200:
                data = resp.json()
                embs = data.get("embeddings", {}).get("float", [])
                dim = len(embs[0]) if embs else 0
                model_used = data.get("meta", {}).get("api_version", {}).get("version", "?")
                print(f"  ✅ Embed OK — model=embed-english-v3.0, dim={dim}")
                if remaining:
                    print(f"     Trial quota: {remaining}/{limit} calls remaining")
                    if int(remaining) < 10:
                        print("  ⚠️  WARNING: trial embed quota nearly exhausted!")
            else:
                print(f"  ❌ Embed failed: HTTP {resp.status_code}: {resp.text[:200]}")
                all_ok = False
        except Exception as e:
            print(f"  ❌ Embed error: {e}")
            all_ok = False

        # 2. Rerank (one call)
        try:
            resp = await client.post(
                "https://api.cohere.com/v2/rerank",
                headers=headers,
                json={
                    "model": "rerank-v3.5",
                    "query": "How to configure Postgres CDC in OLake?",
                    "documents": [
                        "OLake supports CDC from Postgres using logical replication.",
                        "MySQL connector supports full refresh and CDC modes.",
                    ],
                    "top_n": 2,
                },
            )
            remaining = resp.headers.get("x-trial-endpoint-call-remaining")
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                print(f"  ✅ Rerank OK — model=rerank-v3.5, {len(results)} results")
                if remaining:
                    print(f"     Trial quota remaining: {remaining}")
            else:
                print(f"  ❌ Rerank failed: HTTP {resp.status_code}: {resp.text[:200]}")
                all_ok = False
        except Exception as e:
            print(f"  ❌ Rerank error: {e}")
            all_ok = False

    return all_ok


async def main() -> None:
    print("Ask OLake — Key Smoke Test")
    print("=" * 56)

    or_ok = await check_openrouter()
    co_ok = await check_cohere()

    print("\n══ Summary ═════════════════════════════════════════════")
    print(f"  OpenRouter: {'✅ PASS' if or_ok else '❌ FAIL'}")
    print(f"  Cohere:     {'✅ PASS' if co_ok else '❌ FAIL (or not configured)'}")

    if not or_ok:
        print("\n⛔ OpenRouter is required. Set OPENROUTER_API_KEY in .env")
        sys.exit(1)
    else:
        print("\n✅ Ready to ingest. Run: make ingest")


if __name__ == "__main__":
    asyncio.run(main())
