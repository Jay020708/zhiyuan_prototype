import asyncio
import json
import os
import tempfile
import threading
import unittest
from contextlib import closing
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from web_server import COOKIE, WebModelObserver, create_web_app


class WebTest(unittest.TestCase):
    def test_manage_saved_conversations(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "ZHIYUAN_EMBEDDING_BASE_URL": "", "ZHIYUAN_KNOWLEDGE_DB": os.path.join(directory, "web.sqlite3"),
        }), TestClient(create_web_app("")) as client:
            client.post("/api/turn", json={"message": "连接超时了"}).raise_for_status()
            first = client.get("/api/session").json()
            first_path = f"/api/conversations/{first['id']}"
            second = client.post("/api/conversations").json()
            second_path = f"/api/conversations/{second['id']}"
            third = client.post("/api/conversations").json()
            third_path = f"/api/conversations/{third['id']}"

            for title in ("  ", "长" * 33):
                self.assertEqual(client.patch(first_path, json={"title": title}).status_code, 422)
            for method in ("PATCH", "DELETE"):
                self.assertEqual(client.request(method, first_path, headers={
                    "Origin": "https://outside.example",
                }).status_code, 403)
            with closing(TestClient(client.app)) as other:
                other.get("/api/session").raise_for_status()
                self.assertEqual(other.patch(first_path, json={"title": "别人的对话"}).status_code, 404)
                self.assertEqual(other.delete(first_path).status_code, 404)

            renamed = client.patch(first_path, json={"title": "  校外 API 排查  "})
            renamed.raise_for_status()
            self.assertEqual([item["id"] for item in renamed.json()["conversations"]],
                             [third["id"], second["id"], first["id"]])
            self.assertEqual(renamed.json()["conversations"][-1]["title"], "校外 API 排查")
            self.assertEqual(client.get("/api/session").json()["id"], third["id"])
            opened = client.post(f"{first_path}/select").json()
            self.assertEqual(opened["state"], first["state"])
            self.assertEqual(opened["turns"], first["turns"])
            client.post("/api/turn", json={"message": "我在校外，API 超时"}).raise_for_status()
            self.assertEqual(client.get("/api/conversations").json()["conversations"][0]["title"], "校外 API 排查")

            after_inactive_delete = client.delete(second_path).json()
            self.assertEqual(after_inactive_delete["id"], first["id"])
            self.assertEqual([item["id"] for item in after_inactive_delete["conversations"]], [first["id"], third["id"]])
            self.assertEqual(client.get(second_path).status_code, 404)

            after_active_delete = client.delete(first_path).json()
            self.assertEqual(after_active_delete["id"], third["id"])
            self.assertEqual(client.get("/api/session").json(), after_active_delete)
            self.assertEqual(client.post(f"{first_path}/select").status_code, 404)
            self.assertEqual(client.patch(first_path, json={"title": "已删除"}).status_code, 404)

            empty = client.delete(third_path).json()
            self.assertIsNone(empty["id"])
            self.assertEqual(empty["conversations"], [])
            self.assertIsNone(client.cookies.get(COOKIE))
            self.assertEqual(client.get("/api/session").json()["turns"], [])

    def test_web_demos_manual_turns_refresh_and_session_isolation(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "ZHIYUAN_EMBEDDING_BASE_URL": "", "ZHIYUAN_KNOWLEDGE_DB": os.path.join(directory, "web.sqlite3"),
        }), TestClient(create_web_app("")) as client:
            home = client.get("/")
            self.assertEqual(home.status_code, 200)
            self.assertIn("自定义镜像缺依赖", home.text)
            self.assertIn("default-src 'self'", home.headers["content-security-policy"])
            self.assertEqual(home.headers["cache-control"], "no-cache")
            for path in ("/app.js", "/styles.css"):
                asset = client.get(path)
                self.assertEqual(asset.status_code, 200)
                self.assertEqual(asset.headers["cache-control"], "no-cache")
                revalidated = client.get(path, headers={"If-None-Match": asset.headers["etag"]})
                self.assertEqual(revalidated.status_code, 304)
                self.assertEqual(revalidated.headers["cache-control"], "no-cache")
            self.assertEqual(client.get("/api/session").json()["turns"], [])

            api = client.post("/api/demo/api").json()["frames"]
            self.assertEqual(len(api), 7)
            self.assertIn("鉴权", api[4]["state"]["pending"])
            self.assertIn("401", api[4]["state"]["history"][0])
            self.assertEqual(api[-1]["state"]["status"], "resolved")
            self.assertIsNone(client.cookies.get(COOKIE))
            self.assertEqual(client.get("/api/session").json()["turns"], [])

            image = client.post("/api/demo/image").json()["frames"]
            self.assertEqual(len(image), 8)
            self.assertIn("Dockerfile", image[2]["turn"]["assistant"])
            self.assertEqual(image[4]["state"]["pending"], "在新镜像中验证导入")
            self.assertEqual(len(image[-1]["state"]["history"]), 5)
            self.assertIsNone(client.cookies.get(COOKIE))

            other_browser = TestClient(client.app)
            self.assertEqual(other_browser.get("/api/session").json()["turns"], [])
            other_browser.close()
            self.assertEqual(client.post("/api/reset").status_code, 200)
            self.assertEqual(client.get("/api/session").json()["turns"], [])

            initial = client.post("/api/turn", json={"message": "连接超时了"})
            self.assertIn("httponly", client.post("/api/reset").headers["set-cookie"].lower())
            self.assertEqual(initial.json()["state"]["pending"], "确认请求的运行位置")
            client.post("/api/turn", json={"message": "我在校外，API 超时"})
            vpn = client.post("/api/turn", json={"message": "没有"}).json()
            self.assertEqual(vpn["state"]["facts"]["VPN"], "未连接")
            real_cookie = client.cookies.get(COOKIE)
            real_session = client.get("/api/session").json()
            client.post("/api/demo/image").raise_for_status()
            self.assertEqual(client.cookies.get(COOKIE), real_cookie)
            self.assertEqual(client.get("/api/session").json(), real_session)
            changed = client.post("/api/turn", json={"message": "连上 VPN 后返回 401，API_KEY=fake-secret"})
            self.assertNotIn("fake-secret", changed.text)
            restored = client.get("/api/session").json()
            self.assertEqual(restored["state"]["pending"], "本地核对鉴权后重试")
            self.assertEqual(len(restored["turns"]), 3)
            self.assertEqual(changed.headers["cache-control"], "no-store")

            forbidden = client.post("/api/reset", headers={"Origin": "https://outside.example"})
            self.assertEqual(forbidden.status_code, 403)
            self.assertEqual(len(client.get("/api/session").json()["turns"]), 3)


class WebStreamingTest(unittest.IsolatedAsyncioTestCase):
    async def test_model_timeout_retries_only_before_stream_content(self):
        from model_observer import MODEL_TIMEOUT_NOTICE, ZhiyuanObserver
        from web_server import WebModelObserver

        for started in (False, True):
            with self.subTest(started=started):
                with patch.dict(os.environ, {"ZHIYUAN_MODEL_BASE_URL": "https://chat.test/v1"}):
                    observer = WebModelObserver(None, "test-key", "deepseek-chat")
                previews = []
                calls = []

                async def upstream(self, messages, *, max_tokens, on_delta):
                    calls.append(messages)
                    if len(calls) == 1:
                        if started:
                            on_delta("已有输出")
                        return None, MODEL_TIMEOUT_NOTICE
                    return "重试成功", None

                with patch.object(ZhiyuanObserver, "complete", upstream):
                    result = await observer.complete([], max_tokens=100, on_delta=previews.append)
                self.assertEqual(len(calls), 1 if started else 2)
                self.assertEqual(previews, ["已有输出"] if started else [])
                self.assertEqual(result, (None, MODEL_TIMEOUT_NOTICE) if started else ("重试成功", None))

    async def test_disconnected_stream_holds_turn_while_history_write_finishes(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "ZHIYUAN_EMBEDDING_BASE_URL": "", "ZHIYUAN_KNOWLEDGE_DB": os.path.join(directory, "web.sqlite3"),
        }):
            app = create_web_app("")
            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
            ) as client:
                created = await client.post("/api/conversations")
                self.assertEqual(created.status_code, 200)
                first = "连接超时了"
                second = "我在校外，API 超时"
                append_started = threading.Event()
                release_append = threading.Event()
                append_finished = threading.Event()
                append = app.state.history.append

                def delay_first_append(owner, session_id, turn, state):
                    if turn["user"] != first:
                        return append(owner, session_id, turn, state)
                    append_started.set()
                    if not release_append.wait(10):
                        raise TimeoutError("Test did not release the pending history write")
                    result = append(owner, session_id, turn, state)
                    append_finished.set()
                    return result

                request = client.build_request("POST", "/api/turn", json={"message": first},
                                               headers={"accept": "application/x-ndjson"})
                scope = {
                    "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                    "method": "POST", "scheme": "http", "path": "/api/turn", "raw_path": b"/api/turn",
                    "query_string": b"", "root_path": "",
                    "headers": [(name.lower(), value) for name, value in request.headers.raw],
                    "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
                }
                disconnect = asyncio.Event()
                disconnect_received = asyncio.Event()
                body = {"type": "http.request", "body": request.content}

                async def receive():
                    nonlocal body
                    if body is not None:
                        message, body = body, None
                        return message
                    await disconnect.wait()
                    disconnect_received.set()
                    return {"type": "http.disconnect"}

                async def send(message):
                    pass

                with patch.object(app.state.history, "append", delay_first_append):
                    stream = asyncio.create_task(app(scope, receive, send))
                    try:
                        self.assertTrue(await asyncio.to_thread(append_started.wait, 5))
                        disconnect.set()
                        await asyncio.wait_for(disconnect_received.wait(), 5)
                        # Let stream cancellation reach its cleanup while the database thread is blocked.
                        await asyncio.wait({stream}, timeout=0.1)
                        conflict = await client.post("/api/turn", json={"message": second})
                        self.assertEqual(conflict.status_code, 409)
                        conflict = await client.delete(f"/api/conversations/{created.json()['id']}")
                        self.assertEqual(conflict.status_code, 409)
                    finally:
                        disconnect.set()
                        release_append.set()
                        await asyncio.wait_for(stream, 5)
                        if append_started.is_set():
                            self.assertTrue(await asyncio.to_thread(append_finished.wait, 5))

                replied = await client.post("/api/turn", json={"message": second})
                self.assertEqual(replied.status_code, 200)
                restored = (await client.get("/api/session")).json()
                self.assertEqual([turn["user"] for turn in restored["turns"]], [first, second])


class ModelStreamDeadlineTest(unittest.IsolatedAsyncioTestCase):
    async def stream(self, pieces, *, stall=True):
        class TimedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for delay, delta, finished in pieces:
                    await asyncio.sleep(delay)
                    choice = {"delta": {"content": delta}, "finish_reason": "stop" if finished else None}
                    event = json.dumps({"choices": [choice]})
                    yield f"data: {event}\n\n".encode()
                if stall:
                    await asyncio.Event().wait()

        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=TimedStream(),
        ))
        async with httpx.AsyncClient(transport=transport) as client:
            with patch.dict(os.environ, {"ZHIYUAN_MODEL_BASE_URL": "https://chat.test/v1"}):
                observer = WebModelObserver(client, "test-model-key", "deepseek-chat")
            return await observer.complete([], max_tokens=1200, on_delta=lambda _: None, response_timeout=2)

    async def test_stream_progress_renews_idle_deadline(self):
        with patch("web_server.MODEL_FIRST_TOKEN_TIMEOUT", 0.14), patch("web_server.MODEL_STREAM_IDLE_TIMEOUT", 0.1):
            content, notice = await self.stream([
                (0.03, '{"answer":"', False),
                (0.05, "安装", False),
                (0.05, "步骤", False),
                (0.05, '"}', True),
            ])
        self.assertEqual((content, notice), ('{"answer":"安装步骤"}', None))

    async def test_completed_answer_survives_missing_stream_end(self):
        with patch("web_server.MODEL_STREAM_IDLE_TIMEOUT", 0.05), self.assertLogs("uvicorn.error", "WARNING"):
            result = await self.stream([(0.01, '{"answer":"安装步骤"}', False)])
        self.assertEqual(result, ('{"answer":"安装步骤"}', None))

    async def test_stalled_stream_has_distinct_phase_and_rejects_partial_answer(self):
        cases = [
            ([], "等待模型首段回复超时"),
            ([(0.01, '{"answer":"尚未完成', False)], "模型流式输出停顿超时"),
        ]
        with patch("web_server.MODEL_FIRST_TOKEN_TIMEOUT", 0.08), patch("web_server.MODEL_STREAM_IDLE_TIMEOUT", 0.05):
            for pieces, notice in cases:
                with self.subTest(notice=notice), self.assertLogs("uvicorn.error", "WARNING"):
                    self.assertEqual(await self.stream(pieces), (None, notice))


if __name__ == "__main__":
    unittest.main()
