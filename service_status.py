"""Bounded startup and administrator probes for the local application."""

import asyncio
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone

from knowledge_base import KnowledgeBaseError
from model_observer import read_model_json


class ServiceStatus:
    def __init__(self, embeddings, observer, knowledge, *, embedding_error=None, model_error=None, index_error=None):
        self.embeddings = embeddings
        self.observer = observer
        self.knowledge = knowledge
        self.embedding_error = embedding_error
        self.model_error = model_error
        self.index_error = index_error
        self.lock = asyncio.Lock()
        self.latest = None
        self.checked = 0

    async def check(self):
        async with self.lock:
            if self.latest and time.monotonic() - self.checked < 15:
                return self.latest
            vector, model, index = await asyncio.gather(
                self.check_embeddings(), self.check_model(), self.check_index(),
            )
            self.latest = {"embedding": vector, "model": model, "index": index,
                           "checked_at": datetime.now(timezone.utc).isoformat()}
            self.checked = time.monotonic()
            return self.latest

    async def check_embeddings(self):
        hint = "确认向量服务在配置地址运行，并已加载项目指定的 BGE-M3 模型。"
        if self.embedding_error:
            return service("error", self.embedding_error, hint)
        if self.embeddings is None:
            return service("unconfigured", "尚未配置向量服务。", "设置 ZHIYUAN_EMBEDDING_BASE_URL 后重启。")
        try:
            async with asyncio.timeout(12):
                await self.embeddings.embed(["知识库服务检查"])
        except TimeoutError:
            return service("error", "向量检查超过 12 秒。", hint)
        except KnowledgeBaseError as exc:
            return service("error", str(exc), hint)
        return service("ready", "向量服务已返回有效的 1024 维向量。")

    async def check_model(self):
        hint = "检查模型地址和网络。若返回 401，请更新对应平台的 API Key 并重启。"
        if self.model_error:
            return service("error", self.model_error, hint)
        if self.observer is None:
            return service("unconfigured", "尚未配置生成模型密钥。", "设置 ZHIYUAN_MODEL_API_KEY 后重启。")
        try:
            async with asyncio.timeout(12):
                content, notice = await self.observer.complete([
                    {"role": "system", "content": '仅输出 JSON 对象 {"status":"ok"}。'},
                    {"role": "user", "content": "连接检查"},
                ], max_tokens=64)
            if notice:
                return service("error", notice, hint)
            if read_model_json(content) == {"status": "ok"}:
                return service("ready", "生成接口鉴权通过，已返回有效响应。")
        except TimeoutError:
            return service("error", "生成模型检查超过 12 秒。", hint)
        except ValueError:
            pass
        return service("error", "生成接口已响应，但返回格式不符合检查要求。", hint)

    async def check_index(self):
        if self.index_error:
            return service("error", self.index_error, "检查索引模型配置。保留现有数据库并优先使用原向量模型。")
        if self.knowledge is None:
            return service("unconfigured", "知识库索引未启用。", "配置向量服务后重启。")
        try:
            documents = await asyncio.to_thread(self.knowledge.list_documents)
        except sqlite3.Error:
            return service("error", "无法读取 SQLite 索引。", "检查数据库文件权限和磁盘空间。")
        counts = Counter(doc["status"] for doc in documents)
        if not counts["indexed"]:
            status, detail = "empty", "暂无可检索文档。"
        else:
            status, detail = "ready", f'{counts["indexed"]} 份文档可检索。'
        if counts["processing"] or counts["failed"] or counts["retired"]:
            detail += (f' 处理中 {counts["processing"]} 份，失败 {counts["failed"]} 份，'
                       f'已停用旧版本 {counts["retired"]} 份。')
        hint = "在文档记录中查看入库状态和失败原因。" if status == "empty" or counts["failed"] else None
        return service(status, detail, hint)


def service(status, detail, hint=None):
    return {"status": status, "detail": detail, "hint": hint}
