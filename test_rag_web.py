import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import httpx
from fastapi.testclient import TestClient

from web_server import create_web_app


FACT = "星舰联调窗口每周三 14:30 开放。"
FAILURE_TEXT = "仅失败文件含有的编号是 JUNO-000。"
PASSWORD = "rag-test-password"
AsyncClient = httpx.AsyncClient


class RagWebTest(unittest.TestCase):
    @contextmanager
    def app(self, root, limit=None):
        model_calls = []
        embedding_inputs = []

        def provider(request):
            payload = json.loads(request.content)
            if request.url.host == "embed.test":
                self.assertEqual(request.url.path, "/v1/embeddings")
                self.assertEqual(payload["model"], "BAAI/bge-m3")
                texts = payload["input"]
                embedding_inputs.extend(texts)
                if any(FAILURE_TEXT in text for text in texts):
                    return httpx.Response(503)
                return httpx.Response(200, json={"data": [
                    {"index": i, "embedding": [float("联调" in text), float("联调" not in text)] + [0.0] * 1022}
                    for i, text in enumerate(texts)
                ]})
            self.assertEqual(request.url.path, "/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer test-model-key")
            messages = payload["messages"]
            model_calls.append(messages)
            self.assertEqual(messages[0]["role"], "system")
            content = json.loads(messages[1]["content"])
            if "passages" not in content:
                self.assertIn(content["question"], ("你好", "JUNO 编号是多少？"))
                result = {"answer": "你好！有什么可以帮你？" if content["question"] == "你好" else "我不确定这个编号。"}
            elif content["question"] == "你好，今天可以聊聊联调以外的事吗？":
                result = {"answer": "当然可以，你想聊什么？", "evidence": [],
                          "follow_ups": ["可以聊聊怎样安排学习时间吗？"]}
            else:
                self.assertEqual(content["question"], "星舰联调窗口什么时候开放？")
                passage = next(passage for passage in content["passages"] if FACT in passage["text"])
                result = {"answer": "联调窗口的开放时间是每周三下午 2:30。 [1]",
                          "evidence": [{"id": passage["id"], "quote": FACT}],
                          "follow_ups": ["星舰联调窗口如何预约？", "星舰联调窗口如何预约？", 42]}
            answer = json.dumps(result, ensure_ascii=False)
            if payload["stream"]:
                updates = [json.dumps({"choices": [{"delta": {"content": piece}}]})
                           for piece in (answer[:25], answer[25:])]
                body = "".join(f"data: {event}\n\n" for event in [*updates, "[DONE]"])
                return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})
            return httpx.Response(200, json={"choices": [{"message": {
                "content": answer, "reasoning_content": "测试用的内部推理",
            }}]})

        def model_client(**kwargs):
            return AsyncClient(transport=httpx.MockTransport(provider), **kwargs)

        env = {
            "ZHIYUAN_ADMIN_PASSWORD": PASSWORD,
            "ZHIYUAN_EMBEDDING_BASE_URL": "https://embed.test/v1",
            "ZHIYUAN_KNOWLEDGE_DB": str(Path(root) / "knowledge.sqlite3"),
            "ZHIYUAN_MODEL_BASE_URL": "https://chat.test/v1",
            "ZHIYUAN_MODEL_NAME": "deepseek-chat",
        }
        if limit is not None:
            env["ZHIYUAN_UPLOAD_LIMIT_BYTES"] = str(limit)
        with patch.dict(os.environ, env), patch("web_server.httpx.AsyncClient", model_client):
            with TestClient(create_web_app("test-model-key")) as client:
                yield client, embedding_inputs, model_calls

    def upload(self, client, filename, data, media="text/plain"):
        return client.post("/api/admin/documents", content=data, headers={
            "Content-Type": media, "X-Document-Filename": quote(filename),
        })

    def login(self, client):
        result = client.post("/api/admin/login", json={"password": PASSWORD})
        self.assertEqual(result.status_code, 200)
        self.assertIn("httponly", result.headers["set-cookie"].lower())

    def ask(self, client, message):
        result = client.post("/api/turn", json={"message": message})
        self.assertEqual(result.status_code, 200)
        return result.json()["turn"]

    def test_public_question_after_admin_upload_survives_restart(self):
        with tempfile.TemporaryDirectory() as root:
            with self.app(root) as (admin, inputs, model_calls):
                self.assertEqual(admin.get("/admin").status_code, 200)
                self.assertNotIn('type="file"', admin.get("/").text)
                self.assertTrue(admin.get("/api/session").json()["knowledge_base_enabled"])
                self.assertEqual(self.upload(admin, "联调.txt", FACT.encode()).status_code, 403)
                self.assertEqual(admin.get("/api/admin/documents").status_code, 403)
                self.assertEqual(admin.post("/api/admin/login", json={"password": "wrong"}).status_code, 401)
                self.login(admin)
                response = self.upload(admin, "联调.txt", FACT.encode())
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json()["document"]["status"], "processing")
                document = admin.get("/api/admin/documents").json()["documents"][0]
                self.assertEqual(document["status"], "indexed")
                self.assertEqual(document["chunk_count"], 1)
                self.assertIn(FACT, inputs)
                self.assertFalse(model_calls)

            with self.app(root) as (public, inputs, model_calls):
                question = "星舰联调窗口什么时候开放？"
                turn = self.ask(public, question)
                self.assertEqual(turn["interpreter"], "knowledge")
                self.assertIn("每周三下午 2:30", turn["assistant"])
                self.assertIn("[1]", turn["assistant"])
                self.assertEqual(turn["sources"][0]["filename"], "联调.txt")
                self.assertEqual(turn["sources"][0]["quote"], FACT)
                self.assertIn("第 1 行", turn["sources"][0]["location"])
                self.assertEqual(turn["follow_ups"], ["星舰联调窗口如何预约？"])
                self.assertEqual(inputs, [question])
                self.assertEqual(len(model_calls), 1)
                self.assertEqual(public.get("/api/session").json()["turns"][0], turn)
                self.assertEqual(self.upload(public, "联调.txt", FACT.encode()).status_code, 403)
                greeting = public.post("/api/turn", json={"message": "你好"},
                                       headers={"Accept": "application/x-ndjson"})
                greeting_frames = [json.loads(line) for line in greeting.text.splitlines()]
                self.assertTrue(greeting_frames[1]["preview"])
                unrelated = greeting_frames[-1]["result"]["turn"]
                self.assertIn("你好！", unrelated["assistant"])
                self.assertEqual(unrelated["knowledge_status"], "general")
                self.assertEqual(inputs[-1], "你好")
                self.assertEqual(unrelated["sources"], [])
                self.assertIn("这个知识库可以回答哪些问题？", unrelated["follow_ups"])
                self.assertEqual(len(model_calls), 2)
                low_relevance = self.ask(public, "你好，今天可以聊聊联调以外的事吗？")
                self.assertEqual(low_relevance["knowledge_status"], "general")
                self.assertEqual(low_relevance["sources"], [])
                self.assertEqual(low_relevance["follow_ups"], ["可以聊聊怎样安排学习时间吗？"])
                self.assertEqual(len(model_calls), 3)
                streamed = public.post("/api/turn", json={"message": question},
                                       headers={"Accept": "application/x-ndjson"})
                self.assertTrue(streamed.headers["content-type"].startswith("application/x-ndjson"))
                frames = [json.loads(line) for line in streamed.text.splitlines()]
                self.assertEqual(frames[0], {"user": question})
                self.assertTrue(frames[1]["preview"])
                self.assertNotEqual(frames[1]["preview"], turn["assistant"])
                self.assertEqual(frames[-1]["result"]["turn"], turn)
                self.assertEqual(public.get("/api/session").json()["turns"][-1], turn)

                inputs.clear()
                model_calls.clear()
                preset = public.get("/faq.json").json()[0]
                preset_result = public.post("/api/turn?view=public", json={"message": preset["question"]})
                self.assertEqual(preset_result.json()["turn"]["assistant"], preset["answer"])
                self.assertEqual(inputs, [])
                self.assertEqual(model_calls, [])

                result = public.post("/api/turn?view=public", json={"message": question},
                                     headers={"Accept": "application/x-ndjson"})
                self.assertEqual(result.status_code, 200)
                self.assertTrue(result.headers["content-type"].startswith("application/json"))
                self.assertEqual(result.json(), {"turn": {
                    "user": question, "assistant": turn["assistant"],
                    "sources": [{"id": 1, "filename": "联调.txt", "location": turn["sources"][0]["location"]}],
                }})
                history = public.get("/api/session?view=public").json()
                self.assertEqual(history["turns"][-2:], [preset_result.json()["turn"], result.json()["turn"]])
                self.assertEqual(inputs, [question])
                self.assertEqual(len(model_calls), 1)

    def test_invalid_upload_failed_index_and_logout(self):
        with tempfile.TemporaryDirectory() as root, self.app(root, limit=128) as (admin, inputs, model_calls):
            self.login(admin)
            self.assertEqual(self.upload(admin, "note.exe", b"unsupported").status_code, 415)
            self.assertEqual(self.upload(admin, "note.txt", b"z" * 129).status_code, 413)
            self.assertEqual(self.upload(admin, "bad.txt", FAILURE_TEXT.encode()).status_code, 202)
            failed = admin.get("/api/admin/documents").json()["documents"][0]
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["chunk_count"], 0)
            self.assertIn("503", failed["error"])
            inputs.clear()
            self.assertEqual(admin.post("/api/admin/logout").status_code, 204)
            self.assertEqual(self.upload(admin, "联调.txt", FACT.encode()).status_code, 403)
            turn = self.ask(admin, "JUNO 编号是多少？")
            self.assertEqual(turn["assistant"], "我不确定这个编号。")
            self.assertEqual(turn["sources"], [])
            self.assertFalse(inputs)
            self.assertEqual(len(model_calls), 1)
