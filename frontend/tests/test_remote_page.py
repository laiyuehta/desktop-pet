"""遙控頁（remote.html）script 檢查：重複宣告會讓整段 script 不執行（v2.3 螢幕畫面看不到的原因）。"""
import json
import os
import re
import shutil
import subprocess
import unittest

HTML = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pet", "web", "remote.html")
SMOKE = os.path.join(os.path.dirname(__file__), "js", "remote_smoke.js")


def script():
    with open(HTML, encoding="utf-8") as f:
        s = f.read()
    return s.split("<script>", 1)[1].split("</script>", 1)[0]


node = shutil.which("node")


class TestRemotePage(unittest.TestCase):
    def test_no_duplicate_top_level_declarations(self):
        names = re.findall(r"^(?:const|let|var|function|async function)\s+([A-Za-z_$][\w$]*)", script(), re.M)
        dup = sorted({n for n in names if names.count(n) > 1})
        self.assertFalse(dup, f"重複宣告：{dup}")

    def test_states_not_hardcoded_in_markup(self):
        with open(HTML, encoding="utf-8") as f:
            markup = f.read().split("<script>", 1)[0]
        self.assertNotIn("data-state=", markup)
        self.assertIn('id="states"', markup)

    @unittest.skipUnless(node, "沒有 node")
    def test_script_parses(self):
        p = subprocess.run([node, "--check", "-"], input=script(), capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(p.returncode, 0, p.stderr)

    @unittest.skipUnless(node, "沒有 node")
    def test_mirror_starts_mjpeg_stream(self):
        p = subprocess.run([node, SMOKE, HTML, "mjpeg"], capture_output=True, text=True, timeout=30, encoding="utf-8")
        r = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["srcLog"] and r["srcLog"][0] == "/screen.mjpg")
        self.assertTrue("動畫包" in r["info"] and "串流" in r["info"])
        self.assertEqual(r["screenJpgFetches"], 0)
        for st in ("idle", "happy", "bored", "sleep", "happy_1", "happy_3", "surprised_2", "sad_3", "shy", "annoyed_2"):
            self.assertIn(f'data-state="{st}"', r["statesHtml"])

    @unittest.skipUnless(node, "沒有 node")
    def test_mirror_falls_back_to_polling_when_mjpeg_shows_nothing(self):
        p = subprocess.run([node, SMOKE, HTML, "nomjpeg"], capture_output=True, text=True, timeout=30, encoding="utf-8")
        r = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["srcLog"][0] == "/screen.mjpg" and any(s.startswith("blob:") for s in r["srcLog"]))
        self.assertTrue(r["screenJpgFetches"] >= 5 and "輪詢" in r["info"])


if __name__ == "__main__":
    unittest.main()
