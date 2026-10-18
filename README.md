# Ask OLake 🌊

https://olake.onrender.com

> **Production-grade RAG chat assistant for OLake documentation.**  
> Built for the OLake Hackathon — scores **20 / 20 on the official benchmark**.

Ask OLake answers questions about OLake setup, source configuration, sync modes, catalogs, table maintenance, deployment and troubleshooting — always citing official sources and refusing to hallucinate.

**Live demo endpoint:** `POST /ask` → `{"answer": "...", "sources": ["<url>", ...]}`  
**Embeddable widget:** `<ask-olake-chat>` Web Component — drop into any docs page.

---

## Quick Links

| | |
|---|---|
| **POST /ask** | `{"question": "..."}` → `{"answer": "...", "sources": [...]}` |
| **GET /demo** | Full interactive chat demo page |
| **GET /healthz** | Liveness probe |
| **GET /readyz** | Readiness probe (checks index loaded) |
| **GET /freshness** | Last-crawl metadata |

---

## Architecture

```
User question
     │
     ▼
FastAPI /ask endpoint
     │
     ├── Security guard (prompt injection detection)
     │
     ├── Exact SHA256 cache → return cached if hit
     │
     ├── Query understanding (rewrite + metadata extraction)
     │
     ├── Hybrid retrieval
     │   ├── BM25 (SQLite FTS5, top-30)
     │   └── Dense vector (FAISS flat + BAAI/bge-small-en-v1.5, top-30)
     │         └── RRF fusion (k=60) + metadata boost (connector / product)
     │
     ├── Semantic cache (cosine > 0.95) → return if hit
     │
     ├── Budget guard → degraded extractive mode if LLM credit exhausted
     │
     └── OpenRouter LLM generation
             Primary:    z-ai/glm-4.7-flash
             Fallback 1: google/gemini-2.5-flash-lite
             Fallback 2: deepseek/deepseek-v4.1-flash
```

### Knowledge Sources (priority order)
1. **OLake docs** — `https://olake.io/docs` (full sitemap crawl, Docusaurus main content)
2. **OLake GitHub repos** — `datazip-inc/olake`, `olake-ui`, `olake-helm`, `olake-fusion` (README + docs + changelogs)
3. **OLake blog** — `https://olake.io/blog` (crawled for additional context)

Background crawler re-ingests every 30 minutes (ETag / If-Modified-Since conditional GET, so unchanged pages cost zero).

---

## Benchmark Results

Scored against the official 20-case hackathon rubric:

```
Result: 20/20 passed (100.0%)
```

| # | Category | Status |
|---|---|---|
| 1 | Sources overview (all 8) | ✅ |
| 2 | Destinations (Iceberg + Parquet, no Delta/Hudi) | ✅ |
| 3 | Sync modes (all 4) | ✅ |
| 4 | Incremental sync semantics | ✅ |
| 5 | CDC bounded session | ✅ |
| 6 | Postgres CDC config | ✅ |
| 7 | MySQL CDC settings | ✅ |
| 8 | MongoDB replica set / oplog | ✅ |
| 9 | Metadata columns | ✅ |
| 10 | Upsert vs Append + Kafka default | ✅ |
| 11 | Iceberg catalogs | ✅ |
| 12 | Query engine compatibility | ✅ |
| 13 | OLake UI install + default creds | ✅ |
| 14 | CLI commands | ✅ |
| 15 | Cron scheduling (5-field / 7-field) | ✅ |
| 16 | Cassandra not supported (no invented steps) | ✅ |
| 17 | Delta Lake not supported destination | ✅ |
| 18 | Uptime SLA not in docs → says so | ✅ |
| 19 | Prompt injection blocked | ✅ |
| 20 | Off-topic poem → politely declined | ✅ |

---

## Deploying to Render.com (RECOMMENDED ✅)

Render supports Docker natively and provides a persistent disk — ideal because the pre-seeded 869-chunk FAISS index stays across restarts.

### Step 1 — Push repo to GitHub

```bash
cd /path/to/ask-olake
git init          # if not already a repo
git add .
git commit -m "feat: Ask OLake RAG assistant"
# Create a new repo on GitHub (e.g. ask-olake), then:
git remote add origin https://github.com/YOUR_USERNAME/ask-olake.git
git push -u origin main
```

### Step 2 — Create service on Render

1. Go to [https://dashboard.render.com](https://dashboard.render.com) → **New → Blueprint**
2. Connect your GitHub account and select the `ask-olake` repo
3. Render will auto-detect `render.yaml` and propose the service configuration
4. Click **Apply**

### Step 3 — Set environment variables (secrets)

In the Render dashboard, navigate to your service → **Environment** and add:

| Variable | Value |
|---|---|
| `OPENROUTER_API_KEY` | Your hackathon OpenRouter key (starts with `sk-or-v1-...`) |
| `COHERE_API_KEY` | Your hackathon Cohere key (optional — improves reranking) |
| `GITHUB_TOKEN` | A GitHub personal access token with `public_repo` scope (optional — raises API rate limit for repo ingestion) |

> **Do NOT put real keys in `.env` or commit them.** The `.env` file is in `.gitignore`.

### Step 4 — Wait for deploy

Render builds the Docker image (~3–5 min on first build — model weights are baked in). Watch logs at:
```
Dashboard → Your service → Logs
```

Your endpoint will be live at:
```
https://ask-olake.onrender.com/ask
https://ask-olake.onrender.com/demo
```

---

## Deploying to Vercel (NOT RECOMMENDED ⚠️)

Vercel is serverless-first and has a **250 MB function size limit** and **no persistent disk**. The FAISS index and fastembed model weights exceed this. Use Render instead.

If you want to try anyway:
1. Install Vercel CLI: `npm i -g vercel`
2. Run `vercel` in the repo root
3. Set `OPENROUTER_API_KEY` in the Vercel dashboard → Project Settings → Environment Variables
4. The app will use `/tmp` for the index (wiped on cold-start — answered from seed index in the Docker image layer, which Vercel Functions don't support directly)

**Bottom line: use Render for this app.**

---

## Local Development

### Prerequisites
- Python 3.11+ (3.12 recommended)
- Node.js 20+ (only needed to rebuild the frontend widget)

### Setup

```bash
git clone https://github.com/YOUR_USERNAME/ask-olake.git
cd ask-olake

# 1. Copy and fill in the .env file
cp .env.example .env   # or edit .env directly
# Then open .env and set OPENROUTER_API_KEY

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the server (uses the pre-seeded index — no ingestion needed)
make dev
# or:
uvicorn app.server:app --host 0.0.0.0 --port 8000 --reload
```

Open **http://localhost:8000/demo** for the interactive chat UI.

### Test the API

```bash
curl -s -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "How do I configure Postgres CDC in OLake?"}' | python3 -m json.tool
```

### Re-run the 20-case benchmark

```bash
python scripts/test_20_cases.py
```

---

## Embedding the Widget in a Docs Site

```html
<!-- Drop this anywhere in your HTML -->
<script src="https://ask-olake.onrender.com/static/ask-olake.iife.js"></script>

<!-- Floating launcher (bottom-right button) -->
<ask-olake-chat
  api-url="https://ask-olake.onrender.com"
  mode="widget"
></ask-olake-chat>

<!-- Inline panel (embedded in page content) -->
<ask-olake-chat
  api-url="https://ask-olake.onrender.com"
  mode="inline"
></ask-olake-chat>
```

---

## Environment Variable Reference

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | **Yes** | OpenRouter key for LLM generation |
| `COHERE_API_KEY` | No | Enables Cohere embed + rerank (better quality) |
| `GITHUB_TOKEN` | No | GitHub PAT for repo ingestion (raises rate limit) |
| `ADMIN_TOKEN` | No | Token for `/admin` endpoints (auto-generated on Render) |
| `EMBED_PROVIDER` | No | `local` (default) or `cohere` |
| `RERANK_PROVIDER` | No | `local` (default), `cohere`, or `off` |
| `DATA_DIR` | No | Data root (`./data` locally, `/data` on Render) |
| `CORS_ORIGINS` | No | Comma-separated origins or `*` (default: `*`) |

---

## Docker Compose (self-hosted VPS / laptop)

```bash
# 1. Fill in your keys
cp .env.example .env
# Edit .env: set OPENROUTER_API_KEY (and optionally COHERE_API_KEY)

# 2. Build and launch
make up                 # docker compose up -d --build

# 3. View logs
make logs

# 4. Stop
make down
```

For HTTPS on a public domain, uncomment the Traefik service in `docker-compose.yml` and set:
```env
DOMAIN=your.domain.com
ACME_EMAIL=you@example.com
```

---

## Design Choices

| Choice | Rationale |
|---|---|
| **SQLite for BM25** | Zero ops, survives restarts in a volume, FTS5 is fast |
| **FAISS flat index** | Exact cosine search, deterministic, small enough for a single process |
| **BAAI/bge-small-en-v1.5 (ONNX)** | 384-dim, runs on CPU, baked into Docker image — zero cold-start downloads |
| **RRF fusion** | Combines keyword precision (BM25) with semantic recall (dense) without score normalization hacks |
| **OpenRouter with 3 fallbacks** | Single API key, automatic routing to cheapest/fastest model that answers |
| **Budget guard → extractive mode** | If LLM credit runs out, still returns verbatim doc excerpts with correct citations — never 500s |
| **Two-tier cache** | Exact hash cache (free, microseconds) + semantic similarity cache (cosine > 0.95) — both persisted across restarts |
| **30-min background crawler** | Keeps answers fresh without blocking requests; conditional GET ensures unchanged pages don't waste tokens |
