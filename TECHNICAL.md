# 致远一号使用问答助手技术文档

## 应用形态

网页采用同源部署。FastAPI 提供静态 HTML、CSS、JavaScript 和会话 API，OpenJiuwen `WorkflowAgent` 处理两类排障。一个 Python 进程即可运行这一版，浏览器不需要安装客户端。桌面浏览器把对话和排查状态并排显示，窄屏按对话、状态的顺序排列。

网页使用同一个会话处理文档查询和排障。后端识别支持的故障类型，把脱敏后的报错或排查反馈交给 OpenJiuwen 工作流。致远一号模型提取事实，程序校验字段并根据上轮状态选择下一步。提取失败或等待超过 12 秒时，后端回退到规则并在接口返回原因。排查中插问文档会运行知识库检索，排查状态保留在侧栏。检索命中时，生成模型调用超时或接口失败会直接展示相关原文，状态为 `retrieved`。案例回放运行独立的示例工作流，用户可以随时返回自己的对话。

知识库生成的流式请求最多等待首段正文 45 秒。收到正文后，每段新内容将空闲期限延后 20 秒，每次生成调用最多 90 秒。上游流在正文已完整到达后停顿时，服务返回这份回复。日志记录超时阶段、模型名和耗时。用于补全指代的检索改写最多等待 8 秒。

## 安装和本机演示

使用 Python 3.11 至 3.13，以下命令使用 Python 3.12。进入项目根目录执行。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python web_server.py --live
```

按提示隐藏输入比赛 API Key，在浏览器打开 [本机演示](http://127.0.0.1:8765/)。省略 `--live` 即可使用本地规则。也可在服务启动前由平台密钥管理注入 `ZHIYUAN_MODEL_API_KEY`，用 `ZHIYUAN_MODEL_NAME` 选择授权模型，默认 `deepseek-chat`。模型访问仍受校方网络要求约束。 [致远一号 API 文档](https://claw.sjtu.edu.cn/guide/sjtu-api/)

## 知识库部署与验收

应用通过 OpenAI 兼容的 `POST /v1/embeddings` 调用 BGE-M3。

### Ollama 本机演示

完成 `.env` 和 BGE-M3 配置后，在项目目录运行 `.venv/bin/python run_local.py` 即可启动网页。向量地址为本机 Ollama 的 11434 端口时，命令会启动 Ollama 或复用已有进程。远程向量服务由部署方管理。用 `Ctrl+C` 退出时，命令会关闭它启动的服务。网页启动会在终端显示检查结果，也可登录管理后台，在「服务状态」查看向量、生成模型和 SQLite 索引状态。

先运行 Ollama。若使用 CLI 服务，在一个终端保持 `ollama serve` 运行。随后执行 `ollama pull bge-m3`，并在应用目录的 zsh 终端运行下面的命令。准备好管理员密码和赛事 API Key，程序会分别隐藏输入。 [Ollama BGE-M3](https://ollama.com/library/bge-m3) · [Ollama 兼容接口](https://docs.ollama.com/api/openai-compatibility)

```zsh
read -rs 'ZHIYUAN_ADMIN_PASSWORD?设置管理员密码: ' &&
export ZHIYUAN_ADMIN_PASSWORD &&
export ZHIYUAN_EMBEDDING_BASE_URL='http://127.0.0.1:11434/v1' &&
echo &&
.venv/bin/python web_server.py --live
```

应用将本机回环地址的 11434 端口默认识别为 Ollama，使用模型名 `bge-m3`。从容器、其他端口或远程地址访问 Ollama 时，额外设置 `ZHIYUAN_EMBEDDING_MODEL=bge-m3`。已有 TEI 索引切换到 Ollama 时，配置另一个 `ZHIYUAN_KNOWLEDGE_DB` 路径再导入文档。

### TEI 部署

已有服务时，确认它加载的是 `BAAI/bge-m3`。使用 Hugging Face Text Embeddings Inference 新开服务时，可以在 ARM64 Docker 主机上运行以下命令。在 x86_64 主机上将镜像标签改为 `cpu-1.9`。等服务启动并加载权重后再上传。 [BGE-M3 模型卡](https://huggingface.co/BAAI/bge-m3) · [TEI 部署文档](https://huggingface.co/docs/text-embeddings-inference/supported_models)

```bash
docker run --rm -p 127.0.0.1:8080:80 -v zhiyuan-bge-cache:/data \
  ghcr.io/huggingface/text-embeddings-inference:cpu-arm64-1.9 --model-id BAAI/bge-m3
```

安装应用依赖后，在项目目录设置以下环境变量并启动。管理员密码和生成模型密钥可以从部署平台注入。本机也可以用 `web_server.py --live` 隐藏输入生成模型密钥。

| 环境变量 | 用途 |
| --- | --- |
| `ZHIYUAN_EMBEDDING_BASE_URL` | 设置为包含 `/v1` 的向量服务地址，例如 `http://127.0.0.1:8080/v1`。设置后首页启用知识库问答。 |
| `ZHIYUAN_EMBEDDING_MODEL` | 向量服务使用的 BGE-M3 模型名。本机 11434 端口默认 `bge-m3`，其他地址默认 `BAAI/bge-m3`。 |
| `ZHIYUAN_EMBEDDING_API_KEY` | 向量服务需要 Bearer 认证时设置。 |
| `ZHIYUAN_MODEL_API_KEY` | 致远一号生成模型密钥。正式回答需要此密钥。 |
| `ZHIYUAN_MODEL_NAME` | 致远一号默认 `deepseek-chat`。使用 DeepSeek 官方 API 时可设为 `deepseek-flash` 或 `deepseek-v4-pro`。 |
| `ZHIYUAN_MODEL_BASE_URL` | 生成模型接口基址，默认 `https://models.sjtu.edu.cn/api/v1`。切换 DeepSeek 官方 API 时设为 `https://api.deepseek.com` 并使用该平台签发的 API Key。 |
| `ZHIYUAN_ADMIN_PASSWORD` | 管理后台的登录密码。设置非空强密码后启用管理员登录。 |
| `ZHIYUAN_KNOWLEDGE_DB` | 知识库和网页对话共用的 SQLite 路径，默认 `data/knowledge.sqlite3`。 |
| `ZHIYUAN_UPLOAD_LIMIT_BYTES` | 每份文件的字节数上限，默认 `52428800`（50 MiB）。 |

```bash
export ZHIYUAN_EMBEDDING_BASE_URL=http://127.0.0.1:8080/v1
.venv/bin/python web_server.py
```

本机可以在项目的 `.env` 中一次性配置管理员密码、向量服务地址和生成模型密钥。使用 `web_server.py` 启动时会自动读取，终端里已设置的环境变量优先。Git 已忽略 `.env`。

应用容器运行一个 worker。持久化部署时将可写卷挂载到 `/app/data`，并将向量服务地址设为容器可达的主机名或地址。管理员上传后立即收到处理状态，后台同一时间处理一份文档。服务重启时，中断的任务会显示失败，可重新索引或重新上传。SQLite 保存脱敏片段、向量、文件名、原文位置、处理记录和原始上传文件，原始文件用于失败后的重新索引。上传同名文档时可用「替换为新版本」建立版本链：新版本入库成功前，旧版继续参与检索；新版本成功后旧版标记为已停用，检索只使用当前版本。替换内容失败时旧版保持可用，恢复服务后可在文档记录中重新索引。检索使用余弦相似度，返回分数达到 0.45 的前 5 个片段。每份文档最多 4096 个片段。PDF 需要文本层，扫描件可先做文字识别。

### 批量导入与验收

应用启动后，在另一个终端运行下方命令。材料目录中的 `manifest.json` 使用 `sources` 字段映射文件名到官方 HTTPS 原文地址。导入程序复用管理员登录、上传及索引接口，逐份验证完成状态。再次执行时，文件名、原文地址和 SHA-256 都相同且已成功索引的文件会复用既有记录。同名文件内容更新时，加 `--replace` 会把新内容上传为新版本，待新版本入库成功后再停用旧版；不加该参数则报告同名索引冲突，保留原记录。

```bash
.venv/bin/python ingest_documents.py ../zhiyuan_rag_materials \
  --only '01-01 本地大模型 API 使用文档.md' \
  --only '02-08 交大 VPN 官方指南.md'
```

去掉 `--only` 可处理清单全部文档。材料更新后运行 `.venv/bin/python ingest_documents.py ../zhiyuan_rag_materials --replace`，即可逐份建立新版本。先问「致远一号的 API Key 怎么申请？」，核对回答是否符合官方资料。接着在同一会话发「我在校外调用 API 超时了」，回复设备的 VPN 状态和重试结果，核对侧栏。播放一次示例后返回对话，检查刚才的真实排查状态。

1. 创建 UTF-8 文件 `联调说明.txt`，正文写「星舰联调窗口每周三 14:30 开放。」。在 [管理后台](http://127.0.0.1:8765/admin) 登录并上传，等待列表显示索引成功。
2. 在另一个浏览器会话打开 [问答首页](http://127.0.0.1:8765/)，问「星舰联调窗口什么时候开放？」。确认正文含「每周三 14:30」，接口返回的检索片段来自 `联调说明.txt` 第 1 行。接着发送「你好」，核对页面能正常回应问候。
3. 确认首页没有文件输入控件。用以下命令直接请求上传接口，预期 HTTP 403。

   ```bash
   curl -i http://127.0.0.1:8765/api/admin/documents \
     -H 'X-Document-Filename: test.txt' -H 'Content-Type: text/plain' \
     --data-binary 'unauthenticated upload'
   ```

4. 在管理员后台上传只有空白字符的 TXT，确认列表显示处理失败、原因是缺少可索引正文，且片段数为 0。也可暂时停止向量服务，上传一份含独有事实的 TXT，等待列表显示向量服务失败并恢复服务。再询问该事实，确认失败文件没有来源结果。自动测试还模拟了先成功向量化一批、随后一批失败的情况。
5. 在文档记录中对已入库文档点「替换为新版本」，选择正文有差异的同名文件。新版本处理完成前提问，确认仍能命中旧版；显示「已停用」后再次提问，确认来源指向新版本。再停止向量服务做一次替换，确认新版本显示失败、旧版仍可检索；恢复服务后点「重新索引」，确认新版本入库并停用旧版。

运行 `.venv/bin/python -m unittest -v` 可离线复现上述服务端流程。测试用模拟 HTTP 服务验证文档入库和查询均调用 BGE-M3 接口，并验证生成模型的引文、登录权限和失败文件的检索隔离。

## 接口与会话

| 接口 | 用途 |
| --- | --- |
| `GET /` | 返回网页和前端资源。 |
| `GET /api/session` | 读取当前浏览器选中的对话、历史列表、排查状态和模型配置。 |
| `GET /api/conversations` | 按最近更新时间列出当前浏览器的对话。 |
| `POST /api/conversations` | 新建并选中空白对话。 |
| `GET /api/conversations/{id}` | 读取当前浏览器拥有的指定对话。 |
| `PATCH /api/conversations/{id}` | 用 `title` 更新对话名称，返回历史列表。 |
| `DELETE /api/conversations/{id}` | 删除指定对话和消息，返回删除后的会话与历史列表。删除当前对话后选中最近更新的一条记录。 |
| `POST /api/conversations/{id}/select` | 打开历史对话，并保存当前选择。 |
| `POST /api/demo/api` | 在独立示例中运行 API 超时案例，返回逐轮回放帧。 |
| `POST /api/demo/image` | 在独立示例中运行镜像缺依赖案例，返回逐轮回放帧。 |
| `POST /api/turn` | 接收 `message` 和可选的 `conversation_id`。传 `null` 会新建对话，省略该字段会使用浏览器当前选中的对话。知识库回答包含 `sources` 和 `knowledge_status`。 |
| `POST /api/reset` | 新建会话，默认按每轮输入自动选择问答或排障。 |
| `GET /api/health` | 检查 HTTP 服务，并报告是否配置校内模型密钥。 |
| `GET /admin` | 打开独立管理员后台。 |
| `GET /api/admin/session` | 查看管理员会话和上传大小限制。 |
| `POST /api/admin/login`、`POST /api/admin/logout` | 使用配置的密码登录，或注销管理员会话。 |
| `GET /api/admin/documents` | 查看文档处理状态、版本链及失败原因，需要管理员登录。 |
| `POST /api/admin/documents` | 提交文件字节和 URL 编码的 `X-Document-Filename`，可用 `X-Document-Source-URL` 登记官方来源。传入 `X-Document-Supersedes`（旧文档 ID）时创建新版本，旧版在新版入库成功后停用。需要管理员登录。 |
| `POST /api/admin/documents/{id}/retry` | 使用保留的原始文件重新索引入库失败的文档。成功后若该文档是新版本，则停用其旧版。需要管理员登录。 |

随机浏览器标识和当前对话 ID 分别保存在 HttpOnly、SameSite Strict Cookie，HTTPS 下设置 Secure。SQLite 保存已完成的聊天消息、检索片段元数据和诊断状态。读取和续聊都校验浏览器归属。保留原浏览器 Cookie 和数据库即可在服务重启后恢复。每个对话最多 60 轮。续聊携带同一对话的最近几轮帮助理解指代，模型根据本轮检索片段生成正文。生成模型的调用限制为每进程每分钟 20 次，两套回放均不占该额度。知识库提问限制为每进程每分钟 60 次。

## 校内部署及交我办接入

使用者通过浏览器访问部署地址。FastAPI、OpenJiuwen 和 SQLite 文件都由部署方运行或存放在服务器上。知识库检索需要后端访问 BGE-M3 向量服务，后端可调用同机服务或内网服务，例如 `ZHIYUAN_EMBEDDING_BASE_URL=http://ollama:11434/v1`、`ZHIYUAN_EMBEDDING_MODEL=bge-m3`。排障工作流直接在 FastAPI 所在进程执行。它使用致远一号模型提取信息，模型不可用时仍可用规则处理已支持的故障。

迁移时将 `data/knowledge.sqlite3` 的一致性备份放进服务器的持久卷，配置 `ZHIYUAN_KNOWLEDGE_DB=/app/data/knowledge.sqlite3`，并保证容器用户 UID 10001 可写该目录。使用与入库时相同的模型及配置，否则在服务器上重新导入文档。通过部署平台的密钥设置注入 `ZHIYUAN_MODEL_API_KEY`，部署前确认服务主机能够访问校方模型 API。

1. 在组委会允许的校内环境或能访问致远一号模型的服务主机部署。通过服务端密钥配置注入授权，确认主机能调用校内模型。直接运行 Python 或从根目录的 `Dockerfile` 构建部署镜像。
2. 以一个 Uvicorn worker 启动，保持同一对话的写入顺序和模型请求统计一致。在校方认可的入口终止 HTTPS，按校方要求配置访问控制。若通过网关接入，转发浏览器使用的 Host 和协议。在子路径挂载时使用带尾部斜杠的入口，让静态资源和 API 都沿同一前缀请求。
3. 大赛要求部署到交我办智能体大赛专区。交大公开开发者平台说明，接入其他业务系统需按界面、接口规范提交申请。赛道公开 FAQ 对专区内嵌形式、认证以及提交入口未给出细则。实际接入需要赛事群确认的接入方式、团队发布权限和学校提供或认可的访问地址。若接受 H5 链接登记，提交 HTTPS 地址。若要求原生智能体接口或 jAccount，使用校方提供的协议完成适配。 [大赛公告](https://apps.hpc.sjtu.edu.cn/claw/competition) · [交大开发者平台](https://developer.sjtu.edu.cn/form/introduce/) · [赛事 FAQ](https://claw.sjtu.edu.cn/news/competition/qa/)
4. 上线后用至少一个队外测试账号从「交我办」专区实际进入。验证校内与校外允许访问方式、移动端显示、真实模型对话、会话隔离、错误回退与两套演示。`/api/health` 的 `model_configured` 只检查配置，真实连通性以手动对话为准。

不使用容器时，可以从项目目录启动服务，然后由本机网关代理。

```bash
.venv/bin/python -m uvicorn web_server:app --host 127.0.0.1 --port 8765 --workers 1 --no-access-log
```

## 答辩视频脚本

| 建议时段 | 画面与讲解 |
| --- | --- |
| 0:00–0:12 | 打开应用，点「首次调用 API 超时」。交代两个案例与右侧排查进度。 |
| 0:12–0:32 | 在 401 回合暂停，展示 VPN 已完成检查，待排查项已切换为鉴权。 |
| 0:32–0:58 | 点「自定义镜像缺依赖」，在 Dockerfile 回复处暂停，展示依赖建议。继续回放，观察构建、导入、启动分别入账。 |
| 0:58–1:20 | 新建会话，手动输入「等了很久，客户端自己断开」，回复设备位置，再展示模型继续问 VPN 和右侧待排查项更新。 |
| 1:20–1:30 | 上线后录制从交我办大赛专区进入并完成至少一轮真实模型问答。 |
