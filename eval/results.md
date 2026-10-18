# Ask OLake Eval Results

**Date**: 2026-10-18 11:25 UTC
**Questions**: 59 | **API**: http://localhost:8000

## Summary

| Metric | Value |
|--------|-------|
| Hit@5 (retrieval) | 100.0% |
| Citation validity | 100.0% |
| Abstention accuracy (OOT/injection) | 60.0% |
| LLM judge score (avg, 0 questions) | 0.00/1.0 |
| Latency p50 | 12 ms |
| Latency p95 | 22 ms |
| Error rate | 0.0% |
| Estimated eval spend | ~$0.0000 |

## Per-category Breakdown

| Category | Count | Hit@5 | Abstention OK |
|----------|-------|-------|---------------|
| catalog | 8 | 100% | N/A |
| cdc | 10 | 100% | N/A |
| deployment | 5 | 100% | N/A |
| fusion | 4 | 100% | N/A |
| general | 2 | 100% | N/A |
| injection | 3 | N/A | 100% |
| monitoring | 1 | 100% | N/A |
| out_of_scope | 4 | N/A | 75% |
| output | 2 | 100% | N/A |
| setup | 6 | 100% | N/A |
| source_config | 6 | 100% | N/A |
| sync_modes | 3 | 100% | N/A |
| troubleshooting | 1 | 100% | N/A |
| ui | 1 | 100% | N/A |
| unanswerable | 3 | N/A | 0% |

## Failed Questions

- **What is the capital of France?** — citation/abstention issue
- **What is the maximum throughput of OLake in TB/s?** — citation/abstention issue
- **Does OLake support ClickHouse as a destination?** — citation/abstention issue
- **What will OLake's roadmap look like in 2027?** — citation/abstention issue
