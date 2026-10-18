"""Ingestion pipeline.

Sources:
1. OLake docs site (https://olake.io/docs - Docusaurus)
2. GitHub repos: datazip-inc/olake, olake-ui, olake-helm, olake-fusion
3. External reference docs (crawled at ingest, tagged source_type=external)

Run as CLI: python -m app.ingest --all
Or per source: python -m app.ingest --docs / --github / --external
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as md2

from app.config import get_settings
from app.core.logging import get_logger, setup_logging
from app.core.store import Chunk, ChunkStore, compute_hash, get_store
from app.core.embeddings import embed_documents, preload_models
from app.rag.chunker import chunk_markdown, detect_metadata

log = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

DOCS_SITEMAP = "https://olake.io/sitemap.xml"
DOCS_BASE = "https://olake.io/docs"

GITHUB_REPOS = [
    ("datazip-inc/olake", "ingestion"),
    ("datazip-inc/olake-ui", "ui"),
    ("datazip-inc/olake-helm", "helm"),
    ("datazip-inc/olake-fusion", "fusion"),
]

GITHUB_INCLUDE_PATTERNS = [
    r"README\.md$",
    r"docs/.*\.md$",
    r"\.md$",
    r"values\.ya?ml$",
    r"CHANGELOG.*",
    r".*config.*\.(json|ya?ml)$",
]

GITHUB_EXCLUDE_PATTERNS = [
    r"vendor/",
    r"node_modules/",
    r"\.lock$",
    r"package-lock\.json",
    r"yarn\.lock",
    r"go\.sum",
    r"dist/",
    r"build/",
]

EXTERNAL_SOURCES = [
    {
        "url": "https://iceberg.apache.org/docs/latest/",
        "label": "Apache Iceberg Docs",
        "max_pages": 20,
    },
    {
        "url": "https://debezium.io/documentation/reference/stable/connectors/",
        "label": "Debezium Connectors",
        "max_pages": 10,
    },
    {
        "url": "https://www.postgresql.org/docs/current/logical-replication.html",
        "label": "PostgreSQL Logical Replication",
        "max_pages": 3,
    },
    {
        "url": "https://dev.mysql.com/doc/refman/8.0/en/binary-log.html",
        "label": "MySQL Binary Log",
        "max_pages": 3,
    },
]

HTTP_TIMEOUT = 20.0
EMBED_BATCH_SIZE = 32
RATE_LIMIT_DELAY = 0.1  # seconds between requests


# ── HTML → Markdown ───────────────────────────────────────────────────────────

def html_to_markdown(html: str, base_url: str = "") -> tuple[str, str]:
    """Extract main content from Docusaurus/docs page, return (title, markdown)."""
    soup = BeautifulSoup(html, "lxml")

    # Extract title
    title = ""
    if soup.title:
        title = soup.title.get_text(strip=True)
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)

    # Remove nav, footer, sidebar, TOC
    for tag in soup.find_all(["nav", "footer", "aside", "script", "style"]):
        tag.decompose()
    for cls in ["navbar", "sidebar", "footer", "toc", "pagination", "breadcrumbs",
                "table-of-contents", "DocSearch", "announcement"]:
        for el in soup.find_all(class_=re.compile(cls, re.I)):
            el.decompose()
    for el in soup.find_all(attrs={"role": "navigation"}):
        el.decompose()

    # Find main content
    main = (
        soup.find("article")
        or soup.find("main")
        or soup.find(class_=re.compile(r"markdown|doc|content", re.I))
        or soup.find("body")
    )

    if main is None:
        return title, ""

    # Convert to markdown
    markdown = md2(str(main), heading_style="ATX", bullets="-", code_language="")
    # Clean up excessive blank lines
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    return title, markdown


# ── Sitemap crawl ─────────────────────────────────────────────────────────────

async def get_doc_urls(client: httpx.AsyncClient) -> list[str]:
    """Fetch sitemap and return all /docs URLs."""
    try:
        resp = await client.get(DOCS_SITEMAP, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = [
            loc.text.strip()
            for loc in root.findall(".//sm:loc", ns)
            if loc.text and "/docs" in loc.text
        ]
        log.info(f"Found {len(urls)} doc URLs in sitemap")
        return urls
    except Exception as e:
        log.error(f"Sitemap fetch failed: {e}")
        return []


async def crawl_docs(store: ChunkStore, client: httpx.AsyncClient) -> int:
    """Crawl OLake docs site. Returns number of new/updated chunks."""
    urls = await get_doc_urls(client)
    if not urls:
        log.warning("No doc URLs found — check sitemap")
        return 0

    total_chunks = 0
    for url in urls:
        try:
            existing_hash = store.get_ingestion_state(url)
            resp = await client.get(url, timeout=HTTP_TIMEOUT)
            if resp.status_code != 200:
                log.warning(f"Skip {url}: HTTP {resp.status_code}")
                continue

            page_hash = compute_hash(resp.text)
            if existing_hash == page_hash:
                log.debug(f"Unchanged: {url}")
                await asyncio.sleep(RATE_LIMIT_DELAY)
                continue

            title, markdown = html_to_markdown(resp.text, base_url=url)
            if not markdown.strip():
                log.warning(f"Empty content: {url}")
                continue

            # Delete old chunks for this URL
            store.delete_chunks_for_url(url)

            chunks = chunk_markdown(
                markdown, url=url, title=title, source_type="docs",
                updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )

            for chunk in chunks:
                store.upsert_chunk(chunk)

            store.set_ingestion_state(url, page_hash)
            total_chunks += len(chunks)
            log.info(f"Ingested {len(chunks)} chunks: {url}")
            await asyncio.sleep(RATE_LIMIT_DELAY)

        except Exception as e:
            log.error(f"Error crawling {url}: {e}")

    return total_chunks


# ── GitHub ingestion ──────────────────────────────────────────────────────────

GITHUB_API = "https://api.github.com"


def _should_include_file(path: str) -> bool:
    for pat in GITHUB_EXCLUDE_PATTERNS:
        if re.search(pat, path):
            return False
    for pat in GITHUB_INCLUDE_PATTERNS:
        if re.search(pat, path, re.IGNORECASE):
            return True
    return False


async def github_get_tree(
    client: httpx.AsyncClient, repo: str, token: str = ""
) -> list[dict]:
    """Get full file tree via GitHub API."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # Get default branch
    repo_resp = await client.get(f"{GITHUB_API}/repos/{repo}", headers=headers, timeout=HTTP_TIMEOUT)
    if repo_resp.status_code == 404:
        log.warning(f"GitHub repo not found: {repo}")
        return []
    repo_data = repo_resp.json()
    branch = repo_data.get("default_branch", "main")
    sha = repo_data.get("default_branch", "main")

    # Get tree (recursive)
    tree_resp = await client.get(
        f"{GITHUB_API}/repos/{repo}/git/trees/{branch}?recursive=1",
        headers=headers,
        timeout=HTTP_TIMEOUT,
    )
    if tree_resp.status_code != 200:
        log.warning(f"Tree fetch failed for {repo}: {tree_resp.status_code}")
        return []

    tree = tree_resp.json().get("tree", [])
    return [
        item for item in tree
        if item.get("type") == "blob" and _should_include_file(item.get("path", ""))
    ]


async def crawl_github(store: ChunkStore, client: httpx.AsyncClient) -> int:
    """Ingest relevant files from GitHub repos."""
    cfg = get_settings()
    token = cfg.github_token
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    total_chunks = 0
    for repo_name, product in GITHUB_REPOS:
        log.info(f"Ingesting GitHub repo: {repo_name}")
        try:
            files = await github_get_tree(client, repo_name, token)
            log.info(f"  {len(files)} relevant files in {repo_name}")

            for file_info in files:
                file_path = file_info["path"]
                file_url = f"https://github.com/{repo_name}/blob/main/{file_path}"
                raw_url = f"https://raw.githubusercontent.com/{repo_name}/main/{file_path}"

                existing_hash = store.get_ingestion_state(file_url)

                try:
                    resp = await client.get(raw_url, headers=headers, timeout=HTTP_TIMEOUT)
                    if resp.status_code != 200:
                        continue
                    content = resp.text
                    page_hash = compute_hash(content)

                    if existing_hash == page_hash:
                        await asyncio.sleep(RATE_LIMIT_DELAY)
                        continue

                    store.delete_chunks_for_url(file_url)

                    # Convert YAML/JSON to pseudo-markdown for chunking
                    if file_path.endswith((".yaml", ".yml", ".json")):
                        markdown = f"# {file_path}\n\n```yaml\n{content[:8000]}\n```"
                        title = file_path
                    else:
                        title = file_path.split("/")[-1].replace(".md", "").replace("-", " ").title()
                        markdown = content

                    chunks = chunk_markdown(
                        markdown,
                        url=file_url,
                        title=f"{repo_name}: {title}",
                        source_type="github",
                        repo=repo_name,
                        path=file_path,
                        updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    )
                    # Tag product from repo
                    for chunk in chunks:
                        if not chunk.product:
                            chunk.product = product

                    for chunk in chunks:
                        store.upsert_chunk(chunk)

                    store.set_ingestion_state(file_url, page_hash)
                    total_chunks += len(chunks)
                    await asyncio.sleep(RATE_LIMIT_DELAY)

                except Exception as e:
                    log.error(f"Error fetching {raw_url}: {e}")

        except Exception as e:
            log.error(f"Error ingesting repo {repo_name}: {e}")

    log.info(f"GitHub ingestion: {total_chunks} chunks")
    return total_chunks


# ── External docs crawl ───────────────────────────────────────────────────────

ALLOWED_EXTERNAL_DOMAINS = {
    "iceberg.apache.org",
    "debezium.io",
    "www.postgresql.org",
    "dev.mysql.com",
    "www.mongodb.com",
    "docs.mongodb.com",
}


def _is_allowed_external(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc in ALLOWED_EXTERNAL_DOMAINS


async def crawl_external(store: ChunkStore, client: httpx.AsyncClient) -> int:
    """Crawl allowlisted external docs at ingest time."""
    total_chunks = 0

    for source in EXTERNAL_SOURCES:
        url = source["url"]
        label = source["label"]
        max_pages = source.get("max_pages", 5)

        if not _is_allowed_external(url):
            log.warning(f"External domain not allowed: {url}")
            continue

        try:
            resp = await client.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True)
            if resp.status_code != 200:
                continue

            page_hash = compute_hash(resp.text)
            existing = store.get_ingestion_state(url)
            if existing == page_hash:
                continue

            title, markdown = html_to_markdown(resp.text, base_url=url)
            if not markdown.strip():
                continue

            store.delete_chunks_for_url(url)
            chunks = chunk_markdown(
                markdown,
                url=url,
                title=f"[External] {label}: {title}",
                source_type="external",
                updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            for chunk in chunks:
                store.upsert_chunk(chunk)
            store.set_ingestion_state(url, page_hash)
            total_chunks += len(chunks)
            log.info(f"External: {len(chunks)} chunks from {url}")

        except Exception as e:
            log.error(f"External crawl error {url}: {e}")

    return total_chunks


# ── Embedding pass ────────────────────────────────────────────────────────────

async def embed_all_chunks(store: ChunkStore) -> int:
    """Embed all chunks that don't have embeddings yet."""
    with store._conn() as conn:
        rows = conn.execute("""
            SELECT c.id, c.embed_text FROM chunks c
            LEFT JOIN chunk_embeddings e ON e.chunk_id = c.id
            WHERE e.chunk_id IS NULL
        """).fetchall()

    if not rows:
        log.info("All chunks already embedded")
        return 0

    log.info(f"Embedding {len(rows)} chunks...")
    ids = [r["id"] for r in rows]
    texts = [r["embed_text"] or "" for r in rows]

    # Batch with retry
    total = 0
    for i in range(0, len(ids), EMBED_BATCH_SIZE):
        batch_ids = ids[i:i + EMBED_BATCH_SIZE]
        batch_texts = texts[i:i + EMBED_BATCH_SIZE]
        try:
            embeddings, _ = await embed_documents(batch_texts)
            for cid, emb in zip(batch_ids, embeddings):
                store.upsert_embedding(cid, emb)
            total += len(batch_ids)
            log.info(f"  Embedded {total}/{len(ids)} chunks")
        except Exception as e:
            log.error(f"Embedding batch failed: {e}")

    # Rebuild FAISS index
    try:
        from app.rag.retrieval import load_vector_index
        load_vector_index(store, force=True)
    except Exception as e:
        log.warning(f"FAISS index build skipped or failed: {e}")

    return total


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_ingest(docs: bool = True, github: bool = True, external: bool = True) -> dict:
    setup_logging()
    cfg = get_settings()
    store = get_store()

    # Ensure embedding model is loaded
    log.info("Preloading embedding model...")
    preload_models()

    total = {"docs": 0, "github": 0, "external": 0, "embeddings": 0}

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "AskOLake-Ingestion/1.0"},
        timeout=HTTP_TIMEOUT,
    ) as client:
        if docs:
            total["docs"] = await crawl_docs(store, client)
        if github:
            total["github"] = await crawl_github(store, client)
        if external:
            total["external"] = await crawl_external(store, client)

    # Embed new chunks
    total["embeddings"] = await embed_all_chunks(store)

    log.info(f"Ingestion complete: {total}")
    return total


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="OLake ingestion pipeline")
    parser.add_argument("--all", action="store_true", help="Ingest all sources")
    parser.add_argument("--docs", action="store_true", help="Ingest OLake docs")
    parser.add_argument("--github", action="store_true", help="Ingest GitHub repos")
    parser.add_argument("--external", action="store_true", help="Ingest external docs")
    args = parser.parse_args()

    do_all = args.all or (not args.docs and not args.github and not args.external)
    result = asyncio.run(run_ingest(
        docs=do_all or args.docs,
        github=do_all or args.github,
        external=do_all or args.external,
    ))
    print(f"\nIngestion summary: {result}")
    print(f"Total chunks in store: {get_store().count_chunks()}")
