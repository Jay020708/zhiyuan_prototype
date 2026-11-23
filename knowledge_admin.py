"""Administrator sessions and document upload for the web knowledge base."""

import asyncio
import logging
import os
import secrets
import time
from collections import deque
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, SecretStr
from starlette.requests import ClientDisconnect


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
    return bool(request.app.state.admin.password and request.app.state.knowledge)


def require_admin(request):
    if not request.app.state.admin.authenticated(request):
        raise HTTPException(403, "请先登录管理员后台。")
    return request.app.state.knowledge


@router.get("/admin", include_in_schema=False)
async def admin_page():
    return FileResponse(Path(__file__).with_name("static") / "admin.html")


@router.get("/api/admin/session")
async def admin_session(request: Request):
    admin = request.app.state.admin
    return {"authenticated": admin.authenticated(request), "enabled": admin_enabled(request),
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
    knowledge = require_admin(request)
    return {"documents": await asyncio.to_thread(knowledge.list_documents)}


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


async def finish_index(knowledge, lock, document_id, file_type, contents):
    try:
        await knowledge.index_document(document_id, file_type, contents)
    except Exception:
        logger.exception("Knowledge-base indexing failed for document %s", document_id)
    finally:
        lock.release()


@router.post("/api/admin/documents", status_code=202)
async def upload_document(request: Request, background: BackgroundTasks):
    knowledge = require_admin(request)
    filename, file_type = identify_upload(request)
    admin = request.app.state.admin
    lock = admin.upload
    if lock.locked():
        raise HTTPException(409, "已有文件正在处理，请完成后再上传。")
    await lock.acquire()
    try:
        contents = await read_upload(request, admin.upload_limit)
        document = await asyncio.to_thread(knowledge.create_document, filename, file_type)
        background.add_task(finish_index, knowledge, lock, document["id"], file_type, contents)
    except BaseException:
        lock.release()
        raise
    return {"document": document}
