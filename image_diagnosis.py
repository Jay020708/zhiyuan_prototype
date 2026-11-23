"""Diagnose missing Python dependencies in a custom container image."""

import re


DOCKER_GUIDE = "https://docs.docker.com/build/concepts/dockerfile/"
PIP_GUIDE = "https://pip.pypa.io/en/stable/reference/requirements-file-format/"
MODELARTS_GUIDE = "https://support.huaweicloud.com/docker-modelarts/docker-modelarts_6025.html"
VERIFY_TAG = "zhiyuan-deps:verify"

MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", re.ASCII)
MISSING = re.compile(r"No module named\s+['\"]?([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)", re.IGNORECASE | re.ASCII)
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*(?:@sha256:[a-f0-9]{64})?")
TAGGED_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*")
MANIFEST = re.compile(r"requirements[A-Za-z0-9_.-]*\.txt", re.IGNORECASE)
DISTRIBUTION = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?")
KNOWN_PACKAGES = {"yaml": "PyYAML", "PIL": "Pillow", "sklearn": "scikit-learn",
                  "requests": "requests", "numpy": "numpy", "pandas": "pandas"}

EVENT_LABELS = {
    "build_ok": "镜像重建成功", "copy_failed": "构建时无法复制依赖文件",
    "pip_failed": "构建时依赖安装失败", "build_failed": "构建失败，原因待确认",
    "missing": "仍有模块缺失", "import_ok": "容器内导入通过",
    "startup_ok": "应用入口启动成功", "startup_failed": "入口启动失败，原因待确认",
}
CHECK_LABELS = {
    "build_patch": "应用 Dockerfile 修改并重建", "build_context": "检查构建上下文并重建",
    "build_pip": "检查依赖安装并重建", "verify_import": "在新镜像中验证导入",
    "verify_runtime": "核对镜像标签和 Python 解释器", "verify_startup": "用原应用入口验证启动",
}
BUILD_CHECKS = {"build_patch", "build_context", "build_pip"}
BUILD_RESULTS = {"build_ok", "copy_failed", "pip_failed", "build_failed"}
VERIFY_RESULTS = {
    "verify_import": {"missing", "import_ok", "startup_ok"},
    "verify_runtime": {"missing", "import_ok", "startup_ok", "build_ok"},
    "verify_startup": {"missing", "startup_ok", "startup_failed"},
}


def is_image_request(message):
    return bool(MISSING.search(message) or re.search(
        r"ModuleNotFoundError|Dockerfile|(?:镜像|容器).{0,30}(?:依赖|启动|构建|报错|跑不起来)|缺(?:少|失).{0,12}(?:Python )?模块",
        message, re.IGNORECASE,
    ))


def local_image_observation(message, state):
    text = message.casefold()
    missing = MISSING.search(message)
    module = missing.group(1) if missing else None
    if module is None and state.get("route") == "image_ask_module":
        bare_module = message.strip().strip("`")
        if MODULE.fullmatch(bare_module):
            module = bare_module
    base = re.search(r"(?im)(?:\bFROM\s+|基础镜像\s*(?:是|为|[:：])?\s*)`?([A-Za-z0-9][A-Za-z0-9_.:/@-]*)", message)
    tagged = TAGGED_IMAGE.search(message) if state.get("route") == "image_ask_base" else None
    manifest = MANIFEST.search(message)
    package = re.search(r"(?:发行包|包名)\s*(?:是|为|[:：])?\s*`?([A-Za-z0-9][A-Za-z0-9_.-]*)", message)

    declared = None
    if re.search(r"没写|未声明|没有列|没加|未安装到清单", text):
        declared = False
    elif re.search(r"已声明|有写|写了|已列|加进|已加到|已经加", text):
        declared = True
    elif state.get("route") == "image_ask_declared":
        short_answer = text.strip().rstrip("。！!？?")
        if short_answer in {"没有", "没", "否"}:
            declared = False
        elif short_answer in {"有", "是", "已经有了"}:
            declared = True

    manifest_name = None
    if re.search(r"(?:没有|未使用|没用)\s*(?:requirements(?:[\w.-]*)?\.txt|依赖清单)|没有依赖文件", text):
        manifest_name = "none"
    elif manifest:
        manifest_name = manifest.group()
    elif re.search(r"pyproject\.toml|environment\.ya?ml|poetry\.lock|pipfile", text):
        manifest_name = "other"

    event = None
    if missing:
        event = "missing"
    elif re.search(r"(failed to calculate checksum|not found|找不到|不存在).{0,60}requirements|"
                   r"requirements[^\n]*?(?:not found|找不到|不存在)|copy.{0,80}(?:failed|找不到)", text):
        event = "copy_failed"
    elif re.search(r"no matching distribution|could not find a version|resolutionimpossible|pip.{0,20}(?:安装失败|下载失败)", text):
        event = "pip_failed"
    elif re.search(r"(?:重建|构建).{0,12}(?:失败|报错)", text):
        event = "build_failed"
    elif re.search(r"启动失败|启动报错|启动不了|(?:没|没有|未)(?:能|正常)?启动", text):
        event = "startup_failed"
    elif re.search(r"(?:应用|服务|程序|入口|脚本).{0,10}(?:启动成功|正常启动|正常运行|跑起来)|启动成功", text):
        event = "startup_ok"
    elif re.search(r"(?:导入|import)(?:(?!没|未|不|失败).){0,15}(?:通过|成功|正常|没问题)|可以.{0,5}(?:导入|import)", text):
        event = "import_ok"
    elif re.search(r"(?:重建|构建).{0,12}(?:完成|成功|好了|通过)", text):
        event = "build_ok"

    return validate_image_observation({
        "module": module,
        "base": base.group(1) if base else tagged.group() if tagged else None,
        "manifest": manifest_name, "declared": declared,
        "distribution": package.group(1) if package else None, "event": event,
    })


def validate_image_observation(obs):
    """Keep generated commands limited to validated identifiers and fixed literals."""
    checks = {"module": MODULE, "base": IMAGE, "manifest": MANIFEST, "distribution": DISTRIBUTION}
    clean = {}
    for field, regex in checks.items():
        value = obs.get(field)
        allowed_manifest = field == "manifest" and value in ("none", "other")
        clean[field] = value if isinstance(value, str) and len(value) <= 180 and (
            allowed_manifest or regex.fullmatch(value)
        ) else None
    clean["declared"] = obs.get("declared") if type(obs.get("declared")) is bool else None
    event = obs.get("event")
    clean["event"] = event if isinstance(event, str) and event in EVENT_LABELS else None
    return clean


def new_image_state():
    return {"scenario": "image", "route": "image_start", "pending": None, "history": [],
            "module": None, "base": None, "manifest": None, "declared": None,
            "distribution": None}


def decide_image(previous, observation):
    state = {**previous, "history": list(previous["history"])}
    module = observation["module"] or state["module"]
    changed_module = state["module"] is not None and module != state["module"]
    pending = state["pending"]
    event = observation["event"]
    if pending:
        valid_events = BUILD_RESULTS if pending in BUILD_CHECKS else VERIFY_RESULTS[pending]
        if event not in valid_events:
            event = None
    if pending and event:
        entry = {"check": pending, "result": event, "module": state["module"]}
        if previous["route"] == "image_ask_failure":
            state["history"][-1] = entry
        else:
            state["history"].append(entry)
        state["pending"] = None

    if changed_module:
        state.update(declared=None, distribution=None)
    for field in ("module", "base", "manifest", "declared", "distribution"):
        if observation[field] is not None:
            state[field] = observation[field]
    if not state["distribution"] and module:
        state["distribution"] = KNOWN_PACKAGES.get(module.split(".")[0])

    if pending and not event:
        state["route"] = "image_ask_failure" if previous["route"] == "image_ask_failure" else "image_wait_result"
        return state

    completed = {record["check"] for record in state["history"] if record["module"] == module}
    next_check = None
    if event == "startup_ok" and ("verify_startup" in completed or "verify_import" in completed):
        route = "image_resolved"
    elif event == "import_ok" and ("verify_import" in completed or "verify_runtime" in completed):
        route, next_check = "image_verify_startup", "verify_startup"
    elif event == "build_ok" and pending:
        route, next_check = "image_verify_import", "verify_import"
    elif event == "copy_failed" and pending:
        route, next_check = ("image_support", None) if "build_context" in completed else ("image_check_context", "build_context")
    elif event == "pip_failed" and pending:
        route, next_check = ("image_support", None) if "build_pip" in completed else ("image_check_pip", "build_pip")
    elif event in {"build_failed", "startup_failed"} and pending:
        route, next_check = ("image_support", None) if previous["route"] == "image_ask_failure" else ("image_ask_failure", pending)
    elif event == "missing" and not changed_module and pending in {"verify_import", "verify_runtime", "verify_startup"}:
        route, next_check = ("image_support", None) if "verify_runtime" in completed else ("image_check_runtime", "verify_runtime")
    elif not module:
        route = "image_ask_module"
    elif not state["base"]:
        route = "image_ask_base"
    elif state["manifest"] in (None, "other"):
        route = "image_ask_manifest"
    elif state["distribution"] is None:
        route = "image_ask_distribution"
    elif state["manifest"] != "none" and state["declared"] is None:
        route = "image_ask_declared"
    elif not event and state["route"] in {"image_resolved", "image_support"}:
        route = state["route"]
    else:
        route, next_check = "image_patch", "build_patch"
    state["route"] = route
    state["pending"] = next_check
    return state


def image_history(state):
    return [f"{CHECK_LABELS[item['check']]}（{EVENT_LABELS[item['result']]}，{item['module']}）"
            for item in state["history"]]


def import_command(module):
    return f'docker run --rm --entrypoint python {VERIFY_TAG} -c "import importlib; importlib.import_module(\'{module}\')"'


def respond_image(state):
    route = state["route"]
    module = state["module"]
    package = state["distribution"]
    manifest = state["manifest"]
    if route == "image_ask_module":
        return "请贴启动时报错的脱敏末行，例如 ModuleNotFoundError: No module named 'yaml'。"
    if route == "image_ask_base":
        return f"启动时缺少 Python 模块 `{module}`。请贴最终运行阶段的 `FROM` 行，或给出基础镜像名和标签。"
    if route == "image_ask_manifest":
        if manifest == "other":
            return ("已记录项目使用另一种依赖配置。首次演示的修改片段基于 pip 依赖文件。"
                    "如果项目已有对应的 requirements 文件，请提供其文件名和目标包声明；否则可以贴出项目实际使用的依赖配置和安装步骤供核对。")
        return (f"已记下基础镜像 `{state['base']}`。项目用 `requirements.txt` 或类似的 pip 依赖文件吗？"
                f"请给出文件名，并说明里面是否声明了提供 `{module}` 的发行包。没有清单也可以直接告诉我。")
    if route == "image_ask_distribution":
        return f"请从项目文档或已能运行的环境确认 `{module}` 对应的 Python 发行包名，并回复「包名是 …」。"
    if route == "image_ask_declared":
        return f"`{manifest}` 里已经声明 `{package}` 了吗？可以只贴对应依赖行。"

    if route == "image_patch":
        requirements = "requirements.txt" if manifest == "none" else manifest
        if manifest == "none":
            change = f"先在构建目录新建 `{requirements}`，写入 `{package}`，有项目锁定版本时沿用该版本。"
        elif state["declared"]:
            change = f"`{requirements}` 已声明 `{package}`。确认安装步骤在最后实际运行的镜像阶段。"
        else:
            change = f"先把 `{package}` 加到 `{requirements}`，有项目锁定版本时沿用该版本。"
        return (f"缺失的模块为 `{module}`，对应发行包为 `{package}`。{change}\n"
                f"保留最终运行阶段的基础镜像 `{state['base']}`、启动配置和现有代码复制步骤；"
                "在该阶段使用与应用入口相同的 Python 安装依赖，例如：\n"
                f"```dockerfile\nCOPY {requirements} /tmp/zhiyuan-requirements.txt\n"
                "RUN python -m pip install --no-cache-dir -r /tmp/zhiyuan-requirements.txt\n```\n"
                f"在项目构建目录运行 `docker build -t {VERIFY_TAG} .`，然后告诉我重建是否成功。\n"
                f"依据：Dockerfile 指南 {DOCKER_GUIDE} · pip 依赖文件 {PIP_GUIDE}")
    if route == "image_verify_import":
        return (f"已记录镜像重建成功。用新镜像单独验证模块导入，成功时退出码为 0。\n"
                f"`{import_command(module)}`\n请回复导入是否通过，若失败只需贴脱敏报错末行。")
    if route == "image_verify_startup":
        return (f"已记录 `{module}` 在新镜像中可以导入。请用新镜像恢复项目原来的启动入口并检查应用就绪日志。"
                f"如在 ModelArts 上验证，先确认作业选择的是重建并发布后的镜像版本。\n依据：{MODELARTS_GUIDE}")
    if route == "image_check_context":
        return ("构建在复制依赖文件时失败。请核对 Dockerfile 中 COPY 的源路径相对于构建上下文是否存在，"
                "并检查 `.dockerignore` 是否排除了该文件，再重建并回报结果。\n"
                "依据：Docker 构建上下文 https://docs.docker.com/build/concepts/context/")
    if route == "image_check_pip":
        return (f"构建已进入 pip 安装阶段。请核对 `{package}` 的包名、锁定版本、所用包源和基础镜像的 Python 版本，"
                f"修正后重建。\n依据：{PIP_GUIDE}")
    if route == "image_check_runtime":
        return (f"已记录 `{module}` 仍缺失。请确认运行的是新标签 `{VERIFY_TAG}`，依赖安装在最终镜像阶段，"
                "并在容器内用入口对应的 Python 查看安装位置。若入口用虚拟环境的解释器，把命令里的 `python` 换为同一路径。\n"
                f"`docker run --rm --entrypoint python {VERIFY_TAG} -m pip show {package}`\n"
                f"核对后重跑导入检查：`{import_command(module)}`。")
    if route == "image_ask_failure":
        return "已记下失败。请贴这一阶段的第一条脱敏错误，构建时优先贴 COPY 或 pip 的报错，启动时优先贴 traceback 末行。"
    if route == "image_wait_result":
        return f"当前在等这一步的结果：{CHECK_LABELS[state['pending']]}。请回复成功还是失败，以及相应的脱敏错误。"
    if route == "image_support":
        history = "、".join(image_history(state)) or "暂无"
        return ("可把以下脱敏记录交给赛事技术支持（computing@sjtu.edu.cn）。\n"
                f"基础镜像：{state['base']}\n模块：{module}\n依赖配置：{manifest}\n已执行：{history}")
    if route == "image_resolved":
        return f"已确认 `{module}` 可以导入，且新镜像使用原入口成功启动。镜像缺依赖排查完成。"
    raise ValueError(f"Unknown image route: {route}")
