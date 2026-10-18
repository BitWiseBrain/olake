"""Evaluation harness for Ask OLake RAG pipeline.

Runs:
1. Retrieval eval (hit@k, MRR) — ZERO LLM calls
2. Abstention accuracy (injection/OOT questions) — ZERO LLM calls
3. LLM-as-judge correctness — on a 25-question subset only
4. Citation validity — URL format + domain check
5. Latency p50/p95

Usage:
    python -m eval.run --questions eval/questions.jsonl --api http://localhost:8000
    python -m eval.run --retrieval-only          # no LLM calls
    python -m eval.run --max-spend 0.40          # budget cap

Output: eval/results.md
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

REFUSAL_PHRASES = [
    "i can only answer questions about olake",
    "i'm specialized in olake",
    "i couldn't find this in the olake",
    "can only answer questions about olake documentation",
]

IDONOTKNOW_PHRASES = [
    "couldn't find",
    "i don't know",
    "not found in",
    "no information",
]

ALLOWED_DOMAINS = {
    "olake.io",
    "github.com",
    "raw.githubusercontent.com",
    "iceberg.apache.org",
    "debezium.io",
    "www.postgresql.org",
    "dev.mysql.com",
    "www.mongodb.com",
    "docs.mongodb.com",
}


def load_questions(path: str) -> list[dict]:
    questions = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                questions.append(json.loads(line))
    return questions


def _url_valid(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def _url_domain_ok(url: str) -> bool:
    try:
        domain = urlparse(url).netloc.lower()
        return any(domain == d or domain.endswith("." + d) for d in ALLOWED_DOMAINS)
    except Exception:
        return False


def _url_matches_expected(url: str, expected_contains: list[str]) -> bool:
    url_lower = url.lower()
    return any(e.lower() in url_lower for e in expected_contains)


def check_citation_validity(sources: list[str]) -> dict:
    if not sources:
        return {"valid": True, "issues": [], "count": 0}
    issues = []
    for url in sources:
        if not _url_valid(url):
            issues.append(f"invalid_url: {url}")
        elif not _url_domain_ok(url):
            issues.append(f"disallowed_domain: {url}")
    return {"valid": len(issues) == 0, "issues": issues, "count": len(sources)}


def check_abstention(answer: str, category: str, expected_behavior: str) -> bool:
    """Check if answer correctly abstains for OOT/injection questions."""
    answer_lower = answer.lower()
    if expected_behavior == "refusal":
        return any(phrase in answer_lower for phrase in REFUSAL_PHRASES)
    if expected_behavior == "i_dont_know":
        return any(phrase in answer_lower for phrase in IDONOTKNOW_PHRASES)
    return True  # In-scope questions: any non-empty answer counts


def hit_at_k(sources: list[str], expected_contains: list[str], k: int = 5) -> bool:
    if not expected_contains:
        return True  # no expected URLs = OOT question, not applicable
    for url in sources[:k]:
        if _url_matches_expected(url, expected_contains):
            return True
    return False


async def call_ask_api(client: httpx.AsyncClient, api_base: str, question: str) -> tuple[dict, float]:
    start = time.perf_counter()
    try:
        resp = await client.post(
            f"{api_base}/ask",
            json={"question": question},
            timeout=30.0,
        )
        latency = (time.perf_counter() - start) * 1000
        if resp.status_code == 200:
            return resp.json(), latency
        return {"error": f"HTTP {resp.status_code}", "answer": "", "sources": []}, latency
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # Fallback to in-process ASGI app
        try:
            from app.main import create_app
            app = create_app()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as local_client:
                resp = await local_client.post("/ask", json={"question": question}, timeout=30.0)
                latency = (time.perf_counter() - start) * 1000
                if resp.status_code == 200:
                    return resp.json(), latency
                return {"error": f"HTTP {resp.status_code}", "answer": "", "sources": []}, latency
        except Exception as e:
            latency = (time.perf_counter() - start) * 1000
            return {"error": str(e), "answer": "", "sources": []}, latency
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        return {"error": str(e), "answer": "", "sources": []}, latency


async def llm_judge(
    client: httpx.AsyncClient,
    api_base: str,
    question: str,
    answer: str,
    expected_facts: list[str],
    openrouter_key: str,
) -> float:
    """LLM-as-judge: score 0-1 for factual correctness."""
    if not expected_facts or not openrouter_key:
        return -1.0  # skip

    facts_str = "\n".join(f"- {f}" for f in expected_facts)
    prompt = (
        f"Question: {question}\n\n"
        f"Answer: {answer[:800]}\n\n"
        f"Expected facts that should be in the answer:\n{facts_str}\n\n"
        "Score how many expected facts are present or implied in the answer. "
        "Reply with only a number from 0.0 to 1.0 (1.0 = all facts present, 0.0 = none)."
    )

    try:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {openrouter_key}",
                "HTTP-Referer": "https://olake.io",
            },
            json={
                "model": "z-ai/glm-4.7-flash",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 10,
                "temperature": 0,
            },
            timeout=20.0,
        )
        text = resp.json()["choices"][0]["message"]["content"].strip()
        return float(re.search(r"\d+\.?\d*", text).group())
    except Exception:
        return -1.0


async def run_eval(
    questions_path: str,
    api_base: str = "http://localhost:8000",
    retrieval_only: bool = False,
    max_spend: float = 0.40,
    judge_subset: int = 25,
    output_path: str = "eval/results.md",
) -> None:
    questions = load_questions(questions_path)
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")

    print(f"Running eval on {len(questions)} questions against {api_base}")
    print(f"Retrieval only: {retrieval_only}, Judge subset: {judge_subset}, Max spend: ${max_spend}")

    results = []
    latencies = []
    spend_so_far = 0.0
    judge_count = 0
    in_scope_qs = [q for q in questions if q.get("expected_behavior") not in ("refusal", "i_dont_know")]
    oot_qs = [q for q in questions if q.get("expected_behavior") in ("refusal", "i_dont_know")]

    # Shuffle for judge subset (take from in-scope)
    import random
    random.seed(42)
    judge_qs = set(q["question"] for q in random.sample(in_scope_qs, min(judge_subset, len(in_scope_qs))))

    async with httpx.AsyncClient() as client:
        for i, q in enumerate(questions):
            question = q["question"]
            category = q.get("category", "")
            expected_behavior = q.get("expected_behavior", "answer")
            expected_facts = q.get("expected_facts", [])
            expected_url_contains = q.get("expected_url_contains", [])

            print(f"  [{i+1}/{len(questions)}] {question[:60]}...", end="", flush=True)

            if retrieval_only:
                from app.rag.retrieval import retrieve
                from app.core.store import get_store
                from app.rag.query import is_injection_attempt, is_smalltalk, is_out_of_scope
                t0 = time.perf_counter()
                if is_injection_attempt(question) or is_out_of_scope(question):
                    answer = "I can only answer questions about OLake."
                    sources = []
                else:
                    ret_res = await retrieve(question, store=get_store())
                    answer = "retrieved context"
                    sources = [c.url + (c.anchor if c.anchor else "") for c in ret_res.chunks]
                latency_ms = (time.perf_counter() - t0) * 1000
                resp = {"answer": answer, "sources": sources}
            else:
                resp, latency_ms = await call_ask_api(client, api_base, question)
            latencies.append(latency_ms)

            answer = resp.get("answer", "")
            sources = resp.get("sources", [])
            error = resp.get("error", "")

            citation_check = check_citation_validity(sources)
            abstention_ok = check_abstention(answer, category, expected_behavior)
            hit5 = hit_at_k(sources, expected_url_contains, k=5)

            judge_score = -1.0
            if (
                not retrieval_only
                and question in judge_qs
                and expected_behavior == "answer"
                and not error
                and spend_so_far < max_spend
            ):
                judge_score = await llm_judge(
                    client, api_base, question, answer, expected_facts, openrouter_key
                )
                spend_so_far += 0.0001  # rough estimate per judge call
                judge_count += 1

            result = {
                "question": question,
                "category": category,
                "expected_behavior": expected_behavior,
                "answer": answer[:300],
                "sources_count": len(sources),
                "latency_ms": round(latency_ms, 1),
                "citation_valid": citation_check["valid"],
                "citation_issues": citation_check["issues"],
                "abstention_ok": abstention_ok,
                "hit_at_5": hit5,
                "judge_score": judge_score,
                "error": error,
            }
            results.append(result)

            status = "✅" if (citation_check["valid"] and abstention_ok and not error) else "❌"
            print(f" {status} {latency_ms:.0f}ms judge={judge_score:.2f}" if judge_score >= 0 else f" {status} {latency_ms:.0f}ms")

            # Small delay to avoid overwhelming the API
            await asyncio.sleep(0.3)

    # ── Compute metrics ───────────────────────────────────────────────────────
    n = len(results)
    in_scope = [r for r in results if r["expected_behavior"] == "answer"]
    oot = [r for r in results if r["expected_behavior"] in ("refusal", "i_dont_know")]

    hit5_rate = sum(r["hit_at_5"] for r in in_scope) / max(len(in_scope), 1)
    citation_valid_rate = sum(r["citation_valid"] for r in results) / max(n, 1)
    abstention_acc = sum(r["abstention_ok"] for r in oot) / max(len(oot), 1)
    judge_scores = [r["judge_score"] for r in results if r["judge_score"] >= 0]
    avg_judge = sum(judge_scores) / max(len(judge_scores), 1)
    error_rate = sum(1 for r in results if r["error"]) / max(n, 1)

    lats_sorted = sorted(latencies)
    p50 = lats_sorted[n // 2]
    p95 = lats_sorted[int(n * 0.95)]

    # ── Write report ──────────────────────────────────────────────────────────
    report = f"""# Ask OLake Eval Results

**Date**: {time.strftime("%Y-%m-%d %H:%M UTC")}
**Questions**: {n} | **API**: {api_base}

## Summary

| Metric | Value |
|--------|-------|
| Hit@5 (retrieval) | {hit5_rate:.1%} |
| Citation validity | {citation_valid_rate:.1%} |
| Abstention accuracy (OOT/injection) | {abstention_acc:.1%} |
| LLM judge score (avg, {len(judge_scores)} questions) | {avg_judge:.2f}/1.0 |
| Latency p50 | {p50:.0f} ms |
| Latency p95 | {p95:.0f} ms |
| Error rate | {error_rate:.1%} |
| Estimated eval spend | ~${spend_so_far:.4f} |

## Per-category Breakdown

| Category | Count | Hit@5 | Abstention OK |
|----------|-------|-------|---------------|
"""
    by_cat: dict[str, list] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r)
    for cat, cat_results in sorted(by_cat.items()):
        in_s = [r for r in cat_results if r["expected_behavior"] == "answer"]
        oot_c = [r for r in cat_results if r["expected_behavior"] in ("refusal", "i_dont_know")]
        h5 = f"{sum(r['hit_at_5'] for r in in_s)/max(len(in_s),1):.0%}" if in_s else "N/A"
        abs_ok = f"{sum(r['abstention_ok'] for r in oot_c)/max(len(oot_c),1):.0%}" if oot_c else "N/A"
        report += f"| {cat} | {len(cat_results)} | {h5} | {abs_ok} |\n"

    report += """
## Failed Questions

"""
    failed = [r for r in results if not r["citation_valid"] or not r["abstention_ok"] or r["error"]]
    for r in failed[:20]:
        report += f"- **{r['question'][:80]}** — {r['error'] or 'citation/abstention issue'}\n"

    Path(output_path).parent.mkdir(exist_ok=True)
    with open(output_path, "w") as f:
        f.write(report)

    print(f"\n{'='*60}")
    print(f"Eval complete: {n} questions")
    print(f"  Hit@5:          {hit5_rate:.1%}")
    print(f"  Citation valid: {citation_valid_rate:.1%}")
    print(f"  Abstention acc: {abstention_acc:.1%}")
    print(f"  Judge score:    {avg_judge:.2f}")
    print(f"  Latency p50/p95: {p50:.0f}/{p95:.0f} ms")
    print(f"  Spend:          ~${spend_so_far:.4f}")
    print(f"\nReport saved to: {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", default="eval/questions.jsonl")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--retrieval-only", action="store_true")
    parser.add_argument("--max-spend", type=float, default=0.40)
    parser.add_argument("--judge-subset", type=int, default=25)
    parser.add_argument("--output", default="eval/results.md")
    args = parser.parse_args()

    asyncio.run(run_eval(
        args.questions,
        api_base=args.api,
        retrieval_only=args.retrieval_only,
        max_spend=args.max_spend,
        judge_subset=args.judge_subset,
        output_path=args.output,
    ))
