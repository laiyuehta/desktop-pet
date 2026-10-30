"""遙控頁（remote.html）script 檢查：重複宣告會讓整段 script 不執行（v2.3 螢幕畫面看不到的原因）。"""
import json
import os
import re
import shutil
import subprocess

import pytest

HTML = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "pet", "web", "remote.html")
SMOKE = os.path.join(os.path.dirname(__file__), "js", "remote_smoke.js")


def script():
    s = open(HTML, encoding="utf-8").read()
    return s.split("<script>", 1)[1].split("</script>", 1)[0]


def test_no_duplicate_top_level_declarations():
    names = re.findall(r"^(?:const|let|var|function|async function)\s+([A-Za-z_$][\w$]*)", script(), re.M)
    dup = sorted({n for n in names if names.count(n) > 1})
    assert not dup, f"重複宣告：{dup}"


def test_states_not_hardcoded_in_markup():
    markup = open(HTML, encoding="utf-8").read().split("<script>", 1)[0]
    assert 'data-state=' not in markup
    assert 'id="states"' in markup


node = shutil.which("node")


@pytest.mark.skipif(not node, reason="沒有 node")
def test_script_parses():
    p = subprocess.run([node, "--check", "-"], input=script(), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr


@pytest.mark.skipif(not node, reason="沒有 node")
def test_mirror_starts_mjpeg_stream():
    p = subprocess.run([node, SMOKE, HTML, "mjpeg"], capture_output=True, text=True, timeout=30)
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert r["ok"], r
    assert r["srcLog"] and r["srcLog"][0] == "/screen.mjpg"
    assert "動畫包" in r["info"] and "串流" in r["info"]
    assert r["screenJpgFetches"] == 0
    for st in ("idle", "happy", "bored", "sleep", "happy_1", "happy_3", "surprised_2", "sad_3", "shy", "annoyed_2"):
        assert f'data-state="{st}"' in r["statesHtml"]


@pytest.mark.skipif(not node, reason="沒有 node")
def test_mirror_falls_back_to_polling_when_mjpeg_shows_nothing():
    p = subprocess.run([node, SMOKE, HTML, "nomjpeg"], capture_output=True, text=True, timeout=30)
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert r["ok"], r
    assert r["srcLog"][0] == "/screen.mjpg" and any(s.startswith("blob:") for s in r["srcLog"])
    assert r["screenJpgFetches"] >= 5 and "輪詢" in r["info"]
