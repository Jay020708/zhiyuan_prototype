# 致远一号使用排障原型

这是上海交通大学「交我创」昇腾开放赛道的网页和命令行原型。OpenJiuwen Core 的 `WorkflowAgent` 支持首次 API 调用超时和自定义镜像缺少 Python 依赖两个案例，并在各自会话内保存用户提供的环境、待反馈步骤和已检查记录。

## 网页演示

安装下方依赖后，在项目目录运行 `.venv/bin/python web_server.py --live`，按提示隐藏输入赛事 API Key，然后打开 [本机演示](http://127.0.0.1:8765/)。去掉 `--live` 可在无 Key 的环境中用本地规则演示。网页支持一键回放两个案例、暂停或查看全部、手动对话以及会话内查看当前待检查项和已完成记录。案例按钮使用预设反馈和本地规则运行 OpenJiuwen 工作流，手动对话在配置赛用密钥时调用真实模型。

[技术文档](TECHNICAL.md)包含部署命令、交我办接入待确认项、接口和答辩视频脚本。命令行使用方式如下。

通过左侧「历史对话」可以新建或打开对话，点击条目右侧的「⋯」可重命名或删除。聊天记录和排查进度保存在服务端 SQLite，同一浏览器可在刷新或服务重启后继续。

## 文档知识库问答

网页用同一个对话入口接收使用问题、脱敏报错和重试结果。配置 `ZHIYUAN_EMBEDDING_BASE_URL` 后，资料问题走知识库检索。API 超时、401 和镜像缺依赖进入 OpenJiuwen 排障，期间可以插问资料并保留排查进度。两个示例按钮提供独立回放。管理员在 [本机管理后台](http://127.0.0.1:8765/admin) 登录后可上传 UTF-8 TXT、Markdown（`.md`）和含可提取文本的 PDF，默认每份最大 50 MiB。文档记录支持替换旧手册和重新索引：新版本入库成功后才停用旧版，失败时旧版继续参与检索。

知识库使用 BGE-M3 生成 1024 维向量，SQLite 持久化索引。致远一号模型根据问题和检索片段直接组织正文，片段元数据由服务器记录。支持用 `ingest_documents.py` 从材料目录批量导入，材料更新时加 `--replace` 建立新版本。Ollama、TEI 配置与验收步骤见[知识库部署与验收](TECHNICAL.md#知识库部署与验收)。

## 安装和运行

准备 Python 3.11–3.13，进入本目录后执行：

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py --live --demo --case api
.venv/bin/python app.py --live --demo --case image
```

如果使用 Python 3.11 或 3.13，将第一行的 `python3.12` 换成实际的解释器命令。首次启动需要加载 OpenJiuwen 及其依赖。真实模型模式默认用 `deepseek-chat`，并通过隐藏输入提示读取赛事 API Key。部署时可以用环境变量 `ZHIYUAN_MODEL_API_KEY` 注入密钥。通过校园网或交大 VPN 访问模型。演示每轮显示理解来源，任何一轮回退到本地规则或走错预期分支，最终都会报告演示检查未通过。用 `--model qwen` 或 `--model deepseek-reasoner` 可尝试授权邮件列出的其他调用名。

手动演示运行 `.venv/bin/python app.py --live`，第一条输入镜像的 `ModuleNotFoundError` 会进入镜像排障，也可以加 `--case image` 直接进入。输入 `/状态` 可查看已检查项和当前待反馈的步骤，`/重置` 开启新会话并切换排障主题，`/退出` 结束。去掉 `--live` 可离线运行预设演示或本地规则模式。

## 案例一  校外首次调用 API 超时

1. 输入「我第一次用致远一号，怎么调用大模型 API？」助手给出官方申请和验证入口。
2. 输入「连接超时了」。助手先问在哪里运行。回答「在家里的个人电脑上」，它接着问是否已接入交大 VPN。
3. 回答「没有」。助手给出接入校方 VPN 后重试模型列表的步骤和依据，等待结果。
4. 输入「连上 VPN 后重试，现在返回 HTTP 401」。助手记录该次重试，切换到鉴权检查。输入 `/状态` 展示第一项检查已经完成，当前正等待鉴权重试结果。
5. 输入「修正请求头后拿到了模型列表」。助手指导用返回的模型 ID 发起一次最小对话。输入「最小对话请求成功，已收到模型回复」，结束诊断。

在第 4 步也可以回复「连上 VPN 后重试仍然超时」。助手转而核对 API 地址，再次超时则生成包含两项检查的技术支持摘要。

## 案例二  自定义镜像启动时缺少依赖

1. 输入 `ModuleNotFoundError: No module named 'yaml'`。助手提取模块名并追问最终运行阶段的 `FROM`。
2. 依次回复 `FROM python:3.11-slim` 和「有 requirements.txt，里面没写 PyYAML」。助手给出把 `PyYAML` 加入依赖清单的建议，并生成最终阶段的 `COPY` 和 `RUN python -m pip install` 片段。
3. 回复「重建失败，COPY requirements.txt 提示 not found」。助手引导检查构建上下文及 `.dockerignore`。回复「重建成功」后，助手要求在新镜像中单独验证模块导入。
4. 回复「验证导入仍然报 ModuleNotFoundError: No module named 'yaml'」。助手记录失败，并引导核对新镜像标签、最终阶段和 Python 解释器。再次失败会生成包含已执行步骤的技术支持摘要。
5. 回复「换成新标签后，容器内导入成功」。助手要求使用原入口确认启动。回复「应用正常启动」后完成排查。用 `/状态` 可以演示记录里分别保留了构建、导入和启动结果。

压缩包内的 `examples/missing_dependency` 是可在有 Docker 的环境中复现的最小示例。进入示例目录，用 `docker build -t zhiyuan-deps:broken .` 构建，再用 `docker run --rm zhiyuan-deps:broken` 复现报错。按助手提示增加 `PyYAML` 和 Dockerfile 安装步骤后构建 `zhiyuan-deps:verify`，分别执行助手的导入检查和 `docker run --rm zhiyuan-deps:verify`。预设演示中的报错和用户反馈是示例输入，CLI 本身不运行 Docker。

## 实现与范围

`app.py` 定义 OpenJiuwen 的工作流节点和会话入口。`model_observer.py` 通过致远一号 OpenAI 兼容接口提取当前案例需要的事实。模型输入只包含脱敏后的当前消息与理解所需的结构化上下文。模型输出通过字段校验后，由 `diagnosis.py` 或 `image_diagnosis.py` 决定下一步并维护检查记录。网络超时、HTTP 错误或输出无效时，本轮显示原因并回退到本地规则。每轮最多发出一个模型请求。

会话状态在同一进程内保留。镜像案例首版覆盖 pip 管理的缺失 Python 依赖。用户在自己的 Docker 或 ModelArts 环境中执行构建和验证，并回报脱敏结果。

执行 `.venv/bin/python -m unittest -v`，可离线验证工作流分支、会话隔离、检查历史、凭据脱敏和模型失败回退。测试中的模型 HTTP 响应由测试程序模拟。

[校方 API 使用文档](https://claw.sjtu.edu.cn/guide/sjtu-api/) · [OpenJiuwen Core](https://github.com/openJiuwen-ai/agent-core) · [Dockerfile 指南](https://docs.docker.com/build/concepts/dockerfile/) · [ModelArts 自定义镜像](https://support.huaweicloud.com/docker-modelarts/docker-modelarts_6025.html)
