"""Extract, embed, and retrieve passages from locally indexed documents."""

import asyncio
import heapq
import math
import re
import sqlite3
import struct
from bisect import bisect_left
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from diagnosis import redact


EMBEDDING_MODEL = "BAAI/bge-m3"
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


class KnowledgeBaseError(Exception):
    """An error that can be shown to a knowledge-base user."""


class BGEM3Embeddings:
    def __init__(self, client, base_url, api_key=""):
        self.client = client
        self.url = f"{base_url.rstrip('/')}/embeddings"
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def embed(self, texts):
        try:
            response = await self.client.post(
                self.url, headers=self.headers,
                json={"model": EMBEDDING_MODEL, "input": texts, "encoding_format": "float"},
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
            raise KnowledgeBaseError("知识库向量服务返回了无效的 BAAI/bge-m3 向量。") from exc


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
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    document_id TEXT NOT NULL REFERENCES documents(id),
                    position INTEGER NOT NULL,
                    location TEXT NOT NULL,
                    text TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    PRIMARY KEY (document_id, position)
                );
            """)
            db.execute(
                "UPDATE documents SET status = 'failed', error = ? WHERE status = 'processing'",
                ("上次索引因服务重启而中断，请重新上传。",),
            )

    def create_document(self, filename, file_type):
        file_type = _canonical_type(file_type)
        filename = self.sanitize(filename.replace("\\", "/").rsplit("/", 1)[-1])
        document_id = uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO documents (id, filename, file_type, status, created_at) VALUES (?, ?, ?, 'processing', ?)",
                (document_id, filename, file_type, created_at),
            )
        return {"id": document_id, "filename": filename, "status": "processing", "error": None,
                "chunk_count": 0, "created_at": created_at}

    def list_documents(self):
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute("""
                SELECT id, filename, status, error, chunk_count, created_at
                FROM documents ORDER BY created_at DESC, id DESC
            """)]

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
            await asyncio.to_thread(self._fail_index, document_id, "索引处理已中断，请重新上传。")
            raise
        except Exception:
            await asyncio.to_thread(self._fail_index, document_id, "建立索引时出现错误，请重新上传。")
            raise

    def _save_index(self, document_id, rows):
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                "UPDATE documents SET status = 'indexed', chunk_count = ? WHERE id = ? AND status = 'processing'",
                (len(rows), document_id),
            )
            if cursor.rowcount:
                db.executemany("INSERT INTO chunks VALUES (?, ?, ?, ?, ?)", rows)

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
            return db.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is not None

    def _find_chunks(self, query):
        with closing(self._connect()) as db:
            rows = db.execute("""
                SELECT documents.id AS document_id, filename, location, text, embedding
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
