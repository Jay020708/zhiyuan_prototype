"""Start the local web app and, when configured, start Ollama on loopback."""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
PREVIOUS_MODEL_CACHE = Path("/private/tmp/zhiyuan-ollama-models")


def running_ollama(client, origin):
    try:
        response = client.get(f"{origin}/api/tags")
        return response.is_success and isinstance(response.json().get("models"), list)
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


def start_ollama(origin, environment):
    executable = shutil.which("ollama")
    if executable is None:
        raise RuntimeError("找不到 ollama 命令。请先安装 Ollama，或配置已运行的向量服务地址。")
    environment = {**environment, "OLLAMA_HOST": origin}
    if PREVIOUS_MODEL_CACHE.is_dir():
        environment.setdefault("OLLAMA_MODELS", str(PREVIOUS_MODEL_CACHE))
    return subprocess.Popen([executable, "serve"], cwd=ROOT, env=environment, start_new_session=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", interpolate=False)
    environment = os.environ.copy()
    address = urlsplit(environment.get("ZHIYUAN_EMBEDDING_BASE_URL", "").strip())
    local = address.scheme == "http" and address.hostname in {"127.0.0.1", "localhost", "::1"} and address.port == 11434
    child = None
    try:
        if local:
            origin = f"{address.scheme}://{address.netloc}"
            with httpx.Client(timeout=1, trust_env=False) as client:
                if not running_ollama(client, origin):
                    print("正在启动本机 Ollama…", flush=True)
                    child = start_ollama(origin, environment)
                    deadline = time.monotonic() + 20
                    while not running_ollama(client, origin):
                        if child.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("Ollama 未能就绪，请检查端口是否被占用或查看上方日志。")
                        time.sleep(0.25)
                print("本机 Ollama 已就绪。", flush=True)
        return subprocess.call([sys.executable, str(ROOT / "web_server.py"), "--port", str(args.port)],
                               cwd=ROOT, env=environment)
    except (RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
