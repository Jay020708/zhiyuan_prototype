import asyncio
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from web_server import COOKIE, create_web_app


class WebTest(unittest.TestCase):
    def test_web_demos_manual_turns_refresh_and_session_isolation(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "ZHIYUAN_EMBEDDING_BASE_URL": "", "ZHIYUAN_KNOWLEDGE_DB": os.path.join(directory, "web.sqlite3"),
        }), TestClient(create_web_app("")) as client:
            home = client.get("/")
            self.assertEqual(home.status_code, 200)
            self.assertIn("推荐问题", home.text)
            self.assertIn("default-src 'self'", home.headers["content-security-policy"])
            self.assertEqual(client.get("/app.js").status_code, 200)
            self.assertEqual(client.get("/styles.css").status_code, 200)
            self.assertEqual(client.get("/api/session").json()["turns"], [])

            api = client.post("/api/demo/api").json()["frames"]
            self.assertEqual(len(api), 7)
            self.assertIn("鉴权", api[4]["state"]["pending"])
            self.assertIn("401", api[4]["state"]["history"][0])
            self.assertEqual(api[-1]["state"]["status"], "resolved")
            first_cookie = client.cookies.get(COOKIE)
            self.assertEqual(len(client.get("/api/session").json()["turns"]), 7)

            image = client.post("/api/demo/image").json()["frames"]
            self.assertEqual(len(image), 8)
            self.assertIn("Dockerfile", image[2]["turn"]["assistant"])
            self.assertEqual(image[4]["state"]["pending"], "在新镜像中验证导入")
            self.assertEqual(len(image[-1]["state"]["history"]), 5)
            self.assertNotEqual(first_cookie, client.cookies.get(COOKIE))

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


if __name__ == "__main__":
    unittest.main()
