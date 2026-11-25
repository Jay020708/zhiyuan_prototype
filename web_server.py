"""Serve the same OpenJiuwen workflows in a browser."""

import argparse
import asyncio
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from functools import partial
from getpass import getpass
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
import uvicorn
from anyio import CancelScope
from anyio.to_thread import run_sync
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import DEMO_MESSAGES, IMAGE_DEMO_MESSAGES, Conversation, Runner, build_agent
from chat_history import TITLE_LIMIT, ChatHistory
from diagnosis import new_state, observe, redact
from image_diagnosis import local_image_observation, new_image_state
from knowledge_admin import AdminSessions, router as admin_router
from knowledge_answer import UNAVAILABLE, answer_question, contextual_query, stream_answer
from knowledge_base import BGEM3Embeddings, KnowledgeBase, KnowledgeBaseError
from model_observer import DEFAULT_MODEL, KEY_ENV, MODEL_TIMEOUT_NOTICE, SUPPORTED_MODELS, ZhiyuanObserver, read_model_json
from service_status import ServiceStatus


STATIC = Path(__file__).with_name("static")
COOKIE = "zhiyuan_session"
BROWSER_COOKIE = "zhiyuan_browser"
BROWSER_AGE = 365 * 24 * 60 * 60
ACTIVE_TURN_LIMIT = 128
TURN_LIMIT = 60
MODEL_REQUESTS_PER_MINUTE = 20
DIAGNOSIS_MODEL_TIMEOUT = 12
CONTEXT_MODEL_TIMEOUT = 8
MODEL_FIRST_TOKEN_TIMEOUT = 45
MODEL_STREAM_IDLE_TIMEOUT = 20
MODEL_GENERATION_TIMEOUT = 90
KNOWLEDGE_QUESTIONS_PER_MINUTE = 60
CASES = {"api": DEMO_MESSAGES, "image": IMAGE_DEMO_MESSAGES}

TERMINAL_ROUTES = {"resolved", "support", "image_resolved", "image_support"}
REFERENCE_QUESTION = re.compile(r"什么是|是什么|什么时候|何时|含义|怎么|如何|哪里|哪儿|多少|能否|是否|[？?]")
GENERIC_QUESTION = re.compile(r"什么是|是什么|是什么意思|含义|文档在哪|怎么申请|如何申请|怎么查|如何查")
API_CONTEXT = re.compile(r"\bapi\b|/models|请求|模型调用|鉴权|连接|connection", re.IGNORECASE)
logger = logging.getLogger("uvicorn.error")


def reported_case(message, previous_question=""):
    """Recognize supported incidents before sending a question to document search."""
    if GENERIC_QUESTION.search(message):
        return None
    image = local_image_observation(message, {})
    if (image["event"] in {"missing", "copy_failed", "pip_failed"}
            or re.search(r"module\s*not\s*found|(?:镜像|容器|docker).{0,30}(?:缺依赖|缺少.{0,12}模块)",
                         message, re.IGNORECASE)):
        return "image"
    result = observe(message, "start")["result"]
    if result in {"timeout", "401"} and (
        API_CONTEXT.search(message) or API_CONTEXT.search(previous_question)
        or re.fullmatch(r"(?:一直|还是|又)?(?:超时了?|返回\s*401|401)[。！!？?]?", message)
    ):
        return "api"
    if API_CONTEXT.search(message) and re.search(r"一直等不到|等了很久.{0,12}(?:断开|没响应)|始终没响应", message):
        return "api"
    return None


def has_diagnostic_feedback(message, state):
    if not REFERENCE_QUESTION.search(message):
        return message.rstrip("。！!") not in {"你好", "谢谢"}
    statement = re.split(r"[，,。；;]", message, maxsplit=1)[0].strip()
    if statement == message or REFERENCE_QUESTION.search(statement):
        return False
    observation = (local_image_observation(statement, state) if state["scenario"] == "image"
                   else observe(statement, state["route"]))
    return any(value is not None for value in observation.values())


class Turn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=64)


class ConversationTitle(BaseModel):
    title: str


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

    async def extract(self, message, state):
        try:
            return await asyncio.wait_for(super().extract(message, state), DIAGNOSIS_MODEL_TIMEOUT)
        except TimeoutError:
            return None, "模型提取超时"

    async def complete(self, messages, *, max_tokens, on_delta=None, response_timeout=MODEL_GENERATION_TIMEOUT):
        result, started_stream = await self.complete_with_deadline(messages, max_tokens, on_delta, response_timeout)
        if result[1] in {MODEL_TIMEOUT_NOTICE, "等待模型首段回复超时", "连接致远一号模型 API 超时"} and not started_stream:
            retry, _ = await self.complete_with_deadline(messages, max_tokens, on_delta, response_timeout)
            if retry[1] != "本服务的模型调用已达每分钟上限":
                return retry
        return result

    async def complete_with_deadline(self, messages, max_tokens, on_delta, response_timeout):
        if not record_request(self.request_times, MODEL_REQUESTS_PER_MINUTE):
            return (None, "本服务的模型调用已达每分钟上限"), False
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + response_timeout
        initial = min(deadline, started + MODEL_FIRST_TOKEN_TIMEOUT) if on_delta else deadline
        received = ""

        def progress(content):
            nonlocal received
            received = content
            timer.reschedule(min(deadline, loop.time() + MODEL_STREAM_IDLE_TIMEOUT))
            on_delta(content)

        try:
            async with asyncio.timeout_at(initial) as timer:
                content, notice = await super().complete(
                    messages, max_tokens=max_tokens, on_delta=progress if on_delta else None,
                )
        except TimeoutError:
            content = None
            notice = ("模型生成超过整轮时限" if loop.time() >= deadline else
                      "模型流式输出停顿超时" if received else "等待模型首段回复超时")
        if notice and ("超时" in notice or "时限" in notice):
            logger.warning("致远模型调用超时 model=%s elapsed=%.2fs phase=%s",
                           self.model, loop.time() - started, notice)
            if received:
                try:
                    result = read_model_json(received)
                except ValueError:
                    result = None
                if isinstance(result, dict) and isinstance(result.get("answer"), str) and result["answer"].strip():
                    # The answer arrived before the stream stopped sending events.
                    return (received, None), True
        return (content, notice), bool(received)


@dataclass
class BrowserSession:
    conversation: Conversation
    owner: str = ""
    history: ChatHistory | None = None
    transcript: list = field(default_factory=list)

    def diagnostic_target(self, message):
        state = self.conversation.state
        previous = self.transcript[-1]["user"] if self.transcript else ""
        if incident := reported_case(message, previous):
            return incident
        if state["scenario"] and state["route"] not in TERMINAL_ROUTES and has_diagnostic_feedback(message, state):
            return state["scenario"]
        return None

    async def turn(self, message, knowledge=None, observer=None, on_preview=None, *, target=None):
        clean = self.conversation.sanitize(message)
        if target:
            state = self.conversation.state
            if state["scenario"] != target or state["route"] in TERMINAL_ROUTES:
                self.conversation.state = new_image_state() if target == "image" else new_state()
            answer = await self.conversation.turn(clean)
            pair = {"user": clean, "assistant": answer, "interpreter": self.conversation.interpreter,
                    "notice": self.conversation.notice, "kind": "diagnosis"}
        else:
            complete = observer.complete if observer else None
            try:
                hits = []
                if knowledge:
                    rewrite = partial(complete, response_timeout=CONTEXT_MODEL_TIMEOUT) if complete else None
                    query = await contextual_query(clean, self.transcript, rewrite, self.conversation.sanitize)
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
                if knowledge is None:
                    result["notice"] = "后端暂未配置知识库检索。"
            pair = {"user": clean, "assistant": result["answer"], "interpreter": "knowledge",
                    "kind": "knowledge",
                    "knowledge_status": result["status"], "notice": result["notice"], "sources": result["sources"],
                    "follow_ups": result.get("follow_ups", [])}
        if self.history is not None:
            await self.save_turn(pair)
        self.transcript.append(pair)
        return {"turn": pair, "state": self.summary()}

    async def save_turn(self, pair):
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

    async def metadata(self):
        return {"id": self.conversation.id,
                "conversations": await asyncio.to_thread(self.history.list, self.owner)}

    async def stream_turn(self, message, knowledge, observer, *, target):
        events = asyncio.Queue()

        async def run():
            try:
                result = await self.turn(
                    message, knowledge, observer,
                    lambda preview: events.put_nowait({"preview": preview}),
                    target=target,
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
        if self.conversation.state["scenario"] is None:
            return None
        return self.conversation.summary()


def create_web_app(api_key=None, *, startup_checks=False):
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
            observer, model_error = None, None
            if key:
                try:
                    observer = WebModelObserver(client, key, model_name)
                except ValueError:
                    model_error = "生成模型名称与接口配置不匹配。"
            application.state.observer = observer
            application.state.admin = AdminSessions()
            application.state.knowledge = None
            application.state.knowledge_requests = deque()
            db_path = os.environ.get("ZHIYUAN_KNOWLEDGE_DB", "data/knowledge.sqlite3")
            application.state.history = ChatHistory(db_path)
            await asyncio.to_thread(application.state.history.initialize)
            application.state.active_turns = set()
            embeddings, embedding_error, index_error = None, None, None
            try:
                embeddings = BGEM3Embeddings.from_env(embedding_client)
            except ValueError:
                embedding_error = "向量服务地址格式无效，请检查 ZHIYUAN_EMBEDDING_BASE_URL。"
            if embeddings:
                store = KnowledgeBase(
                    db_path,
                    embeddings,
                    sanitize=observer.sanitize if observer else redact,
                )
                try:
                    await asyncio.to_thread(store.initialize)
                except ValueError as exc:
                    index_error = str(exc)
                else:
                    application.state.knowledge = store
            application.state.service_status = ServiceStatus(
                embeddings, observer, application.state.knowledge, embedding_error=embedding_error,
                model_error=model_error, index_error=index_error,
            )
            application.state.demo_agent = build_agent(workflow_id="zhiyuan-web-demo")
            application.state.manual_agent = build_agent(observer, workflow_id="zhiyuan-web-manual") if observer else application.state.demo_agent
            async def check_startup_services():
                results = await application.state.service_status.check()
                for name in ("embedding", "model", "index"):
                    result = results[name]
                    logger.log(logging.INFO if result["status"] == "ready" else logging.WARNING,
                               "服务检查 %s · %s", name, result["detail"])

            startup_probe = asyncio.create_task(check_startup_services()) if startup_checks else None
            try:
                yield
            finally:
                if startup_probe:
                    startup_probe.cancel()
                    with suppress(asyncio.CancelledError):
                        await startup_probe
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

    def make_session(owner, row=None):
        observer = application.state.observer
        sanitize = observer.sanitize if observer else redact
        conversation = Conversation(application.state.manual_agent, sanitize, row["case"] if row else None)
        if row:
            conversation.id = row["id"]
            if row["state"] is not None:
                conversation.state = row["state"]
        return BrowserSession(conversation, owner, application.state.history, row["turns"] if row else [])

    async def get_session(owner, conversation_id):
        if not conversation_id:
            return None
        row = await asyncio.to_thread(application.state.history.get, owner, conversation_id)
        return make_session(owner, row) if row else None

    async def persist_session(request, response, session):
        await asyncio.to_thread(application.state.history.create, session.owner,
                                session.conversation.id)
        set_chat_cookie(request, response, COOKIE, session.conversation.id)
        return session

    async def session_payload(owner, session, *, conversations=None):
        if conversations is None:
            conversations = await asyncio.to_thread(application.state.history.list, owner)
        return {"id": session.conversation.id if session else None,
                "model": configured_model(),
                "knowledge_base_enabled": application.state.knowledge is not None,
                "mode": "auto", "case": None, "turns": session.transcript if session else [],
                "state": session.summary() if session and session.transcript else None,
                "conversations": conversations}

    def configured_model():
        observer = application.state.observer
        return observer.model if observer else None

    @application.middleware("http")
    async def browser_headers(request, call_next):
        origin = request.headers.get("origin")
        if request.method in {"POST", "PATCH", "DELETE"} and origin and urlsplit(origin).netloc != request.url.netloc:
            return Response(status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; base-uri 'self'; object-src 'none'"
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if content_type == "application/json":
            response.headers["Cache-Control"] = "no-store"
        elif content_type in {"text/html", "text/css", "text/javascript", "application/javascript"} or response.status_code == 304:
            response.headers["Cache-Control"] = "no-cache"
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

    @application.patch("/api/conversations/{conversation_id}")
    async def rename_conversation(conversation_id: str, body: ConversationTitle, request: Request, response: Response):
        owner = browser_owner(request, response, create=False)
        title = " ".join(body.title.split())
        if not 1 <= len(title) <= TITLE_LIMIT:
            raise HTTPException(422, f"对话名称须为 1 到 {TITLE_LIMIT} 个字符。")
        observer = application.state.observer
        title = observer.sanitize(title) if observer else redact(title)
        history = application.state.history
        if not await asyncio.to_thread(history.rename, owner, conversation_id, title):
            raise HTTPException(404, "对话不存在。")
        return {"conversations": await asyncio.to_thread(history.list, owner)}

    @application.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str, request: Request, response: Response):
        owner = browser_owner(request, response, create=False)
        history = application.state.history
        key = (owner, conversation_id)
        reserve_turn(key)
        try:
            if not await run_sync(history.delete, owner, conversation_id):
                raise HTTPException(404, "对话不存在。")
        finally:
            application.state.active_turns.discard(key)

        conversations = await asyncio.to_thread(history.list, owner)
        active_id = request.cookies.get(COOKIE)
        if active_id == conversation_id:
            active_id = conversations[0]["id"] if conversations else None
            if active_id:
                set_chat_cookie(request, response, COOKIE, active_id)
            else:
                response.delete_cookie(COOKIE)
        session = await get_session(owner, active_id)
        return await session_payload(owner, session, conversations=conversations)

    @application.post("/api/demo/{case}")
    async def demo(case: Literal["api", "image"]):
        sample = BrowserSession(Conversation(application.state.demo_agent, case=case))
        frames = [await sample.turn(message, target=case) for message in CASES[case]]
        return {"frames": frames, "case": case}

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
            target = session.diagnostic_target(session.conversation.sanitize(message))
            knowledge = application.state.knowledge if target is None else None
            observer = application.state.observer
            if knowledge is not None and not record_request(
                application.state.knowledge_requests, KNOWLEDGE_QUESTIONS_PER_MINUTE,
            ):
                raise HTTPException(429, "知识库问答已达每分钟上限，请稍后再试。")
            if request.headers.get("accept") == "application/x-ndjson":
                reply = ConversationStream(
                    session.stream_turn(message, knowledge, observer, target=target),
                    lambda: application.state.active_turns.discard(key),
                )
                reply.raw_headers.extend(header for header in response.raw_headers if header[0] == b"set-cookie")
                streaming = True
                return reply
            result = await session.turn(message, knowledge, observer, target=target)
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


app = create_web_app(startup_checks=True)


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
    uvicorn.run(create_web_app(key, startup_checks=True), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
