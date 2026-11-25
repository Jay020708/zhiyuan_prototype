import json
import os
import tempfile
import unittest
from contextlib import closing, contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import httpx
from fastapi.testclient import TestClient

from ingest_documents import ingest, load_materials
from web_server import create_web_app


FACT = "星舰联调窗口每周三 14:30 开放。"
FAILURE_TEXT = "仅失败文件含有的编号是 JUNO-000。"
PASSWORD = "rag-test-password"
AsyncClient = httpx.AsyncClient


class RagWebTest(unittest.TestCase):
    @contextmanager
    def app(self, root, limit=None, key="test-model-key", service_failures=None, embedding_outage=None):
        model_calls = []
        embedding_inputs = []

        def provider(request):
            payload = json.loads(request.content)
            if request.url.host == "embed.test":
                if embedding_outage and embedding_outage["active"]:
                    raise httpx.ConnectError("test outage", request=request)
                self.assertEqual(request.url.path, "/v1/embeddings")
                self.assertEqual(payload["model"], "BAAI/bge-m3")
                texts = payload["input"]
                if texts == ["知识库服务检查"] and service_failures and service_failures["embedding"]:
                    raise httpx.ConnectError("test outage", request=request)
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
            if messages[1]["content"] == "连接检查":
                if service_failures and service_failures["model"]:
                    return httpx.Response(401)
                return httpx.Response(200, json={"choices": [{"message": {"content": '{"status":"ok"}'}}]})
            content = json.loads(messages[1]["content"])
            if payload["max_tokens"] == 256:
                self.assertEqual(content["question"], "那校外呢？")
                self.assertIn("联调窗口", content["history"][-1]["user"])
                result = {"query": "校外如何使用星舰联调窗口？"}
            elif content["question"] == "那校外呢？":
                self.assertIn("联调窗口", content["history"][-1]["user"])
                self.assertIn(FACT, content["passages"][0]["text"])
                result = {"answer": "资料中的联调窗口每周三下午开放。", "evidence": [{"id": 1}]}
            elif "passages" not in content:
                self.assertIn(content["question"], ("你好", "JUNO 编号是多少？"))
                result = {"answer": "你好！有什么可以帮你？" if content["question"] == "你好" else "我不确定这个编号。"}
            elif content["question"] == "你好，今天可以聊聊联调以外的事吗？":
                result = {"answer": "当然可以，你想聊什么？", "evidence": [],
                          "follow_ups": ["可以聊聊怎样安排学习时间吗？"]}
            else:
                self.assertEqual(content["question"], "星舰联调窗口什么时候开放？")
                self.assertTrue(any(FACT in passage["text"] for passage in content["passages"]))
                result = {"answer": "联调窗口的开放时间是每周三下午 2:30。",
                          "evidence": [{"id": 1, "quote": FACT}],
                          "follow_ups": ["星舰联调窗口如何预约？", "星舰联调窗口如何预约？", 42]}
            answer = json.dumps(result, ensure_ascii=False)
            if payload["stream"]:
                updates = [json.dumps({"choices": [{"delta": {"content": piece}}]})
                           for piece in (answer[:25], answer[25:])]
                body = "".join(f"data: {event}\n\n" for event in [*updates, "[DONE]"])
                return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})
            return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})

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
            with TestClient(create_web_app(key)) as client:
                yield client, embedding_inputs, model_calls

    def upload(self, client, filename, data, media="text/plain", source_url=None, supersedes=None):
        headers = {
            "Content-Type": media, "X-Document-Filename": quote(filename),
        }
        if source_url is not None:
            headers["X-Document-Source-URL"] = source_url
        if supersedes is not None:
            headers["X-Document-Supersedes"] = supersedes
        return client.post("/api/admin/documents", content=data, headers=headers)

    def login(self, client):
        result = client.post("/api/admin/login", json={"password": PASSWORD})
        self.assertEqual(result.status_code, 200)
        self.assertIn("httponly", result.headers["set-cookie"].lower())

    def ask(self, client, message):
        result = client.post("/api/turn", json={"message": message})
        self.assertEqual(result.status_code, 200)
        return result.json()["turn"]

    def test_admin_only_service_checks_report_failure_and_recovery(self):
        failures = {"embedding": True, "model": True}
        with tempfile.TemporaryDirectory() as root, self.app(root, service_failures=failures) as (client, _, _):
            self.assertEqual(client.get("/api/admin/services").status_code, 403)
            self.login(client)
            failed = client.get("/api/admin/services").json()
            self.assertEqual(failed["embedding"]["status"], "error")
            self.assertIn("无法连接", failed["embedding"]["detail"])
            self.assertEqual(failed["model"]["status"], "error")
            self.assertIn("401", failed["model"]["detail"])
            self.assertEqual(failed["index"]["status"], "empty")

            failures.update(embedding=False, model=False)
            self.assertEqual(client.get("/api/admin/services").json(), failed)
            self.assertEqual(self.upload(client, "联调.txt", FACT.encode()).status_code, 202)
            client.app.state.service_status.checked = 0
            recovered = client.get("/api/admin/services").json()
            self.assertEqual([recovered[key]["status"] for key in ("embedding", "model", "index")],
                             ["ready", "ready", "ready"])

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
                source_url = "https://claw.sjtu.edu.cn/guide/sjtu-api/"
                response = self.upload(admin, "联调.txt", FACT.encode(), source_url=source_url)
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
                self.assertNotIn("[99]", turn["assistant"])
                self.assertEqual(turn["sources"][0]["filename"], "联调.txt")
                self.assertEqual(turn["sources"][0]["quote"], FACT)
                self.assertEqual(turn["sources"][0]["url"], source_url)
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

    def test_invalid_upload_failed_index_and_logout(self):
        with tempfile.TemporaryDirectory() as root, self.app(root, limit=128) as (admin, inputs, model_calls):
            self.login(admin)
            self.assertEqual(self.upload(
                admin, "fake.txt", FACT.encode(), source_url="https://claw.sjtu.edu.cn@example.org/").status_code, 422)
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

    def test_history_switch_context_restart_and_browser_ownership(self):
        with tempfile.TemporaryDirectory() as root:
            with self.app(root) as (browser, inputs, _):
                self.login(browser)
                self.assertEqual(self.upload(browser, "联调.txt", FACT.encode()).status_code, 202)
                first = browser.post("/api/conversations", json={}).json()["id"]
                original = self.ask(browser, "星舰联调窗口什么时候开放？")
                second = browser.post("/api/conversations", json={}).json()
                self.assertEqual(second["turns"], [])
                self.assertNotEqual(first, second["id"])
                self.assertEqual([item["id"] for item in second["conversations"]], [second["id"], first])
                selected = browser.post(f"/api/conversations/{first}/select", json={}).json()
                self.assertEqual(selected["turns"], [original])
                self.assertEqual(browser.get("/api/session").json()["id"], first)
                reply = browser.post("/api/turn", json={"conversation_id": first, "message": "那校外呢？"}).json()
                self.assertIn("校外如何使用星舰联调窗口？", inputs)
                self.assertEqual(reply["turn"]["sources"][0]["filename"], "联调.txt")
                self.assertEqual([item["id"] for item in reply["conversations"]], [first, second["id"]])
                cookies = dict(browser.cookies)

            with self.app(root) as (browser, _, _):
                for key, value in cookies.items():
                    browser.cookies.set(key, value)
                restored = browser.get("/api/session").json()
                self.assertEqual(restored["id"], first)
                self.assertEqual(restored["turns"], [original, reply["turn"]])
                self.assertEqual(browser.get(f"/api/conversations/{second['id']}").json()["turns"], [])
                with closing(TestClient(browser.app)) as other:
                    other.get("/api/session")
                    self.assertEqual(other.get("/api/conversations").json()["conversations"], [])
                    self.assertEqual(other.get(f"/api/conversations/{first}").status_code, 404)
                    self.assertEqual(other.post(f"/api/conversations/{first}/select", json={}).status_code, 404)
                    self.assertEqual(other.post("/api/turn", json={"conversation_id": first, "message": "你好"}).status_code, 404)

    def test_one_conversation_routes_questions_and_keeps_diagnostic_progress(self):
        with tempfile.TemporaryDirectory() as root, self.app(root, key="") as (client, inputs, model_calls):
            self.assertEqual(client.get("/api/session").json()["mode"], "auto")
            client.post("/api/demo/image").raise_for_status()
            self.assertEqual(self.ask(client, "星舰联调窗口什么时候开放？")["kind"], "knowledge")
            self.assertIn("校内", self.ask(client, "致远一号 API 连接超时了。")["assistant"])
            self.assertIn("VPN", self.ask(client, "在家里的个人电脑上")["assistant"])
            original = client.get("/api/session").json()
            interrupted = original["state"]
            other = client.post("/api/conversations", json={}).json()
            self.assertIsNone(other["state"])
            selected = client.post(f"/api/conversations/{original['id']}/select", json={}).json()
            self.assertEqual(selected["state"], interrupted)
            self.assertEqual(selected["turns"], original["turns"])
            self.assertEqual(self.ask(client, "怎么查我的 Token 用量？")["kind"], "knowledge")
            self.assertEqual(client.get("/api/session").json()["state"], interrupted)
            self.assertIn("/models", self.ask(client, "没有")["assistant"])
            self.assertIn("鉴权", self.ask(client, "连上 VPN 后返回 401")["assistant"])
            self.assertIn("最小对话", self.ask(client, "拿到模型列表了，接下来怎么验证？")["assistant"])
            self.assertIn("验证完成", self.ask(client, "最小对话请求成功，已收到模型回复")["assistant"])
            self.assertEqual(self.ask(client, "API Key 怎么申请？")["kind"], "knowledge")
            again = self.ask(client, "致远一号又遇到 API 连接超时。")
            self.assertIn("校内", again["assistant"])
            self.assertFalse(client.get("/api/session").json()["state"]["history"])
            image = self.ask(client, "ModuleNotFoundError: No module named 'numpy'")
            self.assertIn("numpy", image["assistant"])
            self.assertEqual(client.get("/api/session").json()["state"]["scenario"], "image")
            self.assertFalse(inputs)
            self.assertFalse(model_calls)

    def test_replacement_keeps_old_version_until_new_index_succeeds(self):
        outage = {"active": False}
        source = "https://claw.sjtu.edu.cn/guide/sjtu-api/"
        with tempfile.TemporaryDirectory() as root, self.app(root, embedding_outage=outage) as (admin, inputs, _):
            self.assertEqual(admin.post(f"/api/admin/documents/{'a' * 32}/retry").status_code, 403)
            self.login(admin)
            self.assertEqual(self.upload(admin, "联调.txt", FACT.encode(), supersedes="bad").status_code, 422)
            self.assertEqual(self.upload(admin, "联调.txt", FACT.encode(), supersedes="a" * 32).status_code, 404)

            first = self.upload(admin, "联调.txt", FACT.encode(), source_url=source).json()["document"]
            self.assertEqual(first["version"], 1)
            self.assertIsNone(first["supersedes"])
            documents = {doc["id"]: doc for doc in admin.get("/api/admin/documents").json()["documents"]}
            self.assertEqual(documents[first["id"]]["status"], "indexed")

            outage["active"] = True
            new_text = f"{FACT}\n新版补充步骤。"
            response = self.upload(admin, "联调.txt", new_text.encode(), supersedes=first["id"])
            self.assertEqual(response.status_code, 202)
            second = response.json()["document"]
            self.assertEqual(second["version"], 2)
            self.assertEqual(second["supersedes"], first["id"])
            self.assertEqual(second["source_url"], source)
            documents = {doc["id"]: doc for doc in admin.get("/api/admin/documents").json()["documents"]}
            self.assertEqual(documents[second["id"]]["status"], "failed")
            self.assertIn("无法连接", documents[second["id"]]["error"])
            self.assertEqual(documents[first["id"]]["status"], "indexed")
            self.assertIsNone(documents[first["id"]]["superseded_by"])

            outage["active"] = False

            # The old manual still answers until the replacement is indexed.
            turn = self.ask(admin, "星舰联调窗口什么时候开放？")
            self.assertEqual(turn["sources"][0]["filename"], "联调.txt")
            self.assertIn(FACT, turn["sources"][0]["quote"])

            retried = admin.post(f"/api/admin/documents/{second['id']}/retry")
            self.assertEqual(retried.status_code, 202)
            self.assertEqual(retried.json()["document"]["status"], "processing")
            documents = {doc["id"]: doc for doc in admin.get("/api/admin/documents").json()["documents"]}
            self.assertEqual(documents[second["id"]]["status"], "indexed")
            self.assertEqual(documents[second["id"]]["chunk_count"], 1)
            self.assertEqual(documents[first["id"]]["status"], "retired")
            self.assertEqual(documents[first["id"]]["superseded_by"], second["id"])
            self.assertEqual(documents[first["id"]]["superseded_by_version"], 2)

            # Retried and replaced versions cannot be replaced or retried again.
            self.assertEqual(admin.post(f"/api/admin/documents/{second['id']}/retry").status_code, 409)
            self.assertEqual(self.upload(admin, "联调.txt", b"another", supersedes=first["id"]).status_code, 409)

            # Only the new version remains searchable.
            turn = self.ask(admin, "星舰联调窗口什么时候开放？")
            self.assertEqual(turn["sources"][0]["filename"], "联调.txt")
            self.assertIn(FACT, turn["sources"][0]["quote"])

    def test_manifest_import_is_retryable_and_checks_existing_versions(self):
        source = "https://claw.sjtu.edu.cn/guide/sjtu-api/"
        with tempfile.TemporaryDirectory() as root, self.app(root) as (client, _, __):
            folder = Path(root)
            path = folder / "联调.txt"
            path.write_text(FACT, encoding="utf-8")
            (folder / "manifest.json").write_text(json.dumps({"sources": {path.name: source}}), encoding="utf-8")
            self.login(client)
            materials = load_materials(folder, [])
            with redirect_stdout(StringIO()):
                self.assertEqual(ingest(client, materials, 1), 0)
                self.assertEqual(ingest(client, materials, 1), 0)
                path.write_text("新版本", encoding="utf-8")
                self.assertEqual(ingest(client, materials, 1), 1)
            documents = client.get("/api/admin/documents").json()["documents"]
            self.assertEqual(len(documents), 1)
            self.assertEqual(documents[0]["source_url"], source)

            path.write_text(f"{FACT}\n修订后的步骤。", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(ingest(client, materials, 1, replace=True), 0)
                self.assertIn("v2", output.getvalue())
                self.assertEqual(ingest(client, materials, 1), 0)
            documents = client.get("/api/admin/documents").json()["documents"]
            self.assertEqual(len(documents), 2)
            versions = {doc["version"]: doc for doc in documents}
            self.assertEqual(versions[1]["status"], "retired")
            self.assertEqual(versions[2]["status"], "indexed")
            self.assertEqual(versions[1]["superseded_by"], versions[2]["id"])
