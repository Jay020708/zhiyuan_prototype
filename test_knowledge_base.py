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


if __name__ == "__main__":
    unittest.main()
