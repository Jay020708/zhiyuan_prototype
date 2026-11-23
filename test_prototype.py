import json
import unittest

import httpx

from app import DEMO_MESSAGES, IMAGE_DEMO_MESSAGES, IMAGE_DEMO_ROUTES, Conversation, Runner, build_agent
from diagnosis import BASE_URL, redact
from model_observer import ZhiyuanObserver, parse_image_observation, parse_observation


class WorkflowIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_diagnosis_paths_through_openjiuwen(self):
        agent = build_agent()
        self.addAsyncCleanup(Runner.stop)
        conversation = Conversation(agent)
        expected = (
            "onboarding", "ask_location", "ask_vpn", "check_vpn",
            "check_auth", "verify_chat", "resolved",
        )
        for message, route in zip(DEMO_MESSAGES, expected, strict=True):
            response = await conversation.turn(message)
            self.assertEqual(conversation.state["route"], route, response)
        self.assertEqual(conversation.state["location"], "校外")
        self.assertTrue(conversation.state["vpn"])
        self.assertEqual(conversation.state["history"], [
            {"check": "check_vpn", "result": "401"},
            {"check": "check_auth", "result": "models_ok"},
            {"check": "verify_chat", "result": "chat_ok"},
        ])
        self.assertIsNone(conversation.state["pending"])

        ongoing = Conversation(agent)
        await ongoing.turn("我在校外，模型 API 超时，没开 VPN")
        self.assertEqual(ongoing.state["route"], "check_vpn")
        await ongoing.turn("已经连上 VPN")
        self.assertEqual(ongoing.state["route"], "ask_result")
        self.assertEqual(ongoing.state["history"], [])
        await ongoing.turn("重试还是连接超时")
        self.assertEqual(ongoing.state["route"], "check_url")
        await ongoing.turn("模型列表没有返回")
        self.assertEqual(ongoing.state["route"], "ask_result")
        response = await ongoing.turn("核对了 API 地址，重试仍然超时")
        self.assertEqual(ongoing.state["route"], "support")
        self.assertIn("VPN 接入后重试模型列表（仍然超时）", response)
        self.assertIn("核对模型 API 地址后重试（仍然超时）", response)
        self.assertEqual(conversation.state["route"], "resolved")

        direct_success = Conversation(agent)
        await direct_success.turn("在家，没连 VPN，第一次调用连接超时")
        await direct_success.turn("连上 VPN 后返回了模型列表")
        self.assertEqual(direct_success.state["route"], "verify_chat")
        await direct_success.turn("可以了")
        self.assertEqual(direct_success.state["route"], "resolved")

    def test_log_secrets_are_redacted_before_workflow(self):
        example = ('HTTP 401; Authorization: Bearer demo-secret; '
                   'API_KEY="fake-api-secret"; password=demo-password')
        sanitized = redact(example)
        self.assertIn("HTTP 401", sanitized)
        for value in ("demo-secret", "fake-api-secret", "demo-password"):
            self.assertNotIn(value, sanitized)

    async def test_model_extraction_and_fallback_keep_workflow_history(self):
        fake_key = "fake-runtime-key"
        requests_seen = []
        replies = iter((
            {"location": None, "vpn": None, "result": "timeout"},
            {"location": "校外", "vpn": False, "result": None},
            429,
            {"location": None, "vpn": None, "result": "models_ok"},
            {"location": None, "vpn": "yes", "result": "chat_ok"},
            {"location": None, "vpn": None, "result": "chat_ok"},
        ))

        async def handler(request):
            self.assertEqual(str(request.url), f"{BASE_URL}/chat/completions")
            self.assertEqual(request.headers["Authorization"], f"Bearer {fake_key}")
            self.assertNotIn(fake_key, request.content.decode())
            body = json.loads(request.content)
            self.assertEqual(body["model"], "deepseek-chat")
            requests_seen.append(json.loads(body["messages"][1]["content"]))
            reply = next(replies)
            if isinstance(reply, int):
                return httpx.Response(reply, text="Example gateway response")
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            observer = ZhiyuanObserver(client, fake_key)
            agent = build_agent(observer)
            self.addAsyncCleanup(Runner.stop)
            conversation = Conversation(agent, observer.sanitize)
            await conversation.turn(f"请求卡了半天，软件随后自己断开。{fake_key}")
            self.assertEqual(conversation.interpreter, "deepseek-chat")
            self.assertEqual(conversation.state["route"], "ask_location")
            await conversation.turn("我在市区租的房子，校方的隧道还没拨起来。")
            self.assertEqual(conversation.state["route"], "check_vpn")
            await conversation.turn("连上 VPN 后重试，现在返回 HTTP 401。")
            self.assertEqual(conversation.interpreter, "本地规则")
            self.assertIn("429", conversation.notice)
            self.assertEqual(conversation.state["route"], "check_auth")
            await conversation.turn("现在屏幕上已经列出来可以选的模型了。")
            self.assertEqual(conversation.state["route"], "verify_chat")
            await conversation.turn("我还没运行第二个请求。")
            self.assertEqual(conversation.state["route"], "ask_result")
            self.assertIn("格式", conversation.notice)
            await conversation.turn("刚才它给我回了那句话。")
            self.assertEqual(conversation.state["route"], "resolved")
            self.assertEqual([check["result"] for check in conversation.state["history"]],
                             ["401", "models_ok", "chat_ok"])
            self.assertEqual(requests_seen[1]["既有状态"]["route"], "ask_location")
            self.assertEqual(requests_seen[3]["既有状态"]["pending"], "check_auth")

    def test_strict_model_observation_schema(self):
        self.assertEqual(parse_observation('```json\n{"location":null,"vpn":false,"result":null}\n```'),
                         {"location": None, "vpn": False, "result": None})
        for invalid in ('[]', '{"location":null,"vpn":1,"result":null}',
                        '{"location":"校内","vpn":null,"result":"resolved"}'):
            with self.assertRaises(ValueError):
                parse_observation(invalid)

    async def test_image_case_keeps_build_import_and_startup_separate(self):
        agent = build_agent()
        self.addAsyncCleanup(Runner.stop)
        conversation = Conversation(agent)
        for message, route in zip(IMAGE_DEMO_MESSAGES, IMAGE_DEMO_ROUTES, strict=True):
            answer = await conversation.turn(message)
            self.assertEqual(conversation.state["route"], route, answer)
            if route == "image_patch":
                self.assertIn("PyYAML", answer)
                self.assertIn("COPY requirements.txt", answer)
                self.assertIn("RUN python -m pip install", answer)
            elif route == "image_verify_import":
                self.assertIn("importlib.import_module('yaml')", answer)
                await conversation.turn("重建已经成功了。")
                self.assertEqual(conversation.state["route"], "image_wait_result")
                self.assertEqual(conversation.state["pending"], "verify_import")
        self.assertEqual(conversation.state["base"], "python:3.11-slim")
        self.assertEqual(conversation.state["manifest"], "requirements.txt")
        self.assertEqual([entry["result"] for entry in conversation.state["history"]],
                         ["copy_failed", "build_ok", "missing", "import_ok", "startup_ok"])

        followup = Conversation(agent, case="image")
        await followup.turn("No module named 'unknownlib'")
        await followup.turn("python:3.11-slim")
        await followup.turn("没有依赖清单")
        self.assertEqual(followup.state["route"], "image_ask_distribution")
        await followup.turn("包名是 unknown-lib")
        self.assertEqual(followup.state["route"], "image_patch")
        await followup.turn("镜像重建失败")
        self.assertEqual(followup.state["route"], "image_ask_failure")
        await followup.turn("ERROR: No matching distribution found for unknown-lib")
        self.assertEqual(followup.state["route"], "image_check_pip")
        self.assertEqual(len(followup.state["history"]), 1)
        summary = await followup.turn("重新构建还是 pip 安装失败")
        self.assertEqual(followup.state["route"], "image_support")
        self.assertIn("检查依赖安装并重建", summary)

    def test_image_model_validation_rejects_shell_fragments(self):
        observation = {"module": "yaml", "base": "python:3.11-slim", "manifest": "requirements.txt",
                       "declared": False, "distribution": None, "event": "missing"}
        self.assertEqual(parse_image_observation(json.dumps(observation)), observation)
        for field, value in (("manifest", "../requirements.txt"), ("module", "yaml; print(1)"),
                             ("distribution", "--index-url"), ("base", "python:3.11-slim\nRUN anything")):
            with self.assertRaises(ValueError):
                parse_image_observation(json.dumps({**observation, field: value}))


if __name__ == "__main__":
    unittest.main()
