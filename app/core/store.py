"""Chunk data model and SQLite-based persistent store.

Schema:
  chunks(id TEXT PK, url TEXT, anchor TEXT, title TEXT, heading_path TEXT,
         source_type TEXT, repo TEXT, path TEXT, content_hash TEXT,
         updated_at TEXT, content TEXT, metadata_json TEXT)

FTS5 virtual table for BM25:
  chunks_fts(rowid→chunk rowid, content)

Vector index: numpy/FAISS flat file persisted to disk alongside the db.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

_db_lock = threading.Lock()


@dataclass
class Chunk:
    id: str
    url: str
    anchor: str
    title: str
    heading_path: str          # e.g. "Sources > Postgres > CDC"
    source_type: str           # docs | github | external
    repo: str                  # e.g. "datazip-inc/olake"
    path: str                  # file path within repo or URL path
    content_hash: str
    updated_at: str
    content: str
    # Filterable metadata
    connector: str = ""        # postgres|mysql|mongodb|...
    sync_mode: str = ""        # full_refresh|cdc|incremental
    catalog_type: str = ""     # glue|rest|hive|jdbc|nessie|polaris|unity
    product: str = ""          # ingestion|fusion|ui|helm
    # For text prepended before embedding (contextual chunk)
    embed_text: str = ""

    @property
    def metadata(self) -> dict:
        return {
            "connector": self.connector,
            "sync_mode": self.sync_mode,
            "catalog_type": self.catalog_type,
            "product": self.product,
        }


def compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def make_embed_text(chunk: Chunk) -> str:
    """Prepend title + heading path for contextual embedding."""
    parts = []
    if chunk.title:
        parts.append(chunk.title)
    if chunk.heading_path:
        parts.append(chunk.heading_path)
    parts.append(chunk.content)
    return "\n".join(parts)


class ChunkStore:
    """SQLite-backed chunk storage with FTS5 for BM25 retrieval."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with _db_lock, self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    anchor TEXT DEFAULT '',
                    title TEXT DEFAULT '',
                    heading_path TEXT DEFAULT '',
                    source_type TEXT DEFAULT 'docs',
                    repo TEXT DEFAULT '',
                    path TEXT DEFAULT '',
                    content_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    content TEXT NOT NULL,
                    connector TEXT DEFAULT '',
                    sync_mode TEXT DEFAULT '',
                    catalog_type TEXT DEFAULT '',
                    product TEXT DEFAULT '',
                    embed_text TEXT DEFAULT '',
                    row_num INTEGER
                );

                CREATE TABLE IF NOT EXISTS chunk_embeddings (
                    chunk_id TEXT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
                    embedding BLOB NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    content,
                    title,
                    heading_path,
                    content=chunks,
                    content_rowid=row_num
                );

                CREATE TABLE IF NOT EXISTS semantic_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_normalized TEXT NOT NULL,
                    question_hash TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    answer TEXT NOT NULL,
                    sources TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    ttl_seconds INTEGER NOT NULL DEFAULT 86400
                );
                CREATE INDEX IF NOT EXISTS idx_cache_hash ON semantic_cache(question_hash);

                CREATE TABLE IF NOT EXISTS ingestion_state (
                    url TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                );
            """)
            conn.commit()

    def upsert_chunk(self, chunk: Chunk) -> bool:
        """Insert or update a chunk. Returns True if changed."""
        with _db_lock, self._conn() as conn:
            existing = conn.execute(
                "SELECT content_hash FROM chunks WHERE id = ?", (chunk.id,)
            ).fetchone()

            if existing and existing["content_hash"] == chunk.content_hash:
                return False  # unchanged

            # Assign row_num for FTS5 content rowid
            row_num_row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
            row_num = row_num_row[0] + 1 if not existing else conn.execute(
                "SELECT row_num FROM chunks WHERE id = ?", (chunk.id,)
            ).fetchone()["row_num"]

            conn.execute("""
                INSERT INTO chunks
                    (id, url, anchor, title, heading_path, source_type, repo, path,
                     content_hash, updated_at, content, connector, sync_mode,
                     catalog_type, product, embed_text, row_num)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    url=excluded.url, anchor=excluded.anchor, title=excluded.title,
                    heading_path=excluded.heading_path, source_type=excluded.source_type,
                    repo=excluded.repo, path=excluded.path,
                    content_hash=excluded.content_hash, updated_at=excluded.updated_at,
                    content=excluded.content, connector=excluded.connector,
                    sync_mode=excluded.sync_mode, catalog_type=excluded.catalog_type,
                    product=excluded.product, embed_text=excluded.embed_text
            """, (
                chunk.id, chunk.url, chunk.anchor, chunk.title, chunk.heading_path,
                chunk.source_type, chunk.repo, chunk.path,
                chunk.content_hash, chunk.updated_at, chunk.content,
                chunk.connector, chunk.sync_mode, chunk.catalog_type, chunk.product,
                chunk.embed_text, row_num,
            ))

            # FTS update
            if existing:
                conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (row_num,))
            conn.execute(
                "INSERT INTO chunks_fts(rowid, content, title, heading_path) VALUES (?, ?, ?, ?)",
                (row_num, chunk.content, chunk.title, chunk.heading_path),
            )
            conn.commit()
            return True

    def upsert_embedding(self, chunk_id: str, embedding: list[float]) -> None:
        import struct
        blob = struct.pack(f"{len(embedding)}f", *embedding)
        with _db_lock, self._conn() as conn:
            conn.execute("""
                INSERT INTO chunk_embeddings (chunk_id, embedding)
                VALUES (?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET embedding=excluded.embedding
            """, (chunk_id, blob))
            conn.commit()

    def get_all_embeddings(self) -> tuple[list[str], list[list[float]]]:
        """Return (chunk_ids, embeddings) for all chunks."""
        import struct
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT chunk_id, embedding FROM chunk_embeddings"
            ).fetchall()
        ids, vecs = [], []
        for row in rows:
            blob = row["embedding"]
            n = len(blob) // 4
            vec = list(struct.unpack(f"{n}f", blob))
            ids.append(row["chunk_id"])
            vecs.append(vec)
        return ids, vecs

    def get_chunks_by_ids(self, ids: list[str]) -> list[Chunk]:
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM chunks WHERE id IN ({placeholders})", ids
            ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    def bm25_search(self, query: str, top_k: int = 30) -> list[tuple[str, float]]:
        """FTS5 BM25 search. Returns [(chunk_id, score)]."""
        with self._conn() as conn:
            # Get row_nums from FTS5, then map to chunk IDs
            rows = conn.execute("""
                SELECT c.id, bm25(chunks_fts) AS score
                FROM chunks_fts
                JOIN chunks c ON c.row_num = chunks_fts.rowid
                WHERE chunks_fts MATCH ?
                ORDER BY score
                LIMIT ?
            """, (self._fts_escape(query), top_k)).fetchall()
        # BM25 scores are negative in SQLite (more negative = better)
        return [(r["id"], -r["score"]) for r in rows]

    def _fts_escape(self, q: str) -> str:
        """Tokenize and escape query terms for FTS5."""
        clean = re.sub(r"[^\w\s]", " ", q)
        tokens = [t.strip() for t in clean.split() if len(t.strip()) > 1]
        if not tokens:
            return '""'
        stop_words = {
            "how", "do", "does", "what", "is", "are", "the", "in", "on", "to",
            "for", "with", "a", "an", "and", "or", "of", "can", "i", "we", "you",
        }
        meaningful = [t for t in tokens if t.lower() not in stop_words]
        query_tokens = meaningful if meaningful else tokens
        return " OR ".join(f'"{t.replace(chr(34), chr(34)+chr(34))}"' for t in query_tokens)

    def count_chunks(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def get_ingestion_state(self, url: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT content_hash FROM ingestion_state WHERE url = ?", (url,)
            ).fetchone()
        return row["content_hash"] if row else None

    def set_ingestion_state(self, url: str, content_hash: str) -> None:
        with _db_lock, self._conn() as conn:
            conn.execute("""
                INSERT INTO ingestion_state (url, content_hash, ingested_at)
                VALUES (?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    ingested_at=excluded.ingested_at
            """, (url, content_hash, time.strftime("%Y-%m-%dT%H:%M:%SZ")))
            conn.commit()

    def delete_chunks_for_url(self, url: str) -> None:
        with _db_lock, self._conn() as conn:
            conn.execute("DELETE FROM chunks WHERE url = ?", (url,))
            conn.commit()

    # ── Semantic cache ────────────────────────────────────────────────────────

    def cache_get(self, question_hash: str) -> dict | None:
        """Exact-hash cache lookup."""
        now = int(time.time())
        with self._conn() as conn:
            row = conn.execute("""
                SELECT answer, sources, created_at, ttl_seconds
                FROM semantic_cache
                WHERE question_hash = ?
                ORDER BY created_at DESC LIMIT 1
            """, (question_hash,)).fetchone()
        if row and (now - row["created_at"]) < row["ttl_seconds"]:
            return {"answer": row["answer"], "sources": json.loads(row["sources"])}
        return None

    def cache_get_semantic(
        self, query_embedding: list[float], threshold: float = 0.95, ttl: int = 86400
    ) -> dict | None:
        """Cosine similarity semantic cache lookup."""
        import struct
        import math
        now = int(time.time())
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, embedding, answer, sources, created_at, ttl_seconds FROM semantic_cache"
            ).fetchall()
        if not rows:
            return None

        def cosine(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(x * x for x in b))
            return dot / (na * nb + 1e-9)

        best_score = 0.0
        best_row = None
        for row in rows:
            if (now - row["created_at"]) >= row["ttl_seconds"]:
                continue
            blob = row["embedding"]
            n = len(blob) // 4
            vec = list(struct.unpack(f"{n}f", blob))
            score = cosine(query_embedding, vec)
            if score > best_score:
                best_score = score
                best_row = row

        if best_score >= threshold and best_row is not None:
            return {"answer": best_row["answer"], "sources": json.loads(best_row["sources"])}
        return None

    def cache_set(
        self,
        question_normalized: str,
        question_hash: str,
        embedding: list[float],
        answer: str,
        sources: list[str],
        ttl: int = 86400,
    ) -> None:
        import struct
        blob = struct.pack(f"{len(embedding)}f", *embedding)
        with _db_lock, self._conn() as conn:
            conn.execute("""
                INSERT INTO semantic_cache
                    (question_normalized, question_hash, embedding, answer, sources, created_at, ttl_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                question_normalized, question_hash, blob, answer,
                json.dumps(sources), int(time.time()), ttl,
            ))
            conn.commit()

    def cache_clear(self) -> int:
        """Clear all cache entries (called on reindex)."""
        with _db_lock, self._conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM semantic_cache").fetchone()[0]
            conn.execute("DELETE FROM semantic_cache")
            conn.commit()
        return n

    def _row_to_chunk(self, row: sqlite3.Row) -> Chunk:
        return Chunk(
            id=row["id"],
            url=row["url"],
            anchor=row["anchor"] or "",
            title=row["title"] or "",
            heading_path=row["heading_path"] or "",
            source_type=row["source_type"] or "docs",
            repo=row["repo"] or "",
            path=row["path"] or "",
            content_hash=row["content_hash"],
            updated_at=row["updated_at"],
            content=row["content"],
            connector=row["connector"] or "",
            sync_mode=row["sync_mode"] or "",
            catalog_type=row["catalog_type"] or "",
            product=row["product"] or "",
            embed_text=row["embed_text"] or "",
        )


_store: ChunkStore | None = None


def get_store() -> ChunkStore:
    global _store
    if _store is None:
        from app.config import get_settings
        cfg = get_settings()
        db_path = os.path.join(cfg.index_path, "chunks.db")
        _store = ChunkStore(db_path)
    return _store
