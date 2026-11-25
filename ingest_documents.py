"""Import a prepared document manifest through the knowledge-base admin API."""

import argparse
import json
import os
import time
from getpass import getpass
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from knowledge_base import official_source


CONTENT_TYPES = {".txt": "text/plain", ".md": "text/markdown", ".pdf": "application/pdf"}


def load_materials(folder, selected):
    sources = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["sources"]
    if not isinstance(sources, dict) or not sources:
        raise ValueError("manifest.json 没有待导入的 sources。")
    if unknown := set(selected) - sources.keys():
        raise ValueError(f"清单内没有指定文件：{', '.join(sorted(unknown))}")
    materials = []
    for name in dict.fromkeys(selected or sources):
        path = folder / name
        url = sources[name]
        if (path.name != name or path.suffix.lower() not in CONTENT_TYPES or not path.is_file()
                or not isinstance(url, str) or not official_source(url)):
            raise ValueError(f"清单项缺少有效的材料文件或官方链接：{name}")
        materials.append((path, url))
    return materials


def request(client, method, path, **kwargs):
    response = client.request(method, f"api/admin/{path}", **kwargs)
    if not response.is_success:
        raise RuntimeError(f"{path} 返回 HTTP {response.status_code}。")
    return response.json()


def wait_for_index(client, document_id, wait_seconds):
    deadline = time.monotonic() + wait_seconds
    while True:
        documents = request(client, "GET", "documents")["documents"]
        row = next((doc for doc in documents if doc["id"] == document_id), None)
        if row is None:
            raise RuntimeError("服务未返回该文档的索引记录。")
        if row["status"] != "processing":
            return row
        if time.monotonic() >= deadline:
            raise RuntimeError("等待索引超时，稍后可用同一命令继续检查。")
        time.sleep(1)


def ingest(client, materials, wait_seconds, replace=False):
    failed = []
    for path, url in materials:
        name = path.name
        try:
            contents = path.read_bytes()
            checksum = sha256(contents).hexdigest()
            documents = request(client, "GET", "documents")["documents"]
            same_name = [doc for doc in documents if doc["filename"] == name and doc["superseded_by"] is None]
            active = [doc for doc in same_name if doc["status"] in {"indexed", "processing"}]
            row = next((doc for doc in active if doc["sha256"] == checksum and doc["source_url"] == url), None)
            if row:
                row = wait_for_index(client, row["id"], wait_seconds) if row["status"] == "processing" else row
            elif active and not replace:
                raise RuntimeError("已有同名索引，内容或来源不同。请核对文档版本，或使用 --replace 上传为新版本。")
            else:
                candidates = active or (same_name if replace else [])
                target = max(candidates, key=lambda doc: doc["version"], default=None)
                headers = {
                    "Content-Type": CONTENT_TYPES[path.suffix.lower()],
                    "X-Document-Filename": quote(name),
                    "X-Document-Source-URL": url,
                }
                if target:
                    headers["X-Document-Supersedes"] = target["id"]
                row = request(client, "POST", "documents", content=contents,
                              headers=headers)["document"]
                row = wait_for_index(client, row["id"], wait_seconds)
            if row["status"] != "indexed" or not row["chunk_count"]:
                raise RuntimeError(row["error"] or "索引未生成可检索片段。")
            version = f"v{row['version']}" if row.get("version") else "已索引"
            print(f"{name} · {version} · 已索引 {row['chunk_count']} 个片段", flush=True)
        except (OSError, RuntimeError) as error:
            failed.append(name)
            print(f"{name} · {error}", flush=True)
    print(f"导入检查完成，{len(materials) - len(failed)}/{len(materials)} 份文档可检索。")
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("materials", type=Path, help="包含 manifest.json 的待入库目录")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765", help="运行中的应用地址")
    parser.add_argument("--only", action="append", default=[], metavar="FILENAME", help="只导入清单中的指定文件，可重复")
    parser.add_argument("--replace", action="store_true",
                        help="同名文档内容不同时上传为新版本，新版本入库成功后再停用旧版")
    parser.add_argument("--wait-seconds", type=int, default=600, help="每份文档最长等待秒数")
    args = parser.parse_args()
    try:
        address = urlsplit(args.base_url)
        loopback = address.hostname in {"127.0.0.1", "localhost", "::1"}
        if (not address.hostname or address.query or address.fragment or address.username is not None
                or not (address.scheme == "https" or loopback and address.scheme == "http")):
            raise ValueError("应用地址须使用 HTTPS，本机回环地址可使用 HTTP。")
        if args.wait_seconds <= 0:
            raise ValueError("--wait-seconds 必须大于零。")
        materials = load_materials(args.materials, args.only)
        password = os.environ.get("ZHIYUAN_ADMIN_PASSWORD") or getpass("管理员密码（输入隐藏）：")
        if not password:
            raise ValueError("请输入管理员密码。")
        with httpx.Client(base_url=args.base_url, timeout=30, trust_env=False) as client:
            request(client, "POST", "login", json={"password": password})
            return ingest(client, materials, args.wait_seconds, replace=args.replace)
    except (OSError, ValueError, RuntimeError, httpx.HTTPError, KeyError) as error:
        parser.exit(1, f"无法导入：{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
