"""State and decision rules for the first Zhiyuan-1 API call."""

import re


API_GUIDE = "https://claw.sjtu.edu.cn/guide/sjtu-api/"

BASE_URL = "https://models.sjtu.edu.cn/api/v1"

SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[^\s'\";,]+"),
    re.compile(r"(?i)(\b[\w-]*(?:api[_ -]?key|token|password|authorization)\b|密码|密钥)"
               r"([\"']?\s*(?:[:：=]|是)\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,，;；。]+)"),
    re.compile(r"\bsk-[a-zA-Z0-9_-]{8,}\b"),
)


def redact(message):
    message = SECRET_PATTERNS[0].sub("Bearer [已遮盖]", message)
    message = SECRET_PATTERNS[1].sub(r"\1\2[已遮盖]", message)
    return SECRET_PATTERNS[2].sub("[已遮盖]", message)


def new_state():
    return {"scenario": None, "location": None, "vpn": None, "error": None, "route": "start",
            "pending": None, "history": []}


def observe(message, route):
    text = message.casefold().strip().rstrip("。！!？?，,")
    location = None
    if any(word in text for word in ("校外", "家里", "家中", "在家", "外网")):
        location = "校外"
    elif any(word in text for word in ("校内", "校园网", "学校里")):
        location = "校内"

    vpn = None
    if re.search(r"(?:没|未|不)(?:有|曾)?(?:连|开|接|用|使用|登录)\w{0,5}\s*vpn", text):
        vpn = False
    elif re.search(r"vpn\s*(?:没|未|断开|没有)", text):
        vpn = False
    elif re.search(r"(?:连|开|接入|登录)(?:接|上|了|着|校方|交大)*\s*vpn|vpn\s*(?:已|接通|连上|开着)", text):
        vpn = True
    elif route == "ask_vpn":
        if text in {"没有", "没连", "没开", "未连接", "没接", "否"}:
            vpn = False
        elif text in {"有", "连了", "开了", "已连接", "连上了", "是"}:
            vpn = True

    negative_result = re.search(r"(?:未|没|没有|无法|还没)\s*(?:成功|返回|收到|拿到)|失败|不行", text)
    result = None
    if re.search(r"(?<!\d)401(?!\d)|unauthorized|鉴权失败", text):
        result = "401"
    elif re.search(r"超时|timed?\s*out|timeouterror", text):
        result = "timeout"
    elif (not negative_result and
          re.search(r"(?:模型列表|/models).{0,10}(?:返回|拿到|成功)|(?:返回|拿到).{0,8}模型列表", text)):
        result = "models_ok"
    elif (re.search(r"对话.{0,8}(?:成功|返回|正常回复)|(?:收到|返回).{0,8}(?:模型回复|对话回复)", text)
          and not negative_result):
        result = "chat_ok"
    elif text in {"成功了", "可以了", "正常了", "已成功"}:
        result = "success"
    elif any(word in text for word in ("还是失败", "仍然失败", "还是不行", "没成功")):
        result = "failed"
    return {"location": location, "vpn": vpn, "result": result}


CHECKS = {
    "check_vpn": "VPN 接入后重试模型列表",
    "check_url": "核对模型 API 地址后重试",
    "check_auth": "本地核对鉴权后重试",
    "verify_chat": "发起最小对话请求",
}
RESULT_LABELS = {
    "timeout": "仍然超时",
    "401": "返回 401",
    "models_ok": "模型列表已返回",
    "chat_ok": "对话成功",
    "failed": "重试失败，详情待确认",
}


def decide(previous, observation):
    state = {**previous, "scenario": "api", "history": list(previous["history"])}
    if observation["location"] is not None:
        state["location"] = observation["location"]
    if observation["vpn"] is not None:
        state["vpn"] = observation["vpn"]

    result = observation["result"]
    pending = state["pending"]
    if result == "success" and pending:
        result = "chat_ok" if pending == "verify_chat" else "models_ok"
    if result in {None, "success"} and pending:
        state["route"] = "ask_result"
        return state
    if result in RESULT_LABELS and pending:
        state["history"].append({"check": pending, "result": result})
        state["pending"] = None

    if result in {"timeout", "401", "failed"}:
        state["error"] = result
    elif result in {"chat_ok", "models_ok"}:
        state["error"] = None

    tried = {item["check"] for item in state["history"]}
    error = state["error"]
    if result == "chat_ok" or (error is None and previous["route"] == "resolved" and result is None):
        route = "resolved"
    elif result == "models_ok":
        route = "verify_chat"
    elif error == "401":
        route = "support" if "check_auth" in tried else "check_auth"
    elif error == "timeout":
        if state["location"] is None:
            route = "ask_location"
        elif state["location"] == "校外" and state["vpn"] is None and "check_vpn" not in tried:
            route = "ask_vpn"
        elif state["location"] == "校外" and state["vpn"] is False and "check_vpn" not in tried:
            route = "check_vpn"
        else:
            route = "support" if "check_url" in tried else "check_url"
    elif error == "failed":
        route = "support"
    else:
        route = "onboarding"
    state["route"] = route
    if route in CHECKS:
        state["pending"] = route
    return state


RESPONSES = {
    "onboarding": (
        "在「交我办」搜索「致远一号」AI 模型 API 申请（测试）。"
        "获批后在本地配置 API Key 和模型地址，先请求模型列表，再用返回的模型 ID 发起对话。"
        "遇到问题时，可以告诉我超时、401 或其他脱敏错误。",
        "第 1 节与第 4 节",
    ),
    "ask_location": ("我先确认请求的运行位置。你是在校内运行，还是在校外个人电脑运行？", None),
    "ask_vpn": ("记下了，请求在校外运行。发起请求的电脑已经连接交大 VPN 了吗？", "第 2 节"),
    "check_vpn": (
        f"先在发起请求的电脑上连接交大 VPN，然后在本地重试 GET {BASE_URL}/models。"
        "预期会返回模型列表。请告诉我是拿到列表、仍然超时，还是返回了 HTTP 状态码。",
        "第 2 节与第 4 节",
    ),
    "check_url": (
        f"接下来核对本地客户端的 base_url 是否为 {BASE_URL}。"
        "核对后重试模型列表请求，告诉我是拿到列表、仍然超时，还是返回了 HTTP 状态码。",
        "第 4 节",
    ),
    "check_auth": (
        "这次拿到了 HTTP 401，接下来检查鉴权。请在本地核对 API Key 是否有效，"
        "以及请求是否带有 Authorization: Bearer 请求头，然后重试模型列表请求。"
        "只回报结果或状态码即可。",
        "第 2 节与第 4 节",
    ),
    "verify_chat": (
        "模型列表已返回。请用列表中的模型 ID 发起一条最小对话请求，例如「你好」，"
        "再告诉我是否收到模型回复。",
        "第 4 节",
    ),
    "resolved": ("已确认收到模型回复，首次调用验证完成。", None),
}


def check_history(state):
    return [f"{CHECKS[item['check']]}（{RESULT_LABELS[item['result']]}）"
            for item in state["history"]]


def respond(state):
    route = state["route"]
    if route == "ask_result":
        return f"当前在等这一步的结果：{CHECKS[state['pending']]}。请回复错误码、超时或成功结果。"
    if route == "support":
        vpn = {None: "待确认", True: "已连接", False: "未连接"}[state["vpn"]]
        history = "、".join(check_history(state)) or "暂无"
        error = {"timeout": "连接超时", "401": "HTTP 401", "failed": "需补充错误详情"}[state["error"]]
        return ("可以将下面的脱敏摘要复制给赛事技术支持（computing@sjtu.edu.cn）。\n"
                f"运行位置：{state['location'] or '待确认'}\nVPN：{vpn}\n当前问题：{error}\n已检查：{history}")
    answer, section = RESPONSES[route]
    if section:
        return f"{answer}\n依据：交我算《本地大模型 API 使用文档》{section} {API_GUIDE}"
    return answer
