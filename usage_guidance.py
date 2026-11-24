"""Short, source-backed guidance for questions covered without online services."""

import re

from diagnosis import API_GUIDE, observe


MODELARTS_IMAGE_GUIDANCE = (
    "先确认目标镜像是否出现在 ModelArts 的镜像管理中，并核对名称和标签。\n\n"
    "1. 如果是自定义镜像，检查目标标签是否已上传到镜像仓库，以及镜像是否已在 ModelArts 注册。\n"
    "2. 回到创建 Notebook、训练作业或推理服务的页面，核对镜像的用途和目标资源规格。"
    "例如，在 Notebook 中，资源池驱动不匹配时，镜像可能不会显示。\n"
    "3. 重新打开镜像选择列表，确认目标标签能选中。若仍找不到，请告诉我任务类型、选用的资源规格和镜像标签。\n\n"
    "参考：ModelArts 自定义镜像与 Notebook 镜像选择\n"
    "https://support.huaweicloud.com/docker-modelarts/docker-modelarts_6025.html\n"
    "https://support.huaweicloud.com/devtool-modelarts/devtool-modelarts_3495.html"
)
SSH_GUIDANCE = (
    "先在发起 SSH 的电脑确认已接入校园网或交大 VPN，再到致远一号控制台确认目标云主机处于运行中，"
    "且 SSH 使用的是该实例配置的连接地址。完成后重试连接。\n\n"
    "如果依然超时，请告诉我你从哪里连接，并提供脱敏后的报错末行。\n"
    "参考：交大云主机排障 https://claw.sjtu.edu.cn/guide/faq/"
)
RATE_LIMIT_GUIDANCE = (
    "先暂停连续重试，查看该 API Key 的调用额度和近期用量。恢复后用同一个模型发一条最小请求。"
    "如果单条请求仍返回 429，请提供脱敏状态码、请求时间以及是否有并行请求，便于继续定位。\n\n"
    f"参考：致远一号 API 用量说明 {API_GUIDE}"
)
API_START_GUIDANCE = (
    "在「交我办」搜索「致远一号 AI 模型 API 申请」，填写用途并提交。开通后，在本地配置收到的 API 地址和密钥。"
    "在校园网内发起请求，或在校外先接入交大 VPN。先验证能取得模型列表，再用列表中的模型 ID 发送一条最小对话。\n\n"
    f"参考：致远一号 API 申请与调用 {API_GUIDE}"
)
CLARIFY_GUIDANCE = (
    "你是在本地调用模型 API、使用致远一号云主机，还是在 ModelArts 运行任务？"
    "请告诉我卡在申请、安装、构建、部署或启动哪一步，有报错的话可以贴出脱敏后的最后一行。"
)


def usage_guidance(question, presets):
    text = question.casefold()
    api = "api" in text or any(word in text for word in ("模型调用", "调用模型", "调用大模型"))
    if "镜像" in text and any(word in text for word in ("modelarts", "notebook")) and re.search(
        r"[找看选]不到|找不着|不显示|列表里没有|无法选择", text,
    ):
        return MODELARTS_IMAGE_GUIDANCE
    if "ssh" in text and any(word in text for word in ("超时", "timed out", "连不上")):
        return SSH_GUIDANCE
    if api and re.search(r"\b429\b|限流", text):
        return RATE_LIMIT_GUIDANCE
    if any(word in text for word in ("怎么", "如何", "怎样", "从哪", "第一步")):
        if any(word in text for word in ("环境依赖", "配置环境", "装环境")):
            return presets["如何安装致远一号的环境依赖？"]
        if "镜像" in text and any(word in text for word in ("安装", "部署", "制作")):
            return presets["如何安装致远一号的镜像？"]
        if api and any(word in text for word in ("申请", "首次", "第一次")):
            return API_START_GUIDANCE
    return None


def should_diagnose(question, state):
    if state["route"] not in {"start", "onboarding", "resolved", "support", "image_resolved", "image_support"}:
        return True
    text = question.casefold()
    if re.search(r"modulenotfounderror|no module named|缺(?:少|失).{0,12}(?:python )?模块", text):
        return True
    if "api" in text or "模型" in text:
        return observe(question, state["route"])["result"] in {"timeout", "401"}
    return False
