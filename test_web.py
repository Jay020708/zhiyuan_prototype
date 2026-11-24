import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from web_server import COOKIE, create_web_app


class WebTest(unittest.TestCase):
    def test_web_demos_manual_turns_refresh_and_session_isolation(self):
        with patch.dict(os.environ, {"ZHIYUAN_EMBEDDING_BASE_URL": ""}), TestClient(create_web_app("")) as client:
            home = client.get("/")
            self.assertEqual(home.status_code, 200)
            self.assertIn("推荐问题", home.text)
            self.assertNotIn("排查进度", home.text)
            self.assertIn("default-src 'self'", home.headers["content-security-policy"])
            self.assertEqual(client.get("/app.js").status_code, 200)
            self.assertEqual(client.get("/styles.css").status_code, 200)
            self.assertEqual(client.get("/api/session").json()["turns"], [])

            api = client.post("/api/demo/api").json()["frames"]
            self.assertEqual(len(api), 7)
            self.assertIn("鉴权", api[4]["state"]["pending"])
            self.assertIn("401", api[4]["state"]["history"][0])
            self.assertEqual(api[-1]["state"]["status"], "resolved")
            first_cookie = client.cookies.get(COOKIE)
            self.assertEqual(len(client.get("/api/session").json()["turns"]), 7)

            image = client.post("/api/demo/image").json()["frames"]
            self.assertEqual(len(image), 8)
            self.assertIn("Dockerfile", image[2]["turn"]["assistant"])
            self.assertEqual(image[4]["state"]["pending"], "在新镜像中验证导入")
            self.assertEqual(len(image[-1]["state"]["history"]), 5)
            self.assertNotEqual(first_cookie, client.cookies.get(COOKIE))

            other_browser = TestClient(client.app)
            self.assertEqual(other_browser.get("/api/session").json()["turns"], [])
            other_browser.close()
            self.assertEqual(client.post("/api/reset").status_code, 200)
            self.assertEqual(client.get("/api/session").json()["turns"], [])

            initial = client.post("/api/turn", json={"message": "连接超时了"})
            self.assertIn("httponly", client.post("/api/reset").headers["set-cookie"].lower())
            self.assertEqual(initial.json()["state"]["pending"], "确认请求的运行位置")
            client.post("/api/turn", json={"message": "我在校外，API 超时"})
            vpn = client.post("/api/turn", json={"message": "没有"}).json()
            self.assertEqual(vpn["state"]["facts"]["VPN"], "未连接")
            changed = client.post("/api/turn", json={"message": "连上 VPN 后返回 401，API_KEY=fake-secret"})
            self.assertNotIn("fake-secret", changed.text)
            restored = client.get("/api/session").json()
            self.assertEqual(restored["state"]["pending"], "本地核对鉴权后重试")
            self.assertEqual(len(restored["turns"]), 3)
            self.assertEqual(changed.headers["cache-control"], "no-store")

            forbidden = client.post("/api/reset", headers={"Origin": "https://outside.example"})
            self.assertEqual(forbidden.status_code, 403)
            self.assertEqual(len(client.get("/api/session").json()["turns"]), 3)

    def test_recommended_questions_are_available_without_model_or_knowledge(self):
        with patch.dict(os.environ, {"ZHIYUAN_EMBEDDING_BASE_URL": ""}), TestClient(create_web_app("")) as client:
            faq = client.get("/faq.json").json()
            self.assertEqual(len(faq), 3)
            public_turns = []
            for entry in faq:
                result = client.post("/api/turn?view=public", json={"message": entry["question"]})
                self.assertEqual(result.status_code, 200)
                turn = {"user": entry["question"], "assistant": entry["answer"], "sources": []}
                self.assertEqual(result.json(), {"turn": turn})
                public_turns.append(turn)

            def ask(message):
                result = client.post("/api/turn?view=public", json={"message": message})
                self.assertEqual(result.status_code, 200)
                turn = result.json()["turn"]
                self.assertEqual(set(turn), {"user", "assistant", "sources"})
                public_turns.append(turn)
                return turn["assistant"]

            self.assertIn("镜像管理", ask("ModelArts 里选不到我上传的镜像，怎么办？"))
            self.assertIn("报错", ask("部署失败了"))
            self.assertIn("运行位置", ask("我的模型 API 调用连接超时了"))
            self.assertIn("重试", ask("在校外，没连 VPN"))
            self.assertEqual(client.get("/api/session?view=public").json(), {"turns": public_turns})
            self.assertEqual(client.post("/api/reset").status_code, 200)
            self.assertEqual(client.get("/api/session?view=public").json(), {"turns": []})


if __name__ == "__main__":
    unittest.main()
