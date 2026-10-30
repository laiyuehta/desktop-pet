"""prompts/anim 提示詞組合包與 tools/prompt_pack.py：組提示詞、解析、驗證、合併不改 repo、假 LLM 抽卡。"""
import http.server
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import prompt_pack as pp  # noqa: E402


def example_patch() -> dict:
    with open(os.path.join(pp.PACK_DIR, "context.md"), encoding="utf-8") as fh:
        return json.loads(re.findall(r"```json\n(.*?)```", fh.read(), re.S)[-1])


class PromptFilesTest(unittest.TestCase):
    def test_files_and_size(self):
        for f in ("README.md", "system.md", "context.md"):
            self.assertTrue(os.path.isfile(os.path.join(pp.PACK_DIR, f)), f)
        with open(os.path.join(pp.PACK_DIR, "context.md"), encoding="utf-8") as fh:
            self.assertLess(len(fh.read()), 18000)   # 約 < 6k tokens

    def test_all_tasks_build(self):
        tasks = [f[:-3] for f in os.listdir(os.path.join(pp.PACK_DIR, "tasks")) if f.endswith(".md")]
        self.assertGreaterEqual(len(tasks), 4)
        for t in tasks:
            system, user, missing = pp.build_prompt(t, {})
            self.assertIn("HARD RULES", system)
            self.assertTrue(missing, t)   # 範本都有佔位符
            _, user2, missing2 = pp.build_prompt(t, {k: "x" for k in missing})
            self.assertEqual(missing2, [])
            self.assertNotIn("{{", user2)

    def test_context_examples_match_real_config(self):
        sk = pp._load(pp.SKELETON)
        with open(os.path.join(pp.PACK_DIR, "context.md"), encoding="utf-8") as fh:
            blocks = re.findall(r"```json\n(.*?)```", fh.read(), re.S)
        a = json.loads(blocks[0])
        b = json.loads(blocks[1])
        real_a = {k: v for k, v in sk["animations"]["sad_1"].items() if k != "comment"}
        real_b = {k: v for k, v in sk["animations"]["happy_2"].items() if k != "comment"}
        self.assertEqual(a, real_a)
        self.assertEqual(b, real_b)


class ValidateTest(unittest.TestCase):
    def test_existing_emotion_animations_pass(self):
        sk = pp._load(pp.SKELETON)
        bones = [b["name"] for b in sk["bones"]]
        for n, a in sk["animations"].items():
            if n not in ("happy",):   # 舊 happy 有 0.12 秒的關鍵幀（早於 0.05 對齊規則）
                self.assertEqual(pp.check_animation(n, a, bones), [], n)

    def test_example_patch_valid_and_bakes(self):
        d = tempfile.mkdtemp()
        try:
            before = [open(p, "rb").read() for p in (pp.SKELETON, pp.STATES, pp.BAKE)]
            self.assertEqual(pp.validate(example_patch(), d, do_bake=True, log=lambda *_: None), [])
            self.assertTrue(os.path.isfile(os.path.join(d, "pack", "preview", "sleepy_1.gif")))
            self.assertEqual(before, [open(p, "rb").read() for p in (pp.SKELETON, pp.STATES, pp.BAKE)])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_errors_detected(self):
        p = example_patch()
        a = p["animations"]["sleepy_1"]
        a["tracks"]["body"]["y"][-1][1] = 99          # 頭尾不同 + 超出範圍
        a["tracks"]["tail"] = {"x": [[0, 0], [3.2, 0]]}  # 不存在的骨頭
        p["states"]["sleepy_1"]["eyes"] = "wink"
        p["bake_states"]["sleepy_1"]["variants"][1]["blinks"] = [9]
        p["transitions"] = []
        p["extra"] = 1
        err = "\n".join(pp.check_patch(p, pp._load(pp.SKELETON), pp._load(pp.STATES), pp._load(pp.BAKE)))
        for s in ("頭尾", "超出範圍", "tail", "eyes", "blink", "離開", "extra"):
            self.assertIn(s, err)

    def test_extract_json(self):
        raw = "<think>hmm {no}</think>好的\n```json\n{\"notes\": \"a\"}\n```\n"
        self.assertEqual(pp.extract_json(raw), {"notes": "a"})

    def test_anim_var(self):
        v = pp._vars(["CURRENT_JSON=@anim:sad_1"])
        self.assertIn("sad_1", json.loads(v["CURRENT_JSON"]))


class FakeLLM(http.server.BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeLLM.calls.append(body)
        good = len(FakeLLM.calls) % 2 == 1
        content = "```json\n" + json.dumps(example_patch()) + "\n```" if good else "抱歉我不會"
        out = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class GenTest(unittest.TestCase):
    def test_gen_with_fake_endpoint(self):
        srv = http.server.HTTPServer(("127.0.0.1", 0), FakeLLM)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        d = tempfile.mkdtemp()
        try:
            rc = pp.main(["gen", "--task", "variants", "-n", "2", "-o", d,
                          "--base-url", f"http://127.0.0.1:{srv.server_port}/v1", "--model", "m",
                          "--var", "STATE=sleepy_1", "--var", "BRIEF=b", "--var", "EYES=sleepy",
                          "--var", "DURATION=3.2", "--var", "ROUNDS=2", "--var", "EXTRA="])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(os.path.join(d, "v01.json")))
            self.assertFalse(os.path.isfile(os.path.join(d, "v02.json")))
            self.assertIn("#1", FakeLLM.calls[0]["messages"][1]["content"])
            self.assertEqual(pp.main(["validate-all", d]), 0)
        finally:
            srv.shutdown()
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
