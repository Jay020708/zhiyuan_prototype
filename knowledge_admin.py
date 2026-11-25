"""Administrator sessions and document upload for the web knowledge base."""

import asyncio
import logging
import os
import re
import secrets
import time
from collections import deque
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, SecretStr
from starlette.requests import ClientDisconnect

from knowledge_base import KnowledgeBaseError, official_source


ADMIN_COOKIE = "zhiyuan_admin"
ADMIN_AGE = 3600
ADMIN_SESSION_LIMIT = 32
LOGIN_ATTEMPTS_PER_MINUTE = 10
LOGIN_CLIENT_LIMIT = 512
DEFAULT_UPLOAD_LIMIT = 50 * 1024 * 1024
FILE_TYPES = {
    ".txt": {"text/plain"},
    ".md": {"text/markdown", "text/x-markdown", "text/plain", "application/markdown"},
    ".pdf": {"application/pdf"},
}

router = APIRouter()
logger = logging.getLogger(__name__)


class Login(BaseModel):
    password: SecretStr


class AdminSessions:
    def __init__(self):
        self.password = os.environ.get("ZHIYUAN_ADMIN_PASSWORD", "").encode()
        self.upload_limit = int(os.environ.get("ZHIYUAN_UPLOAD_LIMIT_BYTES", DEFAULT_UPLOAD_LIMIT))
        if self.upload_limit <= 0:
            raise ValueError("ZHIYUAN_UPLOAD_LIMIT_BYTES must be positive")
        self.sessions = {}
        self.failed_logins = {}
        self.upload = asyncio.Lock()

    def authenticated(self, request):
        now = time.monotonic()
        for token, expires in list(self.sessions.items()):
            if expires <= now:
                del self.sessions[token]
        return request.cookies.get(ADMIN_COOKIE) in self.sessions

    def login(self, request, password):
        now = time.monotonic()
        client = request.client.host if request.client else ""
        for peer, attempts in list(self.failed_logins.items()):
            if now - attempts[-1] >= 60:
                del self.failed_logins[peer]
        attempts = self.failed_logins.get(client, deque())
        while attempts and now - attempts[0] >= 60:
            attempts.popleft()
        if (len(attempts) >= LOGIN_ATTEMPTS_PER_MINUTE
                or (client not in self.failed_logins and len(self.failed_logins) >= LOGIN_CLIENT_LIMIT)):
            raise HTTPException(429, "登录失败次数过多，请稍后再试。")
        if not secrets.compare_digest(self.password, password.encode(errors="surrogatepass")):
            attempts.append(now)
            self.failed_logins[client] = attempts
            raise HTTPException(401, "管理员密码不正确。")
        self.failed_logins.pop(client, None)
        if self.authenticated(request):
            self.logout(request)
        if len(self.sessions) >= ADMIN_SESSION_LIMIT:
            raise HTTPException(503, "管理员会话过多，请稍后重试。")
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + ADMIN_AGE
        return token

    def logout(self, request):
        self.sessions.pop(request.cookies.get(ADMIN_COOKIE), None)


def admin_enabled(request):
    return bool(request.app.state.admin.password)


def require_admin(request):
    if not request.app.state.admin.authenticated(request):
        raise HTTPException(403, "请先登录管理员后台。")


def require_knowledge(request):
    require_admin(request)
    knowledge = request.app.state.knowledge
    if knowledge is None:
        raise HTTPException(503, "知识库索引未就绪，请查看服务状态。")
    return knowledge


@router.get("/admin", include_in_schema=False)
async def admin_page():
    return FileResponse(Path(__file__).with_name("static") / "admin.html")


@router.get("/api/admin/session")
async def admin_session(request: Request):
    admin = request.app.state.admin
    return {"authenticated": admin.authenticated(request), "enabled": admin_enabled(request),
            "knowledge_configured": request.app.state.knowledge is not None,
            "upload_limit_bytes": admin.upload_limit}


@router.post("/api/admin/login")
async def admin_login(body: Login, request: Request, response: Response):
    if not admin_enabled(request):
        raise HTTPException(503, "管理员后台尚未配置。")
    token = request.app.state.admin.login(request, body.password.get_secret_value())
    response.set_cookie(ADMIN_COOKIE, token, max_age=ADMIN_AGE, httponly=True,
                        secure=request.url.scheme == "https", samesite="strict")
    return {"authenticated": True}


@router.post("/api/admin/logout", status_code=204)
async def admin_logout(request: Request, response: Response):
    request.app.state.admin.logout(request)
    response.delete_cookie(ADMIN_COOKIE, httponly=True,
                           secure=request.url.scheme == "https", samesite="strict")


@router.get("/api/admin/documents")
async def admin_documents(request: Request):
    knowledge = require_knowledge(request)
    return {"documents": await asyncio.to_thread(knowledge.list_documents)}


@router.get("/api/admin/services")
async def admin_services(request: Request):
    require_admin(request)
    return await request.app.state.service_status.check()


def identify_upload(request):
    try:
        filename = unquote(request.headers.get("x-document-filename", ""), errors="strict").strip()
    except UnicodeError as exc:
        raise HTTPException(422, "文件名编码无效。") from exc
    if (not filename or len(filename) > 255 or "/" in filename or "\\" in filename
            or any(not char.isprintable() for char in filename)):
        raise HTTPException(422, "文件名无效。")
    suffix = Path(filename).suffix.lower()
    media = request.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
    if suffix not in FILE_TYPES or (media != "application/octet-stream" and media not in FILE_TYPES[suffix]):
        raise HTTPException(415, "仅支持 TXT、Markdown 和 PDF 文件。")
    return filename, suffix


async def read_upload(request, limit):
    length = request.headers.get("content-length")
    if length is not None:
        try:
            length = int(length)
        except ValueError as exc:
            raise HTTPException(400, "文件大小无效。") from exc
        if length < 0:
            raise HTTPException(400, "文件大小无效。")
        if length > limit:
            raise HTTPException(413, "文件超过上传大小上限。")

    contents = bytearray()
    try:
        async for chunk in request.stream():
            if len(chunk) > limit - len(contents):
                raise HTTPException(413, "文件超过上传大小上限。")
            contents.extend(chunk)
    except ClientDisconnect as exc:
        raise HTTPException(400, "文件上传连接已中断。") from exc
    if not contents:
        raise HTTPException(422, "文件为空。")
    return bytes(contents)


async def replacement_target(request, knowledge):
    """Resolve an optional document that the new upload supersedes."""
    document_id = request.headers.get("x-document-supersedes", "").strip()
    if not document_id:
        return None
    if not re.fullmatch(r"[a-f0-9]{32}", document_id):
        raise HTTPException(422, "要替换的文档编号无效。")
    target = await asyncio.to_thread(knowledge.get_document, document_id)
    if target is None:
        raise HTTPException(404, "要替换的文档不存在。")
    if target["superseded_by"] is not None:
        raise HTTPException(409, "该文档已被新版本替代，请替换当前版本。")
    if target["status"] not in {"indexed", "failed"}:
        raise HTTPException(409, "只有已入库或入库失败的文档可以替换为新版本。")
    return target


async def finish_index(knowledge, lock, document_id, file_type, contents):
    try:
        await knowledge.index_document(document_id, file_type, contents)
    except Exception:
        logger.exception("Knowledge-base indexing failed for document %s", document_id)
    finally:
        lock.release()


@router.post("/api/admin/documents", status_code=202)
async def upload_document(request: Request, background: BackgroundTasks):
    knowledge = require_knowledge(request)
    filename, file_type = identify_upload(request)
    source_url = request.headers.get("x-document-source-url")
    if source_url is not None and not official_source(source_url):
        raise HTTPException(422, "文档原文链接必须是受支持的官方 HTTPS 地址。")
    admin = request.app.state.admin
    lock = admin.upload
    if lock.locked():
        raise HTTPException(409, "已有文件正在处理，请完成后再上传。")
    await lock.acquire()
    try:
        target = await replacement_target(request, knowledge)
        if source_url is None and target is not None:
            source_url = target["source_url"]
        contents = await read_upload(request, admin.upload_limit)
        try:
            document = await asyncio.to_thread(
                knowledge.create_document, filename, file_type,
                source_url=source_url, sha256=sha256(contents).hexdigest(),
                contents=contents, supersedes=target["id"] if target else None,
            )
        except KnowledgeBaseError as exc:
            raise HTTPException(409, str(exc)) from exc
        background.add_task(finish_index, knowledge, lock, document["id"], file_type, contents)
    except BaseException:
        lock.release()
        raise
    return {"document": document}


@router.post("/api/admin/documents/{document_id}/retry", status_code=202)
async def retry_document(document_id: str, request: Request, background: BackgroundTasks):
    knowledge = require_knowledge(request)
    if not re.fullmatch(r"[a-f0-9]{32}", document_id):
        raise HTTPException(404, "文档不存在。")
    admin = request.app.state.admin
    lock = admin.upload
    if lock.locked():
        raise HTTPException(409, "已有文件正在处理，请完成后再试。")
    await lock.acquire()
    try:
        try:
            file_type, contents = await asyncio.to_thread(knowledge.prepare_retry, document_id)
        except KnowledgeBaseError as exc:
            raise HTTPException(409, str(exc)) from exc
        document = await asyncio.to_thread(knowledge.get_document, document_id)
        background.add_task(finish_index, knowledge, lock, document_id, file_type, contents)
    except BaseException:
        lock.release()
        raise
    return {"document": document}
