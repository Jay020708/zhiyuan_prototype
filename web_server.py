"""Serve the same OpenJiuwen workflows in a browser."""

import argparse
import asyncio
import json
import os
import re
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from getpass import getpass
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
import uvicorn
from anyio import CancelScope
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import DEMO_MESSAGES, IMAGE_DEMO_MESSAGES, Conversation, Runner, build_agent
from chat_history import ChatHistory
from diagnosis import redact
from knowledge_admin import AdminSessions, router as admin_router
from knowledge_answer import UNAVAILABLE, answer_question, contextual_query, stream_answer
from knowledge_base import BGEM3Embeddings, KnowledgeBase, KnowledgeBaseError
from model_observer import DEFAULT_MODEL, KEY_ENV, SUPPORTED_MODELS, ZhiyuanObserver


STATIC = Path(__file__).with_name("static")
COOKIE = "zhiyuan_session"
BROWSER_COOKIE = "zhiyuan_browser"
BROWSER_AGE = 365 * 24 * 60 * 60
ACTIVE_TURN_LIMIT = 128
TURN_LIMIT = 60
MODEL_REQUESTS_PER_MINUTE = 20
KNOWLEDGE_QUESTIONS_PER_MINUTE = 60
CASES = {"api": DEMO_MESSAGES, "image": IMAGE_DEMO_MESSAGES}


class Turn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=64)


class ConversationStream(StreamingResponse):
    def __init__(self, events, release):
        super().__init__(events, media_type="application/x-ndjson",
                         headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        self.events = events
        self.release = release

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                with CancelScope(shield=True):
                    await self.events.aclose()
            finally:
                self.release()


def record_request(request_times, limit):
    now = time.monotonic()
    while request_times and now - request_times[0] >= 60:
        request_times.popleft()
    if len(request_times) >= limit:
        return False
    request_times.append(now)
    return True


class WebModelObserver(ZhiyuanObserver):
    def __init__(self, client, key, model):
        super().__init__(client, key, model)
        self.request_times = deque()

    async def complete(self, messages, *, max_tokens, on_delta=None):
        if not record_request(self.request_times, MODEL_REQUESTS_PER_MINUTE):
            return None, "本服务的模型调用已达每分钟上限"
        return await super().complete(messages, max_tokens=max_tokens, on_delta=on_delta)


@dataclass
class BrowserSession:
    conversation: Conversation
    owner: str
    history: ChatHistory
    case: str | None = None
    transcript: list = field(default_factory=list)

    async def turn(self, message, knowledge=None, observer=None, on_preview=None):
        clean = self.conversation.sanitize(message)
        if knowledge is None:
            answer = await self.conversation.turn(clean)
            pair = {"user": clean, "assistant": answer, "interpreter": self.conversation.interpreter,
                    "notice": self.conversation.notice}
        else:
            complete = observer.complete if observer else None
            query = await contextual_query(clean, self.transcript, complete, self.conversation.sanitize)
            try:
                hits = await knowledge.search(query)
            except KnowledgeBaseError as exc:
                result = {"answer": UNAVAILABLE, "sources": [], "status": "unavailable", "notice": str(exc)}
            else:
                if complete and on_preview:
                    def publish(content):
                        preview = self.conversation.sanitize(stream_answer(content))
                        if preview:
                            on_preview(preview)

                    async def complete(messages, *, max_tokens):
                        return await observer.complete(messages, max_tokens=max_tokens, on_delta=publish)
                result = await answer_question(clean, hits, complete, self.conversation.sanitize,
                                               history=self.transcript)
            pair = {"user": clean, "assistant": result["answer"], "interpreter": "knowledge",
                    "knowledge_status": result["status"], "notice": result["notice"], "sources": result["sources"],
                    "follow_ups": result.get("follow_ups", [])}
        write = asyncio.create_task(asyncio.to_thread(
            self.history.append, self.owner, self.conversation.id, pair, self.conversation.state,
        ))
        try:
            saved = await asyncio.shield(write)
        except asyncio.CancelledError:
            # SQLite's worker may already be committing. Keep the turn reservation until it finishes.
            with CancelScope(shield=True):
                await write
            raise
        if not saved:
            raise HTTPException(404, "对话不存在。")
        self.transcript.append(pair)
        return {"turn": pair, "state": self.summary()}

    async def metadata(self):
        return {"id": self.conversation.id,
                "conversations": await asyncio.to_thread(self.history.list, self.owner)}

    async def stream_turn(self, message, knowledge, observer):
        events = asyncio.Queue()

        async def run():
            try:
                result = await self.turn(
                    message, knowledge, observer,
                    lambda preview: events.put_nowait({"preview": preview}),
                )
                result.update(await self.metadata())
                events.put_nowait({"result": result})
            finally:
                events.put_nowait(None)

        task = asyncio.create_task(run())
        try:
            yield json.dumps({"user": self.conversation.sanitize(message)}) + "\n"
            while (event := await events.get()) is not None:
                yield json.dumps(event) + "\n"
            await task
        finally:
            task.cancel()
            with CancelScope(shield=True), suppress(asyncio.CancelledError):
                await task

    def summary(self):
        if self.transcript and self.transcript[-1]["interpreter"] == "knowledge":
            return None
        return self.conversation.summary()


def create_web_app(api_key=None):
    @asynccontextmanager
    async def lifetime(application):
        timeout = httpx.Timeout(60, connect=5)
        embedding_timeout = httpx.Timeout(60, connect=5)
        async with (
            httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client,
            httpx.AsyncClient(timeout=embedding_timeout, follow_redirects=False, trust_env=False) as embedding_client,
        ):
            key = api_key if api_key is not None else os.environ.get(KEY_ENV, "").strip()
            model_name = os.environ.get("ZHIYUAN_MODEL_NAME", DEFAULT_MODEL)
            observer = WebModelObserver(client, key, model_name) if key else None
            application.state.observer = observer
            application.state.admin = AdminSessions()
            application.state.knowledge = None
            application.state.knowledge_requests = deque()
            db_path = os.environ.get("ZHIYUAN_KNOWLEDGE_DB", "data/knowledge.sqlite3")
            application.state.history = ChatHistory(db_path)
            await asyncio.to_thread(application.state.history.initialize)
            application.state.active_turns = set()
            embedding_url = os.environ.get("ZHIYUAN_EMBEDDING_BASE_URL", "").strip()
            if embedding_url:
                embedding_key = os.environ.get("ZHIYUAN_EMBEDDING_API_KEY", "").strip()
                store = KnowledgeBase(
                    db_path,
                    BGEM3Embeddings(embedding_client, embedding_url, embedding_key),
                    sanitize=observer.sanitize if observer else redact,
                )
                await asyncio.to_thread(store.initialize)
                application.state.knowledge = store
            application.state.demo_agent = build_agent(workflow_id="zhiyuan-web-demo")
            application.state.manual_agent = build_agent(observer, workflow_id="zhiyuan-web-manual") if observer else application.state.demo_agent
            try:
                yield
            finally:
                await Runner.stop()

    application = FastAPI(title="致远一号使用问答助手", lifespan=lifetime, docs_url=None, redoc_url=None, openapi_url=None)

    def set_chat_cookie(request, response, name, value):
        response.set_cookie(name, value, max_age=BROWSER_AGE, httponly=True,
                            secure=request.url.scheme == "https", samesite="strict")

    def browser_owner(request, response, *, create=True):
        token = request.cookies.get(BROWSER_COOKIE, "")
        if not re.fullmatch(r"[a-f0-9]{64}", token):
            if not create:
                raise HTTPException(404, "对话不存在。")
            token = secrets.token_hex(32)
            set_chat_cookie(request, response, BROWSER_COOKIE, token)
        return sha256(token.encode("ascii")).hexdigest()

    def make_session(owner, case=None, row=None):
        observer = application.state.observer
        agent = application.state.demo_agent if case and row is None else application.state.manual_agent
        sanitize = observer.sanitize if observer else redact
        conversation = Conversation(agent, sanitize, case)
        if row:
            conversation.id = row["id"]
            if row["state"] is not None:
                conversation.state = row["state"]
        return BrowserSession(conversation, owner, application.state.history, case,
                              row["turns"] if row else [])

    async def get_session(owner, conversation_id):
        if not conversation_id:
            return None
        row = await asyncio.to_thread(application.state.history.get, owner, conversation_id)
        return make_session(owner, row["case"], row) if row else None

    async def persist_session(request, response, session):
        await asyncio.to_thread(application.state.history.create, session.owner,
                                session.conversation.id, session.case)
        set_chat_cookie(request, response, COOKIE, session.conversation.id)
        return session

    async def session_payload(owner, session):
        conversations = await asyncio.to_thread(application.state.history.list, owner)
        return {"id": session.conversation.id if session else None,
                "model": configured_model(),
                "knowledge_base_enabled": application.state.knowledge is not None,
                "case": session.case if session else None, "turns": session.transcript if session else [],
                "state": session.summary() if session and session.transcript else None,
                "conversations": conversations}

    def configured_model():
        observer = application.state.observer
        return observer.model if observer else None

    @application.middleware("http")
    async def browser_headers(request, call_next):
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and urlsplit(origin).netloc != request.url.netloc:
            return Response(status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; base-uri 'self'; object-src 'none'"
        if response.headers.get("content-type", "").startswith("application/json"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/api/session")
    async def read_session(request: Request, response: Response):
        owner = browser_owner(request, response)
        active_id = request.cookies.get(COOKIE)
        session = await get_session(owner, active_id)
        if active_id and session is None:
            response.delete_cookie(COOKIE)
        return await session_payload(owner, session)

    @application.get("/api/conversations")
    async def list_conversations(request: Request, response: Response):
        owner = browser_owner(request, response)
        return {"conversations": await asyncio.to_thread(application.state.history.list, owner)}

    @application.post("/api/conversations")
    @application.post("/api/reset")
    async def create_conversation(request: Request, response: Response):
        owner = browser_owner(request, response)
        session = await persist_session(request, response, make_session(owner))
        return await session_payload(owner, session)

    @application.get("/api/conversations/{conversation_id}")
    @application.post("/api/conversations/{conversation_id}/select")
    async def open_conversation(conversation_id: str, request: Request, response: Response):
        owner = browser_owner(request, response, create=False)
        session = await get_session(owner, conversation_id)
        if session is None:
            raise HTTPException(404, "对话不存在。")
        if request.method == "POST":
            set_chat_cookie(request, response, COOKIE, conversation_id)
        return await session_payload(owner, session)

    def reserve_turn(key):
        active = application.state.active_turns
        if key in active:
            raise HTTPException(409, "本轮对话正在生成，请稍后再试。")
        if len(active) >= ACTIVE_TURN_LIMIT:
            raise HTTPException(503, "当前对话较多，请稍后重试。")
        active.add(key)

    @application.post("/api/demo/{case}")
    async def demo(case: Literal["api", "image"], request: Request, response: Response):
        owner = browser_owner(request, response)
        session = make_session(owner, case)
        key = (owner, session.conversation.id)
        reserve_turn(key)
        try:
            await persist_session(request, response, session)
            frames = [await session.turn(message) for message in CASES[case]]
            return {"frames": frames, "case": case, **await session.metadata()}
        finally:
            application.state.active_turns.discard(key)

    @application.post("/api/turn")
    async def next_turn(body: Turn, request: Request, response: Response):
        message = body.message.strip()
        if not message:
            raise HTTPException(422, "请输入问题或脱敏报错。")
        conversation_id = body.conversation_id if "conversation_id" in body.model_fields_set else request.cookies.get(COOKIE)
        owner = browser_owner(request, response, create=conversation_id is None)
        session = None
        if conversation_id is None:
            session = make_session(owner)
            conversation_id = session.conversation.id
        key = (owner, conversation_id)
        reserve_turn(key)
        streaming = False
        try:
            if session is None:
                session = await get_session(owner, conversation_id)
            else:
                await persist_session(request, response, session)
            if session is None:
                raise HTTPException(404, "对话不存在。")
            if len(session.transcript) >= TURN_LIMIT:
                raise HTTPException(429, "本次会话已到上限，请新建会话。")
            knowledge = application.state.knowledge
            observer = application.state.observer
            if knowledge is not None and not record_request(
                application.state.knowledge_requests, KNOWLEDGE_QUESTIONS_PER_MINUTE,
            ):
                raise HTTPException(429, "知识库问答已达每分钟上限，请稍后再试。")
            if request.headers.get("accept") == "application/x-ndjson":
                reply = ConversationStream(
                    session.stream_turn(message, knowledge, observer),
                    lambda: application.state.active_turns.discard(key),
                )
                reply.raw_headers.extend(header for header in response.raw_headers if header[0] == b"set-cookie")
                streaming = True
                return reply
            result = await session.turn(message, knowledge, observer)
            return {**result, **await session.metadata()}
        finally:
            if not streaming:
                application.state.active_turns.discard(key)

    @application.get("/api/health")
    async def health():
        return {"status": "ok", "model_configured": configured_model() is not None}

    application.include_router(admin_router)
    application.mount("/", StaticFiles(directory=STATIC, html=True), name="web")
    return application


app = create_web_app()


def main():
    load_dotenv(Path(__file__).with_name(".env"), interpolate=False)
    parser = argparse.ArgumentParser(description="启动致远一号网页演示")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--live", action="store_true", help="安全输入赛事 API Key，用于手动对话")
    args = parser.parse_args()
    key = os.environ.get(KEY_ENV, "").strip()
    if args.live and not key:
        key = getpass("致远一号赛事 API Key（输入隐藏）：").strip()
        if not key:
            parser.error("请提供赛事 API Key。")
    uvicorn.run(create_web_app(key), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
