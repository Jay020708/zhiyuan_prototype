"""Run the Zhiyuan-1 support prototype with OpenJiuwen Core."""

import argparse
import asyncio
import os
import sys
import warnings
from getpass import getpass
from uuid import uuid4

import httpx
from openjiuwen.core.common.logging.log_config import configure_log_config

configure_log_config({
    "level": "WARNING", "output": ["console"],
    "interface_output": ["console"], "performance_output": ["console"],
})

from openjiuwen.core.application.workflow_agent import WorkflowAgent
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.legacy import WorkflowAgentConfig
from openjiuwen.core.workflow import End, Start, Workflow, WorkflowCard, WorkflowComponent

from diagnosis import CHECKS, check_history, decide, new_state, observe, redact, respond
from image_diagnosis import (
    CHECK_LABELS, decide_image, image_history, is_image_request, local_image_observation,
    new_image_state, respond_image,
)
from model_observer import DEFAULT_MODEL, KEY_ENV, SUPPORTED_MODELS, ZhiyuanObserver


class Observe(WorkflowComponent):
    def __init__(self, observer):
        super().__init__()
        self.observer = observer

    async def invoke(self, inputs, session, context):
        message = inputs["query"]
        state = inputs["state"]
        scenario = state["scenario"]
        if scenario != "image" and state["route"] in {"start", "onboarding"} and is_image_request(message):
            state = new_image_state()
            scenario = "image"
        notice = None
        if self.observer:
            observation, notice = await self.observer.extract(message, state)
            if observation is not None:
                return {"scenario": scenario or "api", "observation": observation,
                        "interpreter": self.observer.model, "notice": ""}
        observation = local_image_observation(message, state) if scenario == "image" else observe(message, state["route"])
        return {"scenario": scenario or "api", "observation": observation,
                "interpreter": "本地规则", "notice": notice or ""}


class Diagnose(WorkflowComponent):
    async def invoke(self, inputs, session, context):
        state = inputs["previous"]
        observation = inputs["observation"]
        if inputs["scenario"] == "image":
            return decide_image(state if state["scenario"] == "image" else new_image_state(), observation)
        return decide(state, observation)


class Reply(WorkflowComponent):
    async def invoke(self, inputs, session, context):
        state = inputs["state"]
        return {"reply": respond_image(state) if state["scenario"] == "image" else respond(state)}


def build_agent(observer=None, workflow_id="zhiyuan-diagnosis"):
    card = WorkflowCard(
        id=workflow_id,
        version="0.2.0",
        name="致远一号使用排障",
        description="诊断 API 连接或镜像依赖问题，并根据用户验证结果继续排查。",
        input_params={
            "type": "object",
            "properties": {"query": {"type": "string"}, "state": {"type": "object"}},
            "required": ["query", "state"],
        },
    )
    workflow = Workflow(card=card)
    workflow.set_start_comp("start", Start(), {"query": "${query}", "state": "${state}"})
    workflow.add_workflow_comp("observe", Observe(observer), inputs_schema={
        "query": "${start.query}", "state": "${start.state}",
    })
    workflow.add_workflow_comp("diagnose", Diagnose(), inputs_schema={
        "previous": "${start.state}", "observation": "${observe.observation}", "scenario": "${observe.scenario}",
    })
    workflow.add_workflow_comp("reply", Reply(), inputs_schema={"state": "${diagnose}"})
    workflow.set_end_comp("end", End(), inputs_schema={
        "reply": "${reply.reply}", "state": "${diagnose}",
        "interpreter": "${observe.interpreter}", "notice": "${observe.notice}",
    })
    nodes = ("start", "observe", "diagnose", "reply", "end")
    for source, target in zip(nodes, nodes[1:]):
        workflow.add_connection(source, target)

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="AgentConfig is deprecated", category=DeprecationWarning)
        agent = WorkflowAgent(WorkflowAgentConfig(id=f"{workflow_id}-assistant", version="0.2.0"))
    agent.add_workflows([workflow])
    return agent


class Conversation:
    def __init__(self, agent, sanitize=redact, case=None):
        self.agent = agent
        self.sanitize = sanitize
        self.id = uuid4().hex
        self.state = new_image_state() if case == "image" else new_state()
        self.interpreter = ""
        self.notice = ""

    async def turn(self, message, *, agent=None):
        result = await Runner.run_agent(self.agent if agent is None else agent, {
            "query": self.sanitize(message), "state": self.state, "conversation_id": self.id,
        })
        output = result["output"].result["output"]
        self.state = output["state"]
        self.interpreter = output["interpreter"]
        self.notice = output["notice"]
        return output["reply"]

    def summary(self):
        if self.state["scenario"] == "image":
            history = image_history(self.state)
            pending = CHECK_LABELS.get(self.state["pending"])
            manifest = {"none": "无清单", "other": "其他依赖配置"}.get(self.state["manifest"], self.state["manifest"])
            facts = {"模块": self.state["module"], "基础镜像": self.state["base"], "依赖": manifest}
        else:
            facts = {"位置": self.state["location"], "VPN": {True: "已连接", False: "未连接", None: None}[self.state["vpn"]]}
            history = check_history(self.state)
            pending = CHECKS.get(self.state["pending"])

        route = self.state["route"]
        questions = {"ask_location": "确认请求的运行位置", "ask_vpn": "确认设备是否连接交大 VPN",
                     "image_ask_module": "确认缺失的 Python 模块", "image_ask_base": "确认基础镜像和标签",
                     "image_ask_manifest": "确认项目的依赖清单", "image_ask_distribution": "确认模块对应的发行包",
                     "image_ask_declared": "确认依赖清单是否已声明发行包", "image_ask_failure": "确认失败阶段的脱敏报错"}
        status = "resolved" if route in {"resolved", "image_resolved"} else "support" if route in {"support", "image_support"} else "active"
        return {"scenario": self.state["scenario"], "facts": facts, "history": history,
                "pending": questions.get(route, pending), "status": status}

    def show_state(self):
        summary = self.summary()
        facts = " | ".join(f"{field}：{value or '待确认'}" for field, value in summary["facts"].items())
        print(f"[会话状态] {facts}")
        print(f"[会话状态] 已检查：{'、'.join(summary['history']) or '暂无'} | 待反馈：{summary['pending'] or '无'}")
        if self.interpreter:
            print(f"[本轮理解] {self.interpreter}")


DEMO_MESSAGES = (
    "我第一次用致远一号，怎么调用大模型 API？",
    "我已经配置好了，但请求连接超时了。",
    "在家里的个人电脑上。",
    "没有。",
    "连上 VPN 后重试，现在返回 HTTP 401。",
    "我在本地修正了请求头，现在拿到了模型列表。",
    "最小对话请求成功，已收到模型回复。",
)
LIVE_DEMO_MESSAGES = (
    "我第一次用致远一号，怎么调用大模型 API？",
    "一发请求就卡住，等了好一阵之后客户端自己断开了，始终没看到响应。",
    "我在家里的个人电脑上。",
    "还没拨。",
    "交大的 VPN 已经接上了。重新发了一次，这回服务端说 Unauthorized。",
    "改了本地请求头，现在能看到平台列出来的可选模型了。",
    "我挑了一个模型发了句你好，已经看到它回我的那句话了。",
)
DEMO_ROUTES = (
    "onboarding", "ask_location", "ask_vpn", "check_vpn", "check_auth", "verify_chat", "resolved",
)
IMAGE_DEMO_MESSAGES = (
    "自定义镜像一启动就报错：ModuleNotFoundError: No module named 'yaml'",
    "最终运行阶段是 FROM python:3.11-slim。",
    "有 requirements.txt，里面没写 PyYAML。",
    "重建失败，COPY requirements.txt 提示 not found。",
    "从项目根目录执行后，镜像重建成功。",
    "新镜像验证导入仍然报 ModuleNotFoundError: No module named 'yaml'",
    "确认之前用了旧标签。换成新标签后，容器内导入成功。",
    "用同一个新镜像和原入口启动，应用正常启动。",
)
IMAGE_DEMO_ROUTES = (
    "image_ask_base", "image_ask_manifest", "image_patch", "image_check_context",
    "image_verify_import", "image_check_runtime", "image_verify_startup", "image_resolved",
)


async def run_demo(agent, observer, case):
    mode = f"致远模型 {observer.model}" if observer else "本地规则"
    title = "自定义镜像缺少依赖" if case == "image" else "首次 API 调用超时"
    print(f"致远一号排障演示｜{title}｜示例用户反馈，OpenJiuwen 实时执行｜{mode}")
    conversation = Conversation(agent, observer.sanitize if observer else redact)
    if case == "image":
        messages, routes = IMAGE_DEMO_MESSAGES, IMAGE_DEMO_ROUTES
    else:
        messages, routes = LIVE_DEMO_MESSAGES if observer else DEMO_MESSAGES, DEMO_ROUTES
    passed = True
    for message, expected_route in zip(messages, routes, strict=True):
        print(f"\n用户  {message}")
        print(f"助手  {await conversation.turn(message)}")
        conversation.show_state()
        if conversation.notice:
            print(f"[模型状态] {conversation.notice}，本轮已用本地规则。")
            passed = False
        if conversation.state["route"] != expected_route:
            print(f"[演示检查] 预期 {expected_route}，实际 {conversation.state['route']}。")
            passed = False
    print("\n演示检查通过。" if passed else "\n演示有未通过的步骤，请查看上方状态。")
    return 0 if passed else 1


async def run_interactive(agent, observer, case):
    print("致远一号排障助手｜/状态 查看会话  /重置 新建会话  /退出 结束")
    print("可输入错误码或脱敏日志。API Key 请留在自己的客户端。")
    sanitize = observer.sanitize if observer else redact
    conversation = Conversation(agent, sanitize, case)
    while True:
        try:
            message = input("\n用户  ").strip()
        except EOFError:
            break
        if message == "/退出":
            break
        if message == "/状态":
            conversation.show_state()
        elif message == "/重置":
            conversation = Conversation(agent, sanitize, case)
            print("助手  已开始新的诊断会话。")
        elif message:
            print(f"助手  {await conversation.turn(message)}")
            if conversation.notice:
                print(f"[模型状态] {conversation.notice}，本轮已用本地规则。")
    return 0


async def run(demo, model, key, case):
    timeout = httpx.Timeout(18, connect=5)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        observer = ZhiyuanObserver(client, key, model) if key else None
        agent = build_agent(observer)
        try:
            if demo:
                return await run_demo(agent, observer, case or "api")
            return await run_interactive(agent, observer, case)
        finally:
            await Runner.stop()


def main():
    parser = argparse.ArgumentParser(description="致远一号 API 与镜像排障原型")
    parser.add_argument("--demo", action="store_true", help="用预设输入演示完整排障流程")
    parser.add_argument("--case", choices=("api", "image"), help="选择演示案例，默认 api；交互模式可用 image 直接进入镜像排障")
    parser.add_argument("--live", action="store_true", help="调用致远一号模型理解用户输入")
    parser.add_argument("--model", choices=SUPPORTED_MODELS, default=DEFAULT_MODEL, help="致远一号模型调用名")
    args = parser.parse_args()
    key = None
    if args.live:
        key = os.environ.get(KEY_ENV, "").strip()
        if not key and sys.stdin.isatty():
            key = getpass("致远一号赛事 API Key（输入隐藏）：").strip()
        if not key:
            parser.error(f"请在终端隐式输入 API Key，或通过环境变量 {KEY_ENV} 注入。")
    return asyncio.run(run(args.demo, args.model, key, args.case))


if __name__ == "__main__":
    raise SystemExit(main())
