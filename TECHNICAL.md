# 致远一号使用问答助手技术文档

## 应用形态

网页采用同源部署。FastAPI 提供静态 HTML、CSS、JavaScript 和会话 API。首页展示推荐问题和对话记录，固定答复维护在 `static/faq.json`。OpenJiuwen `WorkflowAgent` 继续处理命令行和案例接口中的两类排障。

自由提问时，后端在脱敏后检索资料并调用致远一号生成模型。首页使用 `view=public` 获取最终答复和参考文件位置。推荐问题直接返回固定答复，并与自由提问一起保存在当前会话。

## 安装和本机演示

使用 Python 3.11 至 3.13，以下命令使用 Python 3.12。进入项目根目录执行。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python web_server.py --live
```

按提示隐藏输入比赛 API Key，在浏览器打开 [问答首页](http://127.0.0.1:8765/)。仅体验推荐问题时可直接运行 `web_server.py`。也可在服务启动前由平台密钥管理注入 `ZHIYUAN_MODEL_API_KEY`，用 `ZHIYUAN_MODEL_NAME` 选择授权模型，默认 `deepseek-chat`。模型访问仍受校方网络要求约束。 [致远一号 API 文档](https://claw.sjtu.edu.cn/guide/sjtu-api/)

## 知识库部署与验收

应用通过 OpenAI 兼容的 `POST /v1/embeddings` 调用 BGE-M3。已有服务时，确认它加载的是 `BAAI/bge-m3`。使用 Hugging Face Text Embeddings Inference 新开服务时，可以在 ARM64 Docker 主机上运行以下命令。在 x86_64 主机上将镜像标签改为 `cpu-1.9`。等服务启动并加载权重后再上传。 [BGE-M3 模型卡](https://huggingface.co/BAAI/bge-m3) · [TEI 部署文档](https://huggingface.co/docs/text-embeddings-inference/supported_models) · [兼容接口](https://github.com/huggingface/text-embeddings-inference)

```bash
docker run --rm -p 127.0.0.1:8080:80 -v zhiyuan-bge-cache:/data \
  ghcr.io/huggingface/text-embeddings-inference:cpu-arm64-1.9 --model-id BAAI/bge-m3
```

安装应用依赖后，在项目目录设置以下环境变量并启动。管理员密码和生成模型密钥可以从部署平台注入。本机也可以用 `web_server.py --live` 隐藏输入生成模型密钥。

| 环境变量 | 用途 |
| --- | --- |
| `ZHIYUAN_EMBEDDING_BASE_URL` | 设置为包含 `/v1` 的向量服务地址，例如 `http://127.0.0.1:8080/v1`。设置后首页启用知识库问答。 |
| `ZHIYUAN_EMBEDDING_API_KEY` | 向量服务需要 Bearer 认证时设置。 |
| `ZHIYUAN_MODEL_API_KEY` | 致远一号生成模型密钥。正式回答需要此密钥。 |
| `ZHIYUAN_MODEL_NAME` | 原有生成模型名称，默认 `deepseek-chat`。 |
| `ZHIYUAN_MODEL_BASE_URL` | 生成模型兼容接口的基址，默认 `https://models.sjtu.edu.cn/api/v1`。 |
| `ZHIYUAN_ADMIN_PASSWORD` | 管理后台的登录密码。设置非空强密码后启用管理员登录。 |
| `ZHIYUAN_KNOWLEDGE_DB` | SQLite 路径，默认 `data/knowledge.sqlite3`。 |
| `ZHIYUAN_UPLOAD_LIMIT_BYTES` | 每份文件的字节数上限，默认 `52428800`（50 MiB）。 |

```bash
export ZHIYUAN_EMBEDDING_BASE_URL=http://127.0.0.1:8080/v1
.venv/bin/python web_server.py
```

本机可以在项目的 `.env` 中一次性配置管理员密码、向量服务地址和生成模型密钥。使用 `web_server.py` 启动时会自动读取，终端里已设置的环境变量优先。Git 已忽略 `.env`。

应用容器运行一个 worker。持久化部署时将可写卷挂载到 `/app/data`，并将向量服务地址设为容器可达的主机名或地址。管理员上传后立即收到处理状态，后台同一时间处理一份文档。服务重启时，中断的任务会显示失败，可重新上传。SQLite 保存脱敏片段、向量、文件名、原文位置和处理记录，上传文件在处理结束后释放。检索使用余弦相似度，返回分数达到 0.45 的前 5 个片段。每份文档最多 4096 个片段。PDF 需要文本层，扫描件可先做文字识别。

1. 创建 UTF-8 文件 `联调说明.txt`，正文写「星舰联调窗口每周三 14:30 开放。」。在 [管理后台](http://127.0.0.1:8765/admin) 登录并上传，等待列表显示索引成功。
2. 在另一个浏览器会话打开 [问答首页](http://127.0.0.1:8765/)，问「星舰联调窗口什么时候开放？」。确认回答含「每周三 14:30」，来源显示 `联调说明.txt` 和第 1 行。接着发送「你好」，确认页面正常回复，且该回复没有文档引用。
3. 确认首页没有文件输入控件。用以下命令直接请求上传接口，预期 HTTP 403。

   ```bash
   curl -i http://127.0.0.1:8765/api/admin/documents \
     -H 'X-Document-Filename: test.txt' -H 'Content-Type: text/plain' \
     --data-binary 'unauthenticated upload'
   ```

4. 在管理员后台上传只有空白字符的 TXT，确认列表显示处理失败、原因是缺少可索引正文，且片段数为 0。也可暂时停止向量服务，上传一份含独有事实的 TXT，等待列表显示向量服务失败并恢复服务。再询问该事实，确认失败文件没有来源结果。自动测试还模拟了先成功向量化一批、随后一批失败的情况。

运行 `.venv/bin/python -m unittest -v` 可离线复现上述服务端流程。测试用模拟 HTTP 服务验证文档入库和查询均调用 BGE-M3 接口，并验证生成模型的引文、登录权限和失败文件的检索隔离。

## 接口与会话

| 接口 | 用途 |
| --- | --- |
| `GET /` | 返回网页和前端资源。 |
| `GET /api/session` | 读取当前浏览器的会话。首页加 `?view=public` 获取简化后的对话历史。 |
| `POST /api/demo/api` | 新建一条会话，运行 API 超时案例，返回逐轮回放帧。 |
| `POST /api/demo/image` | 新建一条会话，运行镜像缺依赖案例，返回逐轮回放帧。 |
| `POST /api/turn` | 接收 `{"message":"脱敏问题"}`。首页加 `?view=public` 获取该轮问题、最终回答与参考文件位置。 |
| `POST /api/reset` | 新建手动对话会话。 |
| `GET /api/health` | 检查 HTTP 服务，并报告是否配置校内模型密钥。 |
| `GET /admin` | 打开独立管理员后台。 |
| `GET /api/admin/session` | 查看管理员会话和上传大小限制。 |
| `POST /api/admin/login`、`POST /api/admin/logout` | 使用配置的密码登录，或注销管理员会话。 |
| `GET /api/admin/documents` | 查看文档处理状态及失败原因，需要管理员登录。 |
| `POST /api/admin/documents` | 提交文件字节和 URL 编码的 `X-Document-Filename`，需要管理员登录。 |

会话 ID 通过 HttpOnly、SameSite Strict Cookie 保持隔离，HTTPS 下设置 Secure。聊天记录和诊断状态保存在服务端内存。新会话替换当前会话，服务重启会清空会话。原型每进程容纳至多 128 个活跃会话，每个会话最多 60 轮，空闲会话约一小时后在下次请求时回收。模型密钥仅由后端使用，页面及接口响应均不包含密钥。生成模型的调用限制为每进程每分钟 20 次，两套回放均不占该额度。知识库提问限制为每进程每分钟 60 次。

## 校内部署及交我办接入

1. 在组委会允许的校内环境或能访问致远一号模型的服务主机部署。通过服务端密钥配置注入授权，确认主机能调用校内模型。直接运行 Python 或从根目录的 `Dockerfile` 构建部署镜像。
2. 以一个 Uvicorn worker 启动，保持内存会话和模型请求统计一致。在校方认可的入口终止 HTTPS，按校方要求配置访问控制。若通过网关接入，转发浏览器使用的 Host 和协议。在子路径挂载时使用带尾部斜杠的入口，让静态资源和 API 都沿同一前缀请求。
3. 大赛要求部署到交我办智能体大赛专区。交大公开开发者平台说明，接入其他业务系统需按界面、接口规范提交申请。赛道公开 FAQ 对专区内嵌形式、认证以及提交入口未给出细则。实际接入需要赛事群确认的接入方式、团队发布权限和学校提供或认可的访问地址。若接受 H5 链接登记，提交 HTTPS 地址。若要求原生智能体接口或 jAccount，使用校方提供的协议完成适配。 [大赛公告](https://apps.hpc.sjtu.edu.cn/claw/competition) · [交大开发者平台](https://developer.sjtu.edu.cn/form/introduce/) · [赛事 FAQ](https://claw.sjtu.edu.cn/news/competition/qa/)
4. 上线后用至少一个队外测试账号从「交我办」专区实际进入。验证校内与校外允许访问方式、移动端显示、真实模型对话、会话隔离、错误回退与两套演示。`/api/health` 的 `model_configured` 只检查配置，真实连通性以手动对话为准。

不使用容器时，可以从项目目录启动服务，然后由本机网关代理。

```bash
.venv/bin/python -m uvicorn web_server:app --host 127.0.0.1 --port 8765 --workers 1 --no-access-log
```

## 答辩视频脚本

| 建议时段 | 画面与讲解 |
| --- | --- |
| 0:00–0:12 | 打开首页，展示三个推荐问题。 |
| 0:12–0:32 | 点击镜像问题，展示云主机和 ModelArts 的不同入口与操作说明。 |
| 0:32–0:58 | 输入推荐问题以外的报错，展示最终答复和参考资料。 |
| 0:58–1:20 | 运行命令行排障演示，展示 API 重试后由超时切换到鉴权。 |
| 1:20–1:30 | 上线后录制从交我办大赛专区进入并完成至少一轮真实模型问答。 |
