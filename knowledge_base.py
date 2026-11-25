"""Extract, embed, and retrieve passages from locally indexed documents."""

import asyncio
import heapq
import math
import os
import re
import sqlite3
import struct
from bisect import bisect_left
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from diagnosis import redact


EMBEDDING_MODEL = "BAAI/bge-m3"
OLLAMA_EMBEDDING_MODEL = "bge-m3"
EMBEDDING_DIMENSIONS = 1024
CHUNK_LENGTH = 800
CHUNK_OVERLAP = 120
MAX_DOCUMENT_CHUNKS = 4096
EMBEDDING_BATCH = 16
SEARCH_LIMIT = 5
MIN_SIMILARITY = 0.45

_VECTOR = struct.Struct(f"<{EMBEDDING_DIMENSIONS}f")
_PASSAGE_BREAK = re.compile(r"[\n。！？!?]+|(?<=\.)[ \t]+")
_BINARY_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")


def official_source(value):
    """Keep provenance links on the public documentation hosts used by the project."""
    try:
        url = urlsplit(value)
        hostname = url.hostname or ""
        if (url.scheme == "https" and url.netloc == hostname
                and (hostname.endswith(".sjtu.edu.cn") or hostname in {
                    "support.huaweicloud.com", "docs.docker.com", "pip.pypa.io",
                }) and not any(char.isspace() or ord(char) < 32 for char in value)):
            return value
    except ValueError:
        pass
    return None


class KnowledgeBaseError(Exception):
    """An error that can be shown to a knowledge-base user."""


class BGEM3Embeddings:
    def __init__(self, client, base_url, api_key="", *, model=None):
        try:
            address = urlsplit(base_url)
            host, port = address.hostname, address.port
        except ValueError:
            address, host, port = None, None, None
        if (not host or address.scheme not in {"http", "https"} or address.query
                or address.fragment or address.username is not None
                or any(char.isspace() for char in base_url)):
            raise ValueError("ZHIYUAN_EMBEDDING_BASE_URL 必须是纯 HTTP(S) 地址，例如 http://127.0.0.1:11434/v1。")
        self.client = client
        self.url = f"{base_url.rstrip('/')}/embeddings"
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        local_ollama = host in {"localhost", "127.0.0.1", "::1"} and port == 11434
        self.model = model or (OLLAMA_EMBEDDING_MODEL if local_ollama else EMBEDDING_MODEL)

    @classmethod
    def from_env(cls, client):
        base_url = os.environ.get("ZHIYUAN_EMBEDDING_BASE_URL", "").strip()
        if not base_url:
            return None
        return cls(client, base_url, os.environ.get("ZHIYUAN_EMBEDDING_API_KEY", "").strip(),
                   model=os.environ.get("ZHIYUAN_EMBEDDING_MODEL", "").strip())

    async def embed(self, texts):
        try:
            response = await self.client.post(
                self.url, headers=self.headers,
                json={"model": self.model, "input": texts, "encoding_format": "float"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise KnowledgeBaseError(f"知识库向量服务返回 HTTP {exc.response.status_code}。") from exc
        except httpx.TimeoutException as exc:
            raise KnowledgeBaseError("知识库向量服务请求超时。") from exc
        except (httpx.RequestError, httpx.InvalidURL) as exc:
            raise KnowledgeBaseError("无法连接知识库向量服务。") from exc

        try:
            data = response.json()["data"]
            if not isinstance(data, list) or len(data) != len(texts):
                raise ValueError("Incomplete embeddings")
            vectors = [None] * len(texts)
            for item in data:
                index = item["index"]
                if type(index) is not int or not 0 <= index < len(vectors) or vectors[index] is not None:
                    raise ValueError("Invalid embedding index")
                vector = item["embedding"]
                if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSIONS:
                    raise ValueError("Invalid embedding length")
                if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
                    raise ValueError("Invalid embedding value")
                norm = math.hypot(*vector)
                if not norm or not math.isfinite(norm):
                    raise ValueError("Invalid embedding norm")
                vectors[index] = [value / norm for value in vector]
            return vectors
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise KnowledgeBaseError("知识库向量服务未返回有效的 1024 维向量。") from exc


def _pdf_pages(contents):
    if not contents.startswith(b"%PDF-"):
        raise KnowledgeBaseError("无法读取 PDF，请检查文件格式。")
    try:
        from pypdfium2 import PdfDocument
        from pypdfium2.raw import FPDF_ERR_PASSWORD
    except ImportError as exc:
        raise KnowledgeBaseError("服务缺少 PDF 文本解析依赖。") from exc

    try:
        with PdfDocument(contents) as reader:
            for page_number, page in enumerate(reader, start=1):
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_range()
                    finally:
                        textpage.close()
                finally:
                    page.close()
                yield page_number, text
    except KnowledgeBaseError:
        raise
    except Exception as exc:
        if getattr(exc, "err_code", None) == FPDF_ERR_PASSWORD:
            raise KnowledgeBaseError("请先解除 PDF 的密码保护再上传。") from exc
        raise KnowledgeBaseError("无法读取 PDF，请检查文件是否损坏。") from exc


def _passages(text):
    start = 0
    while start < len(text):
        end = min(start + CHUNK_LENGTH, len(text))
        if end < len(text):
            for match in _PASSAGE_BREAK.finditer(text, start + CHUNK_LENGTH // 2, end):
                end = match.end()
        part = text[start:end]
        passage = part.strip()
        if passage:
            first_character = end - len(part.lstrip())
            yield first_character, first_character + len(passage), passage
        if end == len(text):
            break
        start = end - CHUNK_OVERLAP


def _document_chunks(file_type, contents, sanitize):
    if file_type == "pdf":
        pages = _pdf_pages(contents)
    else:
        try:
            text = contents.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise KnowledgeBaseError("请上传 UTF-8 编码的 TXT 或 Markdown 文件。") from exc
        if text.startswith("%PDF-") or _BINARY_CONTROL.search(text):
            raise KnowledgeBaseError("文件包含非文本内容，请检查文件格式。")
        pages = [(None, text)]

    chunks = []
    for page, text in pages:
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        text = "\n".join(sanitize(line) for line in lines)
        if page is None:
            line_breaks = [match.start() for match in re.finditer("\n", text)]
        for start, end, passage in _passages(text):
            if page is None:
                first_line = bisect_left(line_breaks, start) + 1
                last_line = bisect_left(line_breaks, end - 1) + 1
                location = f"第 {first_line}–{last_line} 行" if first_line != last_line else f"第 {first_line} 行"
            else:
                location = f"第 {page} 页"
            chunks.append((location, passage))
            if len(chunks) > MAX_DOCUMENT_CHUNKS:
                raise KnowledgeBaseError(f"文件正文超过 {MAX_DOCUMENT_CHUNKS} 个片段，请拆分后重新上传。")

    if not chunks:
        if file_type == "pdf":
            raise KnowledgeBaseError("PDF 中没有可提取的文本，请上传含文本的 PDF 或先完成文字识别。")
        raise KnowledgeBaseError("文件中没有可索引的正文。")
    return chunks


def _canonical_type(file_type):
    file_type = file_type.lower().lstrip(".")
    if file_type not in {"txt", "md", "pdf"}:
        raise KnowledgeBaseError("仅支持 TXT、Markdown 和 PDF 文件。")
    return file_type


class KnowledgeBase:
    def __init__(self, db_path, embeddings, *, sanitize=redact):
        self.path = Path(db_path)
        self.embeddings = embeddings
        self.sanitize = sanitize

    def _connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    source_url TEXT,
                    sha256 TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    supersedes TEXT,
                    superseded_by TEXT
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    position INTEGER NOT NULL,
                    location TEXT NOT NULL,
                    text TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    PRIMARY KEY (document_id, position)
                );
                CREATE TABLE IF NOT EXISTS document_files (
                    document_id TEXT PRIMARY KEY REFERENCES documents(id),
                    contents BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(documents)")}
            for name, declaration in {
                "source_url": "TEXT",
                "sha256": "TEXT",
                "version": "INTEGER NOT NULL DEFAULT 1",
                "supersedes": "TEXT",
                "superseded_by": "TEXT",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE documents ADD COLUMN {name} {declaration}")
            if self.embeddings is not None:
                configured_model = self.embeddings.model
                saved = db.execute("SELECT value FROM knowledge_settings WHERE key = 'embedding_model'").fetchone()
                indexed = db.execute("SELECT 1 FROM chunks LIMIT 1").fetchone()
                stored_model = saved["value"] if saved else EMBEDDING_MODEL
                if indexed and stored_model != configured_model:
                    raise ValueError("知识库向量模型与已有索引不一致。请使用原模型，或配置新的 ZHIYUAN_KNOWLEDGE_DB 并重新导入。")
                db.execute("INSERT OR REPLACE INTO knowledge_settings VALUES ('embedding_model', ?)", (configured_model,))
            db.execute(
                "UPDATE documents SET status = 'failed', error = ? WHERE status = 'processing'",
                ("上次索引因服务重启而中断，请重新索引或重新上传。",),
            )

    def create_document(self, filename, file_type, *, source_url=None, sha256=None,
                        contents=None, supersedes=None):
        file_type = _canonical_type(file_type)
        filename = self.sanitize(filename.replace("\\", "/").rsplit("/", 1)[-1])
        document_id = uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as db, db:
            version = 1
            previous_version = None
            if supersedes is not None:
                target = db.execute("SELECT version FROM documents WHERE id = ?", (supersedes,)).fetchone()
                if target is None:
                    raise KnowledgeBaseError("要替换的文档不存在。")
                previous_version = target["version"]
                version = db.execute("""
                    WITH RECURSIVE
                    ancestors(id) AS (
                        SELECT ? AS id
                        UNION ALL
                        SELECT documents.supersedes FROM documents JOIN ancestors ON documents.id = ancestors.id
                        WHERE documents.supersedes IS NOT NULL
                    ),
                    family(id) AS (
                        SELECT id FROM ancestors
                        UNION ALL
                        SELECT documents.id FROM documents JOIN family ON documents.supersedes = family.id
                    )
                    SELECT COALESCE(MAX(version), 0) + 1 FROM documents WHERE id IN (SELECT id FROM family)
                """, (supersedes,)).fetchone()[0]
            db.execute(
                """INSERT INTO documents
                       (id, filename, file_type, status, created_at, source_url, sha256, version, supersedes)
                   VALUES (?, ?, ?, 'processing', ?, ?, ?, ?, ?)""",
                (document_id, filename, file_type, created_at,
                 official_source(source_url) if source_url else None, sha256, version, supersedes),
            )
            if contents is not None:
                db.execute("INSERT INTO document_files VALUES (?, ?)",
                           (document_id, sqlite3.Binary(contents)))
        return {"id": document_id, "filename": filename, "file_type": file_type,
                "status": "processing", "error": None, "chunk_count": 0, "created_at": created_at,
                "source_url": official_source(source_url) if source_url else None, "sha256": sha256,
                "version": version, "supersedes": supersedes, "supersedes_version": previous_version,
                "superseded_by": None, "superseded_by_version": None}

    def list_documents(self):
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute("""
                SELECT documents.id, documents.filename, documents.status, documents.error,
                       documents.chunk_count, documents.created_at, documents.source_url,
                       documents.sha256, documents.version, documents.supersedes,
                       documents.superseded_by,
                       previous.version AS supersedes_version,
                       successor.version AS superseded_by_version
                FROM documents
                LEFT JOIN documents AS previous ON previous.id = documents.supersedes
                LEFT JOIN documents AS successor ON successor.id = documents.superseded_by
                ORDER BY documents.created_at DESC, documents.id DESC
            """)]

    def get_document(self, document_id):
        with closing(self._connect()) as db:
            row = db.execute("""
                SELECT documents.id, documents.filename, documents.file_type, documents.status,
                       documents.error, documents.chunk_count, documents.created_at,
                       documents.source_url, documents.sha256, documents.version,
                       documents.supersedes, documents.superseded_by,
                       previous.version AS supersedes_version,
                       successor.version AS superseded_by_version
                FROM documents
                LEFT JOIN documents AS previous ON previous.id = documents.supersedes
                LEFT JOIN documents AS successor ON successor.id = documents.superseded_by
                WHERE documents.id = ?
            """, (document_id,)).fetchone()
            return dict(row) if row else None

    def prepare_retry(self, document_id):
        """Reset a failed document and return the upload kept for re-indexing."""
        with closing(self._connect()) as db, db:
            row = db.execute(
                "SELECT file_type, status, superseded_by FROM documents WHERE id = ?",
                (document_id,),
            ).fetchone()
            if row is None:
                raise KnowledgeBaseError("文档不存在。")
            if row["superseded_by"] is not None:
                raise KnowledgeBaseError("该文档已被新版本替代，无需重新索引。")
            if row["status"] != "failed":
                raise KnowledgeBaseError("只有入库失败的文档可以重新索引。")
            stored = db.execute(
                "SELECT contents FROM document_files WHERE document_id = ?", (document_id,),
            ).fetchone()
            if stored is None:
                raise KnowledgeBaseError("该文档的原始内容未保留，请重新上传。")
            cursor = db.execute("""
                UPDATE documents SET status = 'processing', error = NULL, chunk_count = 0
                WHERE id = ? AND status = 'failed'
            """, (document_id,))
            if not cursor.rowcount:
                raise KnowledgeBaseError("文档状态已变化，请刷新后重试。")
            return row["file_type"], bytes(stored["contents"])

    async def index_document(self, document_id, file_type, contents):
        try:
            if self.embeddings is None:
                raise KnowledgeBaseError("尚未配置知识库向量服务。")
            chunks = await asyncio.to_thread(_document_chunks, _canonical_type(file_type), contents, self.sanitize)
            rows = []
            for offset in range(0, len(chunks), EMBEDDING_BATCH):
                batch = chunks[offset:offset + EMBEDDING_BATCH]
                vectors = await self.embeddings.embed([text for _, text in batch])
                rows.extend(
                    (document_id, position, location, text, _VECTOR.pack(*vector))
                    for position, ((location, text), vector) in enumerate(zip(batch, vectors), start=offset)
                )
            await asyncio.to_thread(self._save_index, document_id, rows)
        except KnowledgeBaseError as exc:
            await asyncio.to_thread(self._fail_index, document_id, str(exc))
        except asyncio.CancelledError:
            await asyncio.to_thread(self._fail_index, document_id, "索引处理已中断，请重新索引或重新上传。")
            raise
        except Exception:
            await asyncio.to_thread(self._fail_index, document_id, "建立索引时出现错误，请重新索引或重新上传。")
            raise

    def _save_index(self, document_id, rows):
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                """UPDATE documents SET status = 'indexed', error = NULL, chunk_count = ?
                   WHERE id = ? AND status = 'processing'""",
                (len(rows), document_id),
            )
            if not cursor.rowcount:
                return
            db.executemany("INSERT INTO chunks VALUES (?, ?, ?, ?, ?)", rows)
            previous = db.execute("SELECT supersedes FROM documents WHERE id = ?",
                                  (document_id,)).fetchone()["supersedes"]
            ancestors = [row["id"] for row in db.execute("""
                WITH RECURSIVE chain(id) AS (
                    SELECT ? AS id
                    UNION ALL
                    SELECT documents.supersedes FROM documents JOIN chain ON documents.id = chain.id
                    WHERE documents.supersedes IS NOT NULL
                )
                SELECT id FROM chain
            """, (previous,))] if previous else []
            if ancestors:
                # The new version is searchable from now on; retire older indexed versions.
                db.executemany(
                    "UPDATE documents SET superseded_by = ? WHERE id = ? AND superseded_by IS NULL",
                    [(document_id, ancestor) for ancestor in ancestors],
                )
                db.executemany(
                    "UPDATE documents SET status = 'retired' WHERE id = ? AND status = 'indexed'",
                    [(ancestor,) for ancestor in ancestors],
                )

    def _fail_index(self, document_id, message):
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE documents SET status = 'failed', error = ? WHERE id = ? AND status = 'processing'",
                (message, document_id),
            )

    async def search(self, question):
        try:
            if not question.strip() or not await asyncio.to_thread(self._has_chunks):
                return []
            if self.embeddings is None:
                raise KnowledgeBaseError("尚未配置知识库向量服务。")
            [query] = await self.embeddings.embed([self.sanitize(question)])
            return await asyncio.to_thread(self._find_chunks, query)
        except (sqlite3.Error, struct.error) as exc:
            raise KnowledgeBaseError("无法读取知识库索引，请稍后重试。") from exc

    def _has_chunks(self):
        with closing(self._connect()) as db:
            return db.execute("""
                SELECT 1 FROM chunks JOIN documents ON documents.id = chunks.document_id
                WHERE documents.status = 'indexed' LIMIT 1
            """).fetchone() is not None

    def _find_chunks(self, query):
        with closing(self._connect()) as db:
            rows = db.execute("""
                SELECT documents.id AS document_id, filename, location, text, embedding, source_url
                FROM chunks JOIN documents ON documents.id = chunks.document_id
                WHERE documents.status = 'indexed'
            """)
            return heapq.nlargest(SEARCH_LIMIT, self._scored_chunks(rows, query), key=lambda hit: hit["score"])

    @staticmethod
    def _scored_chunks(rows, query):
        for row in rows:
            # Document and query vectors are normalized by the embedding client.
            score = sum(left * right for left, right in zip(query, _VECTOR.unpack(row["embedding"])))
            if score >= MIN_SIMILARITY:
                hit = dict(row)
                del hit["embedding"]
                hit["score"] = min(score, 1.0)
                yield hit
