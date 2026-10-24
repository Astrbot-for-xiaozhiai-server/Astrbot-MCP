"""Self-learning knowledge base.

Storage: SQLite with an FTS5 index (trigram when available, which behaves well
for Chinese text). Optional embeddings can be supplied by any OpenAI-compatible
``/embeddings`` endpoint; keyword and vector results are fused with reciprocal
rank fusion (RRF).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from ..config import KnowledgeConfig
from ..context import AppContext
from ..net import safe_request
from ..utils import (
    decode_bytes,
    json_dumps,
    sha256_text,
    strip_html,
    truncate_text,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".log",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".htm",
    ".xml",
    ".sql",
}


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """Split text into overlapping chunks, preferring paragraph boundaries."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    paragraphs = [
        part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()
    ]
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for paragraph in paragraphs:
        if len(paragraph) > size:
            flush()
            start = 0
            while start < len(paragraph):
                end = min(start + size, len(paragraph))
                chunks.append(paragraph[start:end].strip())
                if end >= len(paragraph):
                    break
                start = max(end - overlap, start + 1)
            continue

        if not current:
            current = paragraph
        elif len(current) + 2 + len(paragraph) <= size:
            current += "\n\n" + paragraph
        else:
            tail = current[-overlap:] if overlap else ""
            flush()
            current = (tail + "\n\n" + paragraph).strip() if tail else paragraph

    flush()
    return [chunk for chunk in chunks if chunk]


def _normalize_tags(tags: str) -> str:
    values = {part.strip() for part in re.split(r"[,;]", tags or "") if part.strip()}
    return ",".join(sorted(values))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class KnowledgeBase:
    def __init__(self, config: KnowledgeConfig) -> None:
        self.config = config
        self.path = Path(config.db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self.fts_enabled = False
        self.fts_tokenizer = "none"
        self._init_db()

    # ------------------------------------------------------------------ setup
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _init_db(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS kb_docs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kb_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id INTEGER NOT NULL REFERENCES kb_docs(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    embedding TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks(doc_id);
                """
            )
            connection.commit()

            for tokenizer in ("trigram", "unicode61"):
                try:
                    connection.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS kb_chunks_fts "
                        f"USING fts5(content, tokenize='{tokenizer}')"
                    )
                    connection.commit()
                    row = connection.execute(
                        "SELECT sql FROM sqlite_master "
                        "WHERE type='table' AND name='kb_chunks_fts'"
                    ).fetchone()
                    definition = str(row["sql"] or "") if row is not None else ""
                    self.fts_enabled = True
                    self.fts_tokenizer = (
                        "trigram" if "trigram" in definition else "unicode61"
                    )
                    break
                except sqlite3.OperationalError:
                    continue
            if not self.fts_enabled:
                logger.warning(
                    "SQLite FTS5 is unavailable; knowledge search will use LIKE"
                )
        finally:
            connection.close()

    # ------------------------------------------------------------------ public
    async def add(
        self,
        source: str,
        content: str,
        *,
        title: str = "",
        tags: str = "",
        replace: bool = True,
    ) -> dict[str, Any]:
        content = (content or "").strip()
        if not content:
            raise ValueError("content must not be empty")
        encoded_size = len(content.encode("utf-8"))
        if encoded_size > self.config.max_content_bytes:
            raise ValueError(
                f"content is too large ({encoded_size} bytes); limit is "
                f"{self.config.max_content_bytes}"
            )

        normalized_source = (
            source or ""
        ).strip() or f"text:{sha256_text(content)[:16]}"
        chunks = chunk_text(content, self.config.chunk_size, self.config.chunk_overlap)
        if not chunks:
            raise ValueError("content produced no chunks")
        if len(chunks) > self.config.max_chunks_per_doc:
            raise ValueError(
                f"content produced {len(chunks)} chunks; limit is "
                f"{self.config.max_chunks_per_doc}. Split the document first."
            )

        embeddings = await self._embed_texts(chunks)
        return await asyncio.to_thread(
            self._write_document,
            normalized_source,
            content,
            title,
            tags,
            replace,
            chunks,
            embeddings,
        )

    async def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        tags: str = "",
    ) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = max(1, min(int(top_k or self.config.max_results), 50))
        tag_values = [
            part.strip() for part in re.split(r"[,;]", tags or "") if part.strip()
        ]

        keyword_items = await asyncio.to_thread(
            self._keyword_search, query, limit * 4, tag_values
        )

        vector_items: list[dict[str, Any]] = []
        if self.config.embedding.enabled:
            vector = await self._embed_one(query)
            if vector:
                vector_items = await asyncio.to_thread(
                    self._vector_search, vector, limit * 4, tag_values
                )

        fused = self._fuse(
            [items for items in (keyword_items, vector_items) if items], limit
        )
        for item in fused:
            item["content"] = truncate_text(item["content"], 1200)

        return {
            "query": query,
            "keyword_hits": len(keyword_items),
            "vector_hits": len(vector_items),
            "count": len(fused),
            "results": fused,
        }

    async def get(self, key: str, *, max_chars: int = 20_000) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_document, key, max_chars)

    async def list_documents(
        self, *, limit: int = 50, offset: int = 0, tags: str = ""
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._list_documents, limit, offset, tags)

    async def delete(self, key: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._delete_document, key)

    async def stats(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._stats)

    async def reindex(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._reindex)

    async def export(
        self, *, path: str = "", include_embeddings: bool = False
    ) -> dict[str, Any]:
        data = await asyncio.to_thread(self._export_data, include_embeddings)
        if not path:
            return data
        target = self._resolve_export_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json_dumps(data), encoding="utf-8")
        return {
            "exported": True,
            "path": str(target),
            "documents": len(data["documents"]),
        }

    # --------------------------------------------------------------- internals
    def _write_document(
        self,
        source: str,
        content: str,
        title: str,
        tags: str,
        replace: bool,
        chunks: list[str],
        embeddings: list[list[float]] | None,
    ) -> dict[str, Any]:
        content_hash = sha256_text(content)
        normalized_tags = _normalize_tags(tags)
        now = utc_now_iso()

        with self._write_lock:
            connection = self._connect()
            try:
                existing = connection.execute(
                    "SELECT id, content_hash, title, tags FROM kb_docs WHERE source=?",
                    (source,),
                ).fetchone()

                existing_tags = (
                    str(existing["tags"] or "") if existing is not None else ""
                )
                if (
                    existing is not None
                    and existing["content_hash"] == content_hash
                    and (existing["title"] or "") == (title or "")
                    and existing_tags == normalized_tags
                ):
                    return {
                        "source": source,
                        "doc_id": existing["id"],
                        "title": existing["title"],
                        "chunks": 0,
                        "tags": normalized_tags,
                        "created": False,
                        "unchanged": True,
                    }

                if existing is not None and not replace:
                    raise ValueError(
                        f"Document {source!r} already exists (replace=false)"
                    )

                if existing is not None:
                    doc_id = int(existing["id"])
                    if self.fts_enabled:
                        connection.execute(
                            "DELETE FROM kb_chunks_fts WHERE rowid IN "
                            "(SELECT id FROM kb_chunks WHERE doc_id=?)",
                            (doc_id,),
                        )
                    connection.execute(
                        "DELETE FROM kb_chunks WHERE doc_id=?", (doc_id,)
                    )
                    connection.execute(
                        "UPDATE kb_docs SET title=?, content=?, tags=?, "
                        "content_hash=?, updated_at=? WHERE id=?",
                        (
                            title or "",
                            content,
                            normalized_tags,
                            content_hash,
                            now,
                            doc_id,
                        ),
                    )
                    created = False
                else:
                    cursor = connection.execute(
                        "INSERT INTO kb_docs(source, title, content, tags, "
                        "content_hash, created_at, updated_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            source,
                            title or "",
                            content,
                            normalized_tags,
                            content_hash,
                            now,
                            now,
                        ),
                    )
                    doc_id = int(cursor.lastrowid)
                    created = True

                for ordinal, chunk in enumerate(chunks):
                    embedding = (
                        json.dumps(embeddings[ordinal], ensure_ascii=False)
                        if embeddings is not None and ordinal < len(embeddings)
                        else None
                    )
                    cursor = connection.execute(
                        "INSERT INTO kb_chunks(doc_id, ordinal, content, embedding) "
                        "VALUES(?,?,?,?)",
                        (doc_id, ordinal, chunk, embedding),
                    )
                    if self.fts_enabled:
                        connection.execute(
                            "INSERT INTO kb_chunks_fts(rowid, content) VALUES(?,?)",
                            (cursor.lastrowid, chunk),
                        )

                connection.commit()
                return {
                    "source": source,
                    "doc_id": doc_id,
                    "title": title,
                    "chunks": len(chunks),
                    "tags": normalized_tags,
                    "created": created,
                    "unchanged": False,
                }
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _keyword_search(
        self, query: str, limit: int, tags: list[str]
    ) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows: list[sqlite3.Row] = []
            if self.fts_enabled and len(query) >= 3:
                fts_query = '"' + query.replace('"', '""') + '"'
                try:
                    rows = connection.execute(
                        """
                        SELECT c.id AS chunk_id, c.ordinal, c.content,
                               d.id AS doc_id, d.title, d.source, d.tags,
                               bm25(kb_chunks_fts) AS rank
                        FROM kb_chunks_fts
                        JOIN kb_chunks c ON c.id = kb_chunks_fts.rowid
                        JOIN kb_docs d ON d.id = c.doc_id
                        WHERE kb_chunks_fts MATCH ?
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (fts_query, limit * 3),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []

            if not rows:
                like = f"%{query}%"
                rows = connection.execute(
                    """
                    SELECT c.id AS chunk_id, c.ordinal, c.content,
                           d.id AS doc_id, d.title, d.source, d.tags,
                           0.0 AS rank
                    FROM kb_chunks c
                    JOIN kb_docs d ON d.id = c.doc_id
                    WHERE c.content LIKE ? OR d.title LIKE ? OR d.tags LIKE ?
                    ORDER BY c.id DESC
                    LIMIT ?
                    """,
                    (like, like, like, limit * 3),
                ).fetchall()

            wanted = set(tags)
            results: list[dict[str, Any]] = []
            for row in rows:
                if wanted:
                    doc_tags = {
                        part.strip()
                        for part in str(row["tags"] or "").split(",")
                        if part.strip()
                    }
                    if not wanted.issubset(doc_tags):
                        continue
                results.append(
                    {
                        "chunk_id": int(row["chunk_id"]),
                        "doc_id": int(row["doc_id"]),
                        "ordinal": int(row["ordinal"]),
                        "content": str(row["content"]),
                        "title": str(row["title"] or ""),
                        "source": str(row["source"]),
                        "tags": str(row["tags"] or ""),
                        "rank": float(row["rank"] or 0.0),
                    }
                )
                if len(results) >= limit:
                    break
            return results
        finally:
            connection.close()

    def _vector_search(
        self, query_vector: list[float], limit: int, tags: list[str]
    ) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT c.id AS chunk_id, c.ordinal, c.content, c.embedding,
                       d.id AS doc_id, d.title, d.source, d.tags
                FROM kb_chunks c
                JOIN kb_docs d ON d.id = c.doc_id
                WHERE c.embedding IS NOT NULL AND c.embedding != ''
                LIMIT 5000
                """
            ).fetchall()

            wanted = set(tags)
            scored: list[dict[str, Any]] = []
            for row in rows:
                if wanted:
                    doc_tags = {
                        part.strip()
                        for part in str(row["tags"] or "").split(",")
                        if part.strip()
                    }
                    if not wanted.issubset(doc_tags):
                        continue
                try:
                    vector = json.loads(row["embedding"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(vector, list):
                    continue
                score = _cosine(query_vector, vector)
                scored.append(
                    {
                        "chunk_id": int(row["chunk_id"]),
                        "doc_id": int(row["doc_id"]),
                        "ordinal": int(row["ordinal"]),
                        "content": str(row["content"]),
                        "title": str(row["title"] or ""),
                        "source": str(row["source"]),
                        "tags": str(row["tags"] or ""),
                        "rank": -score,
                    }
                )

            scored.sort(key=lambda item: item["rank"])
            return scored[:limit]
        finally:
            connection.close()

    @staticmethod
    def _fuse(
        result_lists: list[list[dict[str, Any]]], limit: int
    ) -> list[dict[str, Any]]:
        scores: dict[int, float] = {}
        index: dict[int, dict[str, Any]] = {}
        for items in result_lists:
            for rank, item in enumerate(items):
                chunk_id = item["chunk_id"]
                index.setdefault(chunk_id, item)
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (60 + rank + 1)

        ordered = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)[:limit]
        fused: list[dict[str, Any]] = []
        for chunk_id, score in ordered:
            item = dict(index[chunk_id])
            item.pop("rank", None)
            item["score"] = round(score, 6)
            fused.append(item)
        return fused

    def _get_document(self, key: str, max_chars: int) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = self._find_document(connection, key)
            if row is None:
                raise ValueError(f"Document not found: {key!r}")
            chunk_count = connection.execute(
                "SELECT COUNT(*) AS count FROM kb_chunks WHERE doc_id=?",
                (row["id"],),
            ).fetchone()["count"]
            content = str(row["content"])
            return {
                "id": row["id"],
                "source": row["source"],
                "title": row["title"],
                "tags": row["tags"],
                "chars": len(content),
                "chunks": int(chunk_count),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "truncated": len(content) > max_chars,
                "content": truncate_text(content, max_chars),
            }
        finally:
            connection.close()

    def _list_documents(self, limit: int, offset: int, tags: str) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        wanted = {
            part.strip() for part in re.split(r"[,;]", tags or "") if part.strip()
        }

        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT d.id, d.source, d.title, d.tags,
                       LENGTH(d.content) AS chars,
                       d.created_at, d.updated_at,
                       COUNT(c.id) AS chunks
                FROM kb_docs d
                LEFT JOIN kb_chunks c ON c.doc_id = d.id
                GROUP BY d.id
                ORDER BY d.updated_at DESC, d.id DESC
                """
            ).fetchall()

            documents: list[dict[str, Any]] = []
            for row in rows:
                doc_tags = {
                    part.strip()
                    for part in str(row["tags"] or "").split(",")
                    if part.strip()
                }
                if wanted and not wanted.issubset(doc_tags):
                    continue
                documents.append(
                    {
                        "id": row["id"],
                        "source": row["source"],
                        "title": row["title"],
                        "tags": row["tags"],
                        "chars": row["chars"],
                        "chunks": int(row["chunks"]),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                )

            total = len(documents)
            return {
                "total": total,
                "limit": limit,
                "offset": offset,
                "documents": documents[offset : offset + limit],
            }
        finally:
            connection.close()

    def _delete_document(self, key: str) -> dict[str, Any]:
        with self._write_lock:
            connection = self._connect()
            try:
                row = self._find_document(connection, key)
                if row is None:
                    raise ValueError(f"Document not found: {key!r}")
                doc_id = int(row["id"])
                if self.fts_enabled:
                    connection.execute(
                        "DELETE FROM kb_chunks_fts WHERE rowid IN "
                        "(SELECT id FROM kb_chunks WHERE doc_id=?)",
                        (doc_id,),
                    )
                connection.execute("DELETE FROM kb_docs WHERE id=?", (doc_id,))
                connection.commit()
                return {
                    "deleted": True,
                    "id": doc_id,
                    "source": row["source"],
                }
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _stats(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            docs = connection.execute("SELECT COUNT(*) AS c FROM kb_docs").fetchone()[
                "c"
            ]
            chunks = connection.execute(
                "SELECT COUNT(*) AS c FROM kb_chunks"
            ).fetchone()["c"]
            embedded = connection.execute(
                "SELECT COUNT(*) AS c FROM kb_chunks "
                "WHERE embedding IS NOT NULL AND embedding != ''"
            ).fetchone()["c"]
            latest = connection.execute(
                "SELECT MAX(updated_at) AS t FROM kb_docs"
            ).fetchone()["t"]
            return {
                "documents": int(docs),
                "chunks": int(chunks),
                "embedded_chunks": int(embedded),
                "fts_enabled": self.fts_enabled,
                "fts_tokenizer": self.fts_tokenizer,
                "embedding_enabled": self.config.embedding.enabled,
                "db_path": str(self.path),
                "db_size": self.path.stat().st_size if self.path.exists() else 0,
                "latest_updated_at": latest,
            }
        finally:
            connection.close()

    def _reindex(self) -> dict[str, Any]:
        if not self.fts_enabled:
            raise ValueError("FTS5 is unavailable on this SQLite build")
        with self._write_lock:
            connection = self._connect()
            try:
                connection.execute("DELETE FROM kb_chunks_fts")
                connection.execute(
                    "INSERT INTO kb_chunks_fts(rowid, content) "
                    "SELECT id, content FROM kb_chunks"
                )
                connection.commit()
                count = connection.execute(
                    "SELECT COUNT(*) AS c FROM kb_chunks"
                ).fetchone()["c"]
                return {"reindexed": True, "chunks": int(count)}
            finally:
                connection.close()

    def _export_data(self, include_embeddings: bool) -> dict[str, Any]:
        connection = self._connect()
        try:
            docs = [
                dict(row)
                for row in connection.execute(
                    "SELECT id, source, title, content, tags, content_hash, "
                    "created_at, updated_at FROM kb_docs ORDER BY id"
                ).fetchall()
            ]
            columns = "id, doc_id, ordinal, content"
            if include_embeddings:
                columns += ", embedding"
            chunks = [
                dict(row)
                for row in connection.execute(
                    f"SELECT {columns} FROM kb_chunks ORDER BY doc_id, ordinal"
                ).fetchall()
            ]
            return {
                "version": 1,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "documents": docs,
                "chunks": chunks,
            }
        finally:
            connection.close()

    def _resolve_export_path(self, path: str) -> Path:
        base = self.path.parent.resolve()
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = base / target
        target = target.resolve()
        if target != base and base not in target.parents:
            raise ValueError("Export path must stay inside the knowledge DB directory")
        return target

    @staticmethod
    def _find_document(connection: sqlite3.Connection, key: str) -> sqlite3.Row | None:
        normalized = str(key).strip()
        if not normalized:
            return None
        if normalized.isdigit():
            row = connection.execute(
                "SELECT * FROM kb_docs WHERE id=?", (int(normalized),)
            ).fetchone()
            if row is not None:
                return row
        return connection.execute(
            "SELECT * FROM kb_docs WHERE source=?", (normalized,)
        ).fetchone()

    # -------------------------------------------------------------- embeddings
    async def _embed_texts(self, texts: list[str]) -> list[list[float]] | None:
        config = self.config.embedding
        if not config.enabled:
            return None
        if not config.api_key:
            logger.warning("Embeddings are enabled but embedding.api_key is empty")
            return None

        vectors: list[list[float]] = []
        try:
            async with httpx.AsyncClient(timeout=config.timeout) as client:
                for start in range(0, len(texts), 64):
                    batch = texts[start : start + 64]
                    payload: dict[str, Any] = {
                        "model": config.model,
                        "input": batch,
                    }
                    if config.dimensions:
                        payload["dimensions"] = config.dimensions
                    response = await client.post(
                        config.base_url.rstrip("/") + "/embeddings",
                        headers={"Authorization": f"Bearer {config.api_key}"},
                        json=payload,
                    )
                    response.raise_for_status()
                    data = response.json().get("data", [])
                    data.sort(key=lambda item: int(item.get("index", 0)))
                    vectors.extend(item["embedding"] for item in data)
        except Exception as exc:  # noqa: BLE001 - embeddings are best-effort
            logger.warning(
                "Embedding request failed, storing chunks without vectors: %s", exc
            )
            return None

        if len(vectors) != len(texts):
            logger.warning(
                "Embedding count mismatch (expected %s, got %s)",
                len(texts),
                len(vectors),
            )
            return None
        return vectors

    async def _embed_one(self, text: str) -> list[float] | None:
        vectors = await self._embed_texts([text])
        return vectors[0] if vectors else None


async def _learn_url(ctx: AppContext, url: str) -> dict[str, str]:
    response = await safe_request(
        ctx,
        "GET",
        url,
        timeout=ctx.config.security.request_timeout,
        follow_redirects=True,
    )
    if not response.ok:
        raise ValueError(f"Failed to fetch {url}: HTTP {response.status}")
    content_type = response.content_type
    raw_text = response.text()

    if "html" in content_type or not content_type:
        extracted = strip_html(raw_text, base_url=response.url)
        return {
            "title": extracted["title"],
            "text": extracted["text"],
            "source": response.url,
        }
    return {"title": response.url, "text": raw_text, "source": response.url}


def register(mcp: FastMCP, ctx: AppContext) -> None:
    config = ctx.config.services.knowledge
    kb = KnowledgeBase(config)

    @mcp.tool()
    async def kb_add(
        source: str,
        content: str,
        title: str = "",
        tags: str = "",
        replace: bool = True,
    ) -> str:
        """向知识库添加或更新一篇文档。

        Args:
            source: 文档唯一标识，例如 URL、文件路径或自定义 ID。
            content: 文档正文。
            title: 标题。
            tags: 逗号分隔的标签，便于后续过滤。
            replace: source 已存在时是否覆盖。
        """
        return json_dumps(
            await kb.add(source, content, title=title, tags=tags, replace=replace)
        )

    @mcp.tool()
    async def kb_remember(
        content: str,
        title: str = "",
        tags: str = "",
        source: str = "",
    ) -> str:
        """记住一条事实或笔记（供 Agent 自学习时调用；自动去重）。

        Args:
            content: 要长期记住的内容。
            title: 可选标题。
            tags: 逗号分隔的标签。
            source: 可选唯一标识；默认按内容哈希生成。
        """
        result = await kb.add(source, content, title=title, tags=tags, replace=True)
        result["remembered"] = True
        return json_dumps(result)

    @mcp.tool()
    async def kb_search(query: str, top_k: int = 0, tags: str = "") -> str:
        """检索知识库，返回最相关的片段。

        Args:
            query: 查询文本。
            top_k: 返回片段数，1-50；0 表示使用配置值。
            tags: 逗号分隔的标签过滤（需全部匹配）。
        """
        return json_dumps(await kb.search(query, top_k=top_k, tags=tags))

    @mcp.tool()
    async def kb_learn_url(url: str, tags: str = "", title: str = "") -> str:
        """抓取网页、提取正文并写入知识库（自学习入口）。

        Args:
            url: 要学习的 http/https 地址。
            tags: 逗号分隔的标签。
            title: 可选标题；留空使用网页标题。
        """
        learned = await _learn_url(ctx, url)
        text = learned["text"].strip()
        if not text:
            raise ValueError(f"No readable text extracted from {url}")
        result = await kb.add(
            learned["source"],
            text,
            title=title or learned["title"],
            tags=tags,
            replace=True,
        )
        result["learned_chars"] = len(text)
        return json_dumps(result)

    @mcp.tool()
    async def kb_learn_file(path: str, tags: str = "") -> str:
        """从本地文本文件学习内容并写入知识库。

        Args:
            path: 相对于 files.root 沙箱的文件路径。
            tags: 逗号分隔的标签。
        """
        files_config = ctx.config.services.files
        base = Path(files_config.root).resolve()
        target = (
            (base / Path(path)).resolve()
            if not Path(path).is_absolute()
            else Path(path).resolve()
        )
        if target != base and base not in target.parents:
            raise ValueError(f"Path {path!r} escapes the files sandbox {str(base)!r}")
        if not target.is_file():
            raise ValueError(f"File not found: {path}")
        if target.suffix.lower() not in _TEXT_EXTENSIONS:
            raise ValueError(
                f"Unsupported file type {target.suffix!r}; supported: "
                + ", ".join(sorted(_TEXT_EXTENSIONS))
            )

        data = target.read_bytes()
        if len(data) > config.max_content_bytes:
            raise ValueError(
                f"File is too large ({len(data)} bytes); limit is "
                f"{config.max_content_bytes}"
            )
        if b"\x00" in data[:4096]:
            raise ValueError("File appears to be binary")

        text = decode_bytes(data).strip()
        if not text:
            raise ValueError("File is empty")
        relative = str(target.relative_to(base))
        result = await kb.add(
            f"file:{relative}",
            text,
            title=target.name,
            tags=tags,
            replace=True,
        )
        result["learned_chars"] = len(text)
        return json_dumps(result)

    @mcp.tool()
    async def kb_list(limit: int = 50, offset: int = 0, tags: str = "") -> str:
        """列出知识库中的文档。

        Args:
            limit: 返回数量，1-500。
            offset: 分页偏移。
            tags: 逗号分隔的标签过滤。
        """
        return json_dumps(
            await kb.list_documents(limit=limit, offset=offset, tags=tags)
        )

    @mcp.tool()
    async def kb_get(key: str, max_chars: int = 20000) -> str:
        """按 ID 或 source 读取一篇知识库文档。

        Args:
            key: 文档 ID、source 或 URL。
            max_chars: 正文最大字符数，100-200000。
        """
        return json_dumps(
            await kb.get(key, max_chars=max(100, min(int(max_chars), 200_000)))
        )

    @mcp.tool()
    async def kb_delete(key: str) -> str:
        """按 ID 或 source 删除知识库文档。

        Args:
            key: 文档 ID 或 source。
        """
        return json_dumps(await kb.delete(key))

    @mcp.tool()
    async def kb_stats() -> str:
        """查看知识库统计信息（文档数、片段数、FTS 与向量状态）。"""
        return json_dumps(await kb.stats())

    @mcp.tool()
    async def kb_reindex() -> str:
        """重建全文索引（在数据库被外部修改后使用）。"""
        return json_dumps(await kb.reindex())

    @mcp.tool()
    async def kb_export(path: str = "", include_embeddings: bool = False) -> str:
        """导出知识库为 JSON。

        Args:
            path: 导出文件路径（必须在知识库目录内）；留空则直接返回 JSON。
            include_embeddings: 是否包含向量数据。
        """
        return json_dumps(
            await kb.export(path=path, include_embeddings=include_embeddings)
        )
