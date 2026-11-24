"""Serve the same OpenJiuwen workflows in a browser."""

import argparse
import asyncio
import json
import os
import time
from collections import deque
from contextlib import aclosing, asynccontextmanager, suppress
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import DEMO_MESSAGES, IMAGE_DEMO_MESSAGES, Conversation, Runner, build_agent
from diagnosis import redact
from knowledge_admin import AdminSessions, router as admin_router
from knowledge_answer import UNAVAILABLE, answer_question, stream_answer
from knowledge_base import BGEM3Embeddings, KnowledgeBase, KnowledgeBaseError
from model_observer import DEFAULT_MODEL, KEY_ENV, SUPPORTED_MODELS, ZhiyuanObserver


STATIC = Path(__file__).with_name("static")
COOKIE = "zhiyuan_session"
SESSION_AGE = 3600
SESSION_LIMIT = 128
TURN_LIMIT = 60
MODEL_REQUESTS_PER_MINUTE = 20
KNOWLEDGE_QUESTIONS_PER_MINUTE = 60
CASES = {"api": DEMO_MESSAGES, "image": IMAGE_DEMO_MESSAGES}


class Turn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


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
    case: str | None = None
    transcript: list = field(default_factory=list)
    touched: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def turn(self, message, knowledge=None, observer=None, on_preview=None):
        clean = self.conversation.sanitize(message)
        if knowledge is None:
            answer = await self.conversation.turn(clean)
            pair = {"user": clean, "assistant": answer, "interpreter": self.conversation.interpreter,
                    "notice": self.conversation.notice}
        else:
            try:
                hits = await knowledge.search(clean)
            except KnowledgeBaseError as exc:
                result = {"answer": UNAVAILABLE, "sources": [], "status": "unavailable", "notice": str(exc)}
            else:
                complete = observer.complete if observer else None
                if complete and on_preview:
                    def publish(content):
                        preview = self.conversation.sanitize(stream_answer(content))
                        if preview:
                            on_preview(preview)

                    async def complete(messages, *, max_tokens):
                        return await observer.complete(messages, max_tokens=max_tokens, on_delta=publish)
                result = await answer_question(clean, hits, complete, self.conversation.sanitize)
            pair = {"user": clean, "assistant": result["answer"], "interpreter": "knowledge",
                    "knowledge_status": result["status"], "notice": result["notice"], "sources": result["sources"],
                    "follow_ups": result.get("follow_ups", [])}
        self.transcript.append(pair)
        self.touched = time.monotonic()
        return {"turn": pair, "state": self.summary()}

    async def stream_turn(self, message, knowledge, observer):
        events = asyncio.Queue()

        async def run():
            try:
                result = await self.turn(
                    message, knowledge, observer,
                    lambda preview: events.put_nowait({"preview": preview}),
                )
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
            with suppress(asyncio.CancelledError):
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
            embedding_url = os.environ.get("ZHIYUAN_EMBEDDING_BASE_URL", "").strip()
            if embedding_url:
                embedding_key = os.environ.get("ZHIYUAN_EMBEDDING_API_KEY", "").strip()
                store = KnowledgeBase(
                    os.environ.get("ZHIYUAN_KNOWLEDGE_DB", "data/knowledge.sqlite3"),
                    BGEM3Embeddings(embedding_client, embedding_url, embedding_key),
                    sanitize=observer.sanitize if observer else redact,
                )
                await asyncio.to_thread(store.initialize)
                application.state.knowledge = store
            application.state.demo_agent = build_agent(workflow_id="zhiyuan-web-demo")
            application.state.manual_agent = build_agent(observer, workflow_id="zhiyuan-web-manual") if observer else application.state.demo_agent
            application.state.sessions = {}
            try:
                yield
            finally:
                await Runner.stop()

    application = FastAPI(title="致远一号使用问答助手", lifespan=lifetime, docs_url=None, redoc_url=None, openapi_url=None)

    def expire_sessions():
        now = time.monotonic()
        sessions = application.state.sessions
        for token, session in list(sessions.items()):
            if not session.lock.locked() and now - session.touched > SESSION_AGE:
                del sessions[token]

    def get_session(request):
        expire_sessions()
        return application.state.sessions.get(request.cookies.get(COOKIE))

    def new_session(request, response, case=None):
        sessions = application.state.sessions
        old = get_session(request)
        if old is not None:
            if old.lock.locked():
                raise HTTPException(409, "本轮排查进行中，请稍后再试。")
            del sessions[old.conversation.id]
        if len(sessions) >= SESSION_LIMIT:
            raise HTTPException(503, "当前会话较多，请稍后重试。")
        observer = application.state.observer
        agent = application.state.demo_agent if case else application.state.manual_agent
        sanitize = observer.sanitize if observer else redact
        session = BrowserSession(Conversation(agent, sanitize, case), case)
        sessions[session.conversation.id] = session
        response.set_cookie(COOKIE, session.conversation.id, max_age=SESSION_AGE, httponly=True,
                            secure=request.url.scheme == "https", samesite="strict")
        return session

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
    async def read_session(request: Request):
        session = get_session(request)
        enabled = application.state.knowledge is not None
        if session is None:
            return {"model": configured_model(), "knowledge_base_enabled": enabled, "case": None, "turns": [], "state": None}
        return {"model": configured_model(), "case": session.case, "turns": list(session.transcript),
                "state": session.summary(), "knowledge_base_enabled": enabled}

    @application.post("/api/reset")
    async def reset(request: Request, response: Response):
        new_session(request, response)
        return {"state": None}

    @application.post("/api/demo/{case}")
    async def demo(case: Literal["api", "image"], request: Request, response: Response):
        session = new_session(request, response, case)
        async with session.lock:
            frames = [await session.turn(message) for message in CASES[case]]
            session.conversation.agent = application.state.manual_agent
        return {"frames": frames, "case": case}

    @application.post("/api/turn")
    async def next_turn(body: Turn, request: Request, response: Response):
        message = body.message.strip()
        if not message:
            raise HTTPException(422, "请输入问题或脱敏报错。")
        session = get_session(request) or new_session(request, response)
        if session.lock.locked():
            raise HTTPException(409, "本轮排查进行中，请稍后再试。")
        if len(session.transcript) >= TURN_LIMIT:
            raise HTTPException(429, "本次会话已到上限，请新建会话。")
        if application.state.knowledge is not None and not record_request(
            application.state.knowledge_requests, KNOWLEDGE_QUESTIONS_PER_MINUTE,
        ):
            raise HTTPException(429, "知识库问答已达每分钟上限，请稍后再试。")
        if request.headers.get("accept") == "application/x-ndjson":
            await session.lock.acquire()

            async def stream():
                try:
                    async with aclosing(session.stream_turn(message, application.state.knowledge,
                                                            application.state.observer)) as events:
                        async for event in events:
                            yield event
                finally:
                    session.lock.release()

            return StreamingResponse(stream(), media_type="application/x-ndjson",
                                     headers={**dict(response.headers), "Cache-Control": "no-store",
                                              "X-Accel-Buffering": "no"})
        async with session.lock:
            return await session.turn(message, application.state.knowledge, application.state.observer)

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
