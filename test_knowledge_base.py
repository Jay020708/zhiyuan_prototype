import asyncio
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

import httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from knowledge_base import BGEM3Embeddings, KnowledgeBase, KnowledgeBaseError


def vector(first=1, second=0):
    return [first, second] + [0] * 1022


class KnowledgeBaseTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "knowledge.sqlite3"

    async def test_index_persists_and_retrieves_relevant_lines(self):
        def embeddings(request):
            payload = json.loads(request.content)
            self.assertEqual(request.url.path, "/v1/embeddings")
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            self.assertEqual(payload["model"], "BAAI/bge-m3")
            data = [
                {"index": index, "embedding": vector(0, 1) if "开户" in text else vector()}
                for index, text in enumerate(payload["input"])
            ]
            return httpx.Response(200, json={"data": data})

        async with httpx.AsyncClient(transport=httpx.MockTransport(embeddings)) as client:
            model = BGEM3Embeddings(client, "https://embedding.example/v1/", "test-key")
            index = KnowledgeBase(self.path, model)
            index.initialize()
            doc = index.create_document("部署.md", ".md")
            await index.index_document(doc["id"], ".md", "\r\n# 部署\r\n使用校园网连接。\r\n".encode())
            other = index.create_document("资料.txt", "txt")
            await index.index_document(other["id"], "txt", "开户资料".encode())
            restarted = KnowledgeBase(self.path, model)
            restarted.initialize()

            documents = {doc["filename"]: doc for doc in restarted.list_documents()}
            self.assertEqual(documents["部署.md"]["status"], "indexed")
            self.assertEqual(documents["部署.md"]["chunk_count"], 1)
            hits = await restarted.search("怎样配置校园网？")
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["filename"], "部署.md")
            self.assertEqual(hits[0]["location"], "第 2–3 行")
            self.assertEqual(hits[0]["text"], "# 部署\n使用校园网连接。")

    async def test_ollama_model_alias_and_incompatible_index(self):
        def embeddings(request):
            self.assertEqual(request.url.path, "/v1/embeddings")
            self.assertEqual(json.loads(request.content)["model"], "bge-m3")
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": vector()}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(embeddings)) as client:
            local = BGEM3Embeddings(client, "http://127.0.0.1:11434/v1")
            index = KnowledgeBase(self.path, local)
            index.initialize()
            document = index.create_document("部署.txt", "txt")
            await index.index_document(document["id"], "txt", "使用校园网连接。".encode())
            self.assertEqual(len(await index.search("怎样连接？")), 1)
            self.assertEqual(BGEM3Embeddings(client, "http://embed:11434/v1", model="bge-m3").model, local.model)
            with self.assertRaisesRegex(ValueError, "重新导入"):
                KnowledgeBase(self.path, BGEM3Embeddings(client, "http://embed:8080/v1")).initialize()
            with self.assertRaisesRegex(ValueError, "纯 HTTP"):
                BGEM3Embeddings(client, "[http://localhost:11434/v1](http://localhost:11434/v1)")

    async def test_extracts_text_from_pdf_with_original_page_number(self):
        writer = PdfWriter()
        writer.add_blank_page(200, 200)
        page = writer.add_blank_page(200, 200)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/Standard"): font}),
        })
        contents = DecodedStreamObject()
        contents.set_data(b"BT /Standard 11 Tf 20 50 Td (Use the campus VPN.) Tj ET")
        page.replace_contents(contents)
        pdf = BytesIO()
        writer.write(pdf)

        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, json={"data": [{"index": 0, "embedding": vector()}]}
        ))
        async with httpx.AsyncClient(transport=transport) as client:
            model = BGEM3Embeddings(client, "https://embedding.example/v1")
            index = KnowledgeBase(self.path, model)
            index.initialize()
            doc = index.create_document("guide.pdf", "pdf")
            await index.index_document(doc["id"], "pdf", pdf.getvalue())
            [hit] = await index.search("怎么连接？")
            self.assertEqual(hit["location"], "第 2 页")
            self.assertEqual(hit["text"], "Use the campus VPN.")

    async def test_partial_failed_index_remains_invisible(self):
        first_response = asyncio.Event()
        second_response = asyncio.Event()
        calls = 0

        async def embeddings(request):
            nonlocal calls
            calls += 1
            payload = json.loads(request.content)
            if calls == 1:
                return httpx.Response(200, json={"data": [
                    {"index": index, "embedding": vector()} for index in range(len(payload["input"]))
                ]})
            first_response.set()
            await second_response.wait()
            return httpx.Response(503)

        async with httpx.AsyncClient(transport=httpx.MockTransport(embeddings)) as client:
            index = KnowledgeBase(self.path, BGEM3Embeddings(client, "https://embedding.example/v1"))
            index.initialize()
            doc = index.create_document("大文档.txt", "txt")
            task = asyncio.create_task(index.index_document(doc["id"], "txt", ("说明。\n" * 6000).encode()))
            try:
                await asyncio.wait_for(first_response.wait(), timeout=5)
                self.assertEqual(index.list_documents()[0]["status"], "processing")
                self.assertEqual(await asyncio.wait_for(index.search("说明"), timeout=2), [])
            finally:
                second_response.set()
                await task

            failed = index.list_documents()[0]
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["chunk_count"], 0)
            self.assertIn("503", failed["error"])
            self.assertEqual(await index.search("说明"), [])
            self.assertEqual(calls, 2)

    async def test_unfinished_document_is_marked_failed_at_startup(self):
        index = KnowledgeBase(self.path, None)
        index.initialize()
        index.create_document("未完成.txt", "txt")

        restarted = KnowledgeBase(self.path, None)
        restarted.initialize()
        self.assertEqual(restarted.list_documents()[0]["status"], "failed")
        self.assertEqual(await restarted.search("说明"), [])

    async def test_embedding_rejects_incomplete_or_invalid_vectors(self):
        for data in (
            [{"index": 0, "embedding": vector()}],
            [{"index": 0, "embedding": vector()}, {"index": 0, "embedding": vector()}],
            [{"index": 0, "embedding": vector()}, {"index": 1, "embedding": [1, 2]}],
            [{"index": 0, "embedding": vector()}, {"index": 1, "embedding": vector(0, 0)}],
        ):
            with self.subTest(data_length=len(data)):
                transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"data": data}))
                async with httpx.AsyncClient(transport=transport) as client:
                    model = BGEM3Embeddings(client, "https://embedding.example/v1")
                    with self.assertRaises(KnowledgeBaseError):
                        await model.embed(["第一段", "第二段"])

    async def test_replacement_retires_old_version_only_after_new_index_succeeds(self):
        def embeddings(request):
            payload = json.loads(request.content)
            return httpx.Response(200, json={"data": [
                {"index": index, "embedding": vector(0, 1) if "新版本" in text else vector()}
                for index, text in enumerate(payload["input"])
            ]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(embeddings)) as client:
            index = KnowledgeBase(self.path, BGEM3Embeddings(client, "https://embedding.example/v1"))
            index.initialize()
            old = index.create_document("手册.txt", "txt", contents="旧版本步骤".encode())
            await index.index_document(old["id"], "txt", "旧版本步骤".encode())
            self.assertEqual(index.list_documents()[0]["status"], "indexed")

            new = index.create_document("手册.txt", "txt", contents="新版本步骤".encode(),
                                        supersedes=old["id"])
            self.assertEqual(new["version"], 2)
            documents = {doc["id"]: doc for doc in index.list_documents()}
            self.assertEqual(documents[old["id"]]["status"], "indexed")
            self.assertEqual(documents[new["id"]]["status"], "processing")

            await index.index_document(new["id"], "txt", "新版本步骤".encode())
            documents = {doc["id"]: doc for doc in index.list_documents()}
            self.assertEqual(documents[old["id"]]["status"], "retired")
            self.assertEqual(documents[old["id"]]["superseded_by"], new["id"])
            self.assertEqual(documents[old["id"]]["superseded_by_version"], 2)
            self.assertEqual(documents[new["id"]]["status"], "indexed")
            self.assertEqual(documents[new["id"]]["supersedes"], old["id"])
            self.assertEqual(documents[new["id"]]["supersedes_version"], 1)

            hits = await index.search("新版本")
            self.assertEqual([hit["text"] for hit in hits], ["新版本步骤"])
            self.assertEqual(await index.search("旧版本"), [])

    async def test_failed_replacement_keeps_old_version_and_retry_uses_stored_file(self):
        outage = {"active": False}

        def embeddings(request):
            if outage["active"]:
                raise httpx.ConnectError("test outage", request=request)
            payload = json.loads(request.content)
            return httpx.Response(200, json={"data": [
                {"index": index, "embedding": vector(0, 1) if "新版本" in text else vector()}
                for index, text in enumerate(payload["input"])
            ]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(embeddings)) as client:
            index = KnowledgeBase(self.path, BGEM3Embeddings(client, "https://embedding.example/v1"))
            index.initialize()
            old = index.create_document("手册.txt", "txt", contents="旧版本步骤".encode())
            await index.index_document(old["id"], "txt", "旧版本步骤".encode())

            outage["active"] = True
            new = index.create_document("手册.txt", "txt", contents="新版本步骤".encode(),
                                        supersedes=old["id"])
            await index.index_document(new["id"], "txt", "新版本步骤".encode())
            documents = {doc["id"]: doc for doc in index.list_documents()}
            self.assertEqual(documents[old["id"]]["status"], "indexed")
            self.assertIsNone(documents[old["id"]]["superseded_by"])
            self.assertEqual(documents[new["id"]]["status"], "failed")

            outage["active"] = False
            self.assertEqual([hit["text"] for hit in await index.search("旧版本")], ["旧版本步骤"])

            file_type, contents = index.prepare_retry(new["id"])
            self.assertEqual((file_type, contents), ("txt", "新版本步骤".encode()))
            self.assertEqual(index.get_document(new["id"])["status"], "processing")
            await index.index_document(new["id"], file_type, contents)
            documents = {doc["id"]: doc for doc in index.list_documents()}
            self.assertEqual(documents[old["id"]]["status"], "retired")
            self.assertEqual(documents[new["id"]]["status"], "indexed")
            self.assertEqual(await index.search("旧版本"), [])
            self.assertEqual([hit["text"] for hit in await index.search("新版本")], ["新版本步骤"])

            with self.assertRaisesRegex(KnowledgeBaseError, "只有入库失败"):
                index.prepare_retry(new["id"])
            with self.assertRaisesRegex(KnowledgeBaseError, "已被新版本替代"):
                index.prepare_retry(old["id"])

    async def test_successful_replacement_after_failed_attempt_links_the_whole_chain(self):
        outage = {"active": False}

        def embeddings(request):
            if outage["active"]:
                raise httpx.ConnectError("test outage", request=request)
            payload = json.loads(request.content)
            return httpx.Response(200, json={"data": [
                {"index": index, "embedding": vector(0, 1) if "新版本" in text else vector()}
                for index, text in enumerate(payload["input"])
            ]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(embeddings)) as client:
            index = KnowledgeBase(self.path, BGEM3Embeddings(client, "https://embedding.example/v1"))
            index.initialize()
            first = index.create_document("手册.txt", "txt", contents="旧版本步骤".encode())
            await index.index_document(first["id"], "txt", "旧版本步骤".encode())

            outage["active"] = True
            failed = index.create_document("手册.txt", "txt", contents="失败版本".encode(),
                                           supersedes=first["id"])
            await index.index_document(failed["id"], "txt", "失败版本".encode())

            outage["active"] = False
            third = index.create_document("手册.txt", "txt", contents="新版本步骤".encode(),
                                          supersedes=failed["id"])
            self.assertEqual(third["version"], 3)
            await index.index_document(third["id"], "txt", "新版本步骤".encode())

            documents = {doc["id"]: doc for doc in index.list_documents()}
            self.assertEqual(documents[first["id"]]["status"], "retired")
            self.assertEqual(documents[first["id"]]["superseded_by"], third["id"])
            self.assertEqual(documents[failed["id"]]["status"], "failed")
            self.assertEqual(documents[failed["id"]]["superseded_by"], third["id"])
            self.assertEqual(documents[third["id"]]["status"], "indexed")
            self.assertEqual(await index.search("旧版本"), [])
            with self.assertRaisesRegex(KnowledgeBaseError, "已被新版本替代"):
                index.prepare_retry(failed["id"])

    async def test_version_numbers_skip_failed_attempts_in_the_same_family(self):
        index = KnowledgeBase(self.path, None)
        index.initialize()
        first = index.create_document("手册.txt", "txt")
        second = index.create_document("手册.txt", "txt", supersedes=first["id"])
        retry = index.create_document("手册.txt", "txt", supersedes=first["id"])
        third = index.create_document("手册.txt", "txt", supersedes=second["id"])
        self.assertEqual((first["version"], second["version"], retry["version"], third["version"]),
                         (1, 2, 3, 4))

    async def test_retry_requires_a_stored_upload(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(503))
        async with httpx.AsyncClient(transport=transport) as client:
            index = KnowledgeBase(self.path, BGEM3Embeddings(client, "https://embedding.example/v1"))
            index.initialize()
            document = index.create_document("旧记录.txt", "txt")
            await index.index_document(document["id"], "txt", "说明".encode())
            self.assertEqual(index.list_documents()[0]["status"], "failed")
            with self.assertRaisesRegex(KnowledgeBaseError, "原始内容未保留"):
                index.prepare_retry(document["id"])

    async def test_initialize_migrates_documents_without_version_columns(self):
        import sqlite3

        with sqlite3.connect(self.path) as db:
            db.execute("""
                CREATE TABLE documents (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, file_type TEXT NOT NULL,
                    status TEXT NOT NULL, error TEXT, chunk_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)
            db.execute("INSERT INTO documents VALUES (?, '旧手册.txt', 'txt', 'indexed', NULL, 1, ?)",
                       ("a" * 32, "2024-01-01T00:00:00+00:00"))

        index = KnowledgeBase(self.path, None)
        index.initialize()
        document = index.list_documents()[0]
        self.assertEqual(document["version"], 1)
        self.assertIsNone(document["supersedes"])
        self.assertIsNone(document["superseded_by"])
        self.assertIsNone(document["source_url"])


if __name__ == "__main__":
    unittest.main()
