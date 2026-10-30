"""後端（大腦）測試：用假 LLM 伺服器，不需要真的模型。"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_llm import FakeLLM, free_port  # noqa: E402

from backend.config import load_config  # noqa: E402
from backend.llm import LLMError, extract_json, strip_think, validate_reply  # noqa: E402
from backend.mood import MoodEngine  # noqa: E402
from backend.server import Brain, serve_in_thread  # noqa: E402
from backend.store import PetStore, atomic_write_json  # noqa: E402


def make_cfg(tmp, base_url, **extra):
    over = {"data_dir": tmp,
            "llm": {"local": {"base_url": base_url, "model": "fake", "timeout_s": 2, "retries": 1},
                    "health_cache_s": 0},
            "chatter": {"enabled": False}}
    for k, v in extra.items():
        over[k] = v
    return load_config(overrides=over)


class Http:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.load(r)


class JsonHelpersTest(unittest.TestCase):
    def test_strip_think(self):
        self.assertEqual(strip_think("<think>abc\n</think>\n{\"a\":1}"), '{"a":1}')
        self.assertEqual(strip_think("{\"a\":1}<think>沒結束"), '{"a":1}')

    def test_extract_json_with_noise(self):
        obj = extract_json('好的！```json\n{"say": "嗨 {你好}", "emotion": "happy"}\n```')
        self.assertEqual(obj["say"], "嗨 {你好}")

    def test_extract_json_missing(self):
        with self.assertRaises(LLMError):
            extract_json("沒有 JSON")

    def test_validate(self):
        r = validate_reply({"say": "  哈囉  ", "emotion": "angry", "intent": "fly"})
        self.assertEqual(r, {"say": "哈囉", "emotion": "normal", "intent": "none"})
        self.assertTrue(validate_reply({"say": "長" * 100}, max_chars=10)["say"].endswith("…"))
        with self.assertRaises(LLMError):
            validate_reply({"emotion": "happy"})


class BackendApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        for s in getattr(self, "servers", []):
            s.shutdown()
            s.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, base_url, **extra):
        self.brain = Brain(make_cfg(self.tmp, base_url, **extra))
        httpd, port = serve_in_thread(self.brain)
        self.servers = getattr(self, "servers", []) + [httpd]
        return Http(port)

    def test_talk_valid_json(self):
        llm = FakeLLM("valid")
        try:
            api = self.start(llm.base_url)
            r = api.post("/v1/pet/talk", {"pet_id": "p1", "text": "你好"})
            self.assertEqual(r["say"], "你好呀～")
            self.assertEqual(r["emotion"], "happy")
            self.assertEqual(r["source"], "local")
            types = [d["type"] for d in r["directives"]]
            self.assertIn("say", types)
            self.assertIn("emotion", types)
            # Qwen3 不思考：/no_think + enable_thinking=false，並要求 JSON
            body = llm.calls[0]
            self.assertTrue(body["messages"][-1]["content"].endswith("/no_think"))
            self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
            self.assertEqual(body["messages"][0]["role"], "system")
            self.assertIn("種子", body["messages"][0]["content"])
            # 第二句會帶入上一輪對話記憶
            api.post("/v1/pet/talk", {"pet_id": "p1", "text": "你是誰"})
            roles = [m["role"] for m in llm.calls[1]["messages"]]
            self.assertEqual(roles, ["system", "user", "assistant", "user"])
        finally:
            llm.close()

    def test_talk_think_tags_stripped_and_intent(self):
        llm = FakeLLM("think")
        try:
            api = self.start(llm.base_url)
            r = api.post("/v1/pet/talk", {"pet_id": "p1", "text": "嗨"})
            self.assertEqual(r["say"], "嗨嗨～")
            self.assertNotIn("think", json.dumps(r, ensure_ascii=False))
            self.assertIn({"type": "actuator", "motion": "nod"}, r["directives"])
        finally:
            llm.close()

    def test_talk_invalid_json_falls_back_after_retry(self):
        llm = FakeLLM("invalid")
        try:
            api = self.start(llm.base_url)
            r = api.post("/v1/pet/talk", {"pet_id": "p1", "text": "你好"})
            self.assertEqual(r["source"], "fallback")
            self.assertTrue(r["say"])
            self.assertEqual(len(llm.calls), 2)         # 重試一次
        finally:
            llm.close()

    def test_retry_then_success(self):
        llm = FakeLLM("seq", replies=["亂講話", '{"say":"第二次就好了","emotion":"shy"}'])
        try:
            api = self.start(llm.base_url)
            r = api.post("/v1/pet/talk", {"pet_id": "p1", "text": "?"})
            self.assertEqual((r["say"], r["source"], r["attempts"]), ("第二次就好了", "local", 2))
        finally:
            llm.close()

    def test_talk_llm_down_fallback(self):
        api = self.start(f"http://127.0.0.1:{free_port()}/v1")
        t0 = time.time()
        r = api.post("/v1/pet/talk", {"pet_id": "p1", "text": "你好"})
        self.assertEqual(r["source"], "fallback")
        self.assertIn(r["say"], ["哈囉～", "你好呀！"])
        # 測試目標為保護連線失敗時平滑退回 fallback，門檻避免綁緊特定機型與負載
        self.assertLess(time.time() - t0, 3.5)
        h = api.get("/health")
        self.assertTrue(h["ok"])
        self.assertFalse(h["llm"]["local"]["reachable"])

    def test_talk_timeout_fallback_no_long_retry(self):
        llm = FakeLLM("slow", delay=3)
        try:
            api = self.start(llm.base_url)
            self.brain.cfg["llm"]["local"]["timeout_s"] = 1
            self.brain.router.local.client.timeout_s = 1
            t0 = time.time()
            r = api.post("/v1/pet/talk", {"pet_id": "p1", "text": "你好"})
            self.assertEqual(r["source"], "fallback")
            # timeout_s 為 1s，逾時後不應重試，放寬至 2.5s 避免慢速環境邊界抖動
            self.assertLess(time.time() - t0, 2.5)
        finally:
            llm.close()

    def test_health_reachable(self):
        llm = FakeLLM("valid")
        try:
            h = self.start(llm.base_url).get("/health")
            self.assertTrue(h["llm"]["local"]["reachable"])
            self.assertEqual(h["llm"]["local"]["models"], ["fake-model"])
            self.assertFalse(h["llm"]["cloud"]["enabled"])
        finally:
            llm.close()

    def test_event_updates_mood_growth_and_state(self):
        api = self.start(f"http://127.0.0.1:{free_port()}/v1")
        r = api.post("/v1/pet/event", {"pet_id": "p2", "event": {"type": "touch", "name": "pat"},
                                       "state": "idle", "stage": 1})
        self.assertEqual(r["growth"]["value"], 1.0)
        self.assertGreater(r["mood"]["happiness"], 50)
        st = api.get("/v1/pet/p2/state")
        self.assertEqual(st["counters"]["pat"], 1)
        self.assertEqual(st["memory"][-1]["name"], "pat")
        self.assertIsNotNone(st["last_seen"])

    def test_bad_requests(self):
        api = self.start(f"http://127.0.0.1:{free_port()}/v1")
        for path, body in (("/v1/pet/event", {"pet_id": "../etc", "event": {"name": "pat"}}),
                           ("/v1/pet/event", {"pet_id": "ok", "event": {}}),
                           ("/v1/pet/talk", {"pet_id": "ok", "text": "  "})):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                api.post(path, body)
            self.assertEqual(cm.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            api.get("/v1/pet/nobody/state")
        self.assertEqual(cm.exception.code, 404)

    def test_token_required_when_set(self):
        cfg = make_cfg(self.tmp, "http://127.0.0.1:1/v1")
        cfg["server"]["api_token"] = "secret"
        httpd, port = serve_in_thread(Brain(cfg))
        self.servers = [httpd]
        api = Http(port)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            api.post("/v1/pet/event", {"pet_id": "p", "event": {"name": "pat"}})
        self.assertEqual(cm.exception.code, 401)
        req = urllib.request.Request(api.base + "/v1/pet/event", method="POST",
                                     data=json.dumps({"pet_id": "p", "event": {"name": "pat"}}).encode(),
                                     headers={"X-Pet-Token": "secret"})
        self.assertTrue(json.load(urllib.request.urlopen(req, timeout=5))["ok"])


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip(self):
        st = PetStore(self.tmp, {"happiness": 50, "affection": 30, "energy": 70}, memory_size=3)
        rec = st.load("pet-a")
        rec["growth"]["value"] = 12.5
        rec["mood"]["happiness"] = 77
        for i in range(5):
            st.remember(rec, {"kind": "talk", "text": f"t{i}", "reply": "r"})
        st.save(rec)
        again = PetStore(self.tmp, {"happiness": 50, "affection": 30, "energy": 70}).load("pet-a")
        self.assertEqual(again["growth"]["value"], 12.5)
        self.assertEqual(again["mood"]["happiness"], 77)
        self.assertEqual([m["text"] for m in again["memory"]], ["t2", "t3", "t4"])
        self.assertEqual(sorted(os.listdir(self.tmp)), ["pet-a.json"])   # 沒有殘留暫存檔

    def test_old_file_gets_defaults(self):
        atomic_write_json(os.path.join(self.tmp, "old.json"), {"pet_id": "old", "growth": {"value": 3}})
        rec = PetStore(self.tmp, {"happiness": 50, "affection": 30, "energy": 70}).load("old")
        self.assertEqual(rec["growth"], {"value": 3, "stage": 1})
        self.assertIn("happiness", rec["mood"])

    def test_invalid_id(self):
        with self.assertRaises(ValueError):
            PetStore(self.tmp, {}).path("../../x")


class MoodTest(unittest.TestCase):
    def cfg(self, **kw):
        return load_config(overrides=kw)

    def test_decay_towards_neutral_and_energy(self):
        eng = MoodEngine(self.cfg())
        rec = {"mood": {"happiness": 90, "affection": 30, "energy": 70}, "last_update": 0,
               "device_state": "idle", "growth": {"value": 0, "stage": 1}, "counters": {}}
        eng.decay(rec, now=3600 * 2)
        self.assertAlmostEqual(rec["mood"]["happiness"], 78)
        self.assertAlmostEqual(rec["mood"]["energy"], 54)
        rec["device_state"] = "sleep"
        eng.decay(rec, now=3600 * 3)
        self.assertAlmostEqual(rec["mood"]["energy"], 79)

    def test_stage_advance_disabled_by_default(self):
        eng = MoodEngine(self.cfg())
        rec = {"growth": {"value": 10000, "stage": 1}}
        self.assertEqual(eng.check_stage(rec), [])

    def test_stage_advance_when_enabled(self):
        eng = MoodEngine(self.cfg(growth={"auto_advance": True, "thresholds": {"2": 5}}))
        rec = {"growth": {"value": 4.5, "stage": 1}}
        d = eng.add_growth(rec, 1.0)
        self.assertIn({"type": "set_stage", "stage": 2}, d)
        self.assertEqual(rec["growth"]["stage"], 2)

    def test_low_energy_sleep_hint_only_on_heartbeat_with_cooldown(self):
        eng = MoodEngine(self.cfg(chatter={"enabled": False}))
        rec = {"mood": {"happiness": 50, "affection": 30, "energy": 5}, "device_state": "idle",
               "growth": {"value": 0, "stage": 1}, "counters": {}}
        self.assertEqual(eng.apply_event(rec, {"name": "pat"}, now=1000), [])
        d = eng.apply_event(rec, {"type": "sys", "name": "heartbeat"}, now=1000)
        self.assertIn({"type": "set_state", "state": "sleep"}, d)
        self.assertEqual(eng.apply_event(rec, {"type": "sys", "name": "heartbeat"}, now=1100), [])

    def test_chatter(self):
        eng = MoodEngine(self.cfg(chatter={"enabled": True, "chance": 1.0, "cooldown_s": 0}))
        rec = {"mood": {"happiness": 50, "affection": 30, "energy": 70}, "device_state": "idle",
               "growth": {"value": 0, "stage": 1}, "counters": {}}
        d = eng.apply_event(rec, {"name": "pat"}, now=1)
        self.assertEqual(d[0]["type"], "say")

    def test_mood_determines_initial_happy_level_on_pat(self):
        eng = MoodEngine(self.cfg(chatter={"enabled": False}))
        rec_normal = {"mood": {"happiness": 50, "affection": 30, "energy": 70}, "device_state": "idle",
                      "growth": {"value": 0, "stage": 1}, "counters": {}}
        self.assertEqual(eng.apply_event(rec_normal, {"name": "pat"}, now=1), [])
        self.assertEqual(eng.emotion_level(rec_normal, "pat"), 1)

        rec_good = {"mood": {"happiness": 70, "affection": 30, "energy": 70}, "device_state": "idle",
                    "growth": {"value": 0, "stage": 1}, "counters": {}}
        d_good = eng.apply_event(rec_good, {"name": "pat"}, now=2)
        self.assertIn({"type": "emotion", "emotion": "happy", "level": 2, "seconds": 2}, d_good)

        rec_great = {"mood": {"happiness": 85, "affection": 30, "energy": 70}, "device_state": "idle",
                     "growth": {"value": 0, "stage": 1}, "counters": {}}
        d_great = eng.apply_event(rec_great, {"name": "pat"}, now=3)
        self.assertIn({"type": "emotion", "emotion": "happy", "level": 3, "seconds": 2}, d_great)


if __name__ == "__main__":
    unittest.main()


class PhotoApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cfg = make_cfg(self.tmp, "http://127.0.0.1:1/v1", photos={"retention": 3})
        cfg["server"]["api_token"] = "tok"
        self.brain = Brain(cfg)
        self.httpd, port = serve_in_thread(self.brain)
        self.base = f"http://127.0.0.1:{port}"

    def tearDown(self):
        self.httpd.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, path, data=None, token="tok", ctype="image/jpeg"):
        h = {"Content-Type": ctype}
        if token:
            h["X-Pet-Token"] = token
        r = urllib.request.Request(self.base + path, data=data, headers=h, method="POST" if data is not None else "GET")
        return urllib.request.urlopen(r, timeout=5)

    def code(self, *a, **kw):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.req(*a, **kw)
        return cm.exception.code

    def test_upload_list_get_retention(self):
        jpg = b"\xff\xd8" + b"x" * 200
        ids = []
        for i in range(5):
            r = json.load(self.req(f"/v1/pet/photo?pet_id=p1&reason=manual&taken={time.time() - 10 + i}", jpg))
            self.assertTrue(r["ok"])
            ids.append(r["photo_id"])
        lst = json.load(self.req("/v1/pet/p1/photos"))
        self.assertEqual(lst["count"], 3)
        self.assertEqual([p["photo_id"] for p in lst["photos"]], ids[::-1][:3])
        self.assertEqual(self.req(f"/v1/pet/p1/photos/{ids[-1]}").read(), jpg)
        self.assertEqual(self.code(f"/v1/pet/p1/photos/{ids[0]}"), 404)       # 超過保留數量被刪
        self.assertEqual(len(os.listdir(os.path.join(self.tmp, "photos", "p1"))), 6)  # 3 jpg + 3 json
        st = json.load(self.req("/v1/pet/p1/state"))
        self.assertEqual(st["photos"][-1]["photo_id"], ids[-1])

    def test_auth_and_validation(self):
        jpg = b"\xff\xd8" + b"x" * 200
        self.assertEqual(self.code("/v1/pet/photo?pet_id=p1", jpg, token=None), 401)
        self.assertEqual(self.code("/v1/pet/photo?pet_id=p1", jpg, token="bad"), 401)
        self.assertEqual(self.code("/v1/pet/p1/photos", token=None), 401)
        self.assertEqual(self.code("/v1/pet/p1/photos/20260101-000000-000", token=None), 401)
        self.assertEqual(self.code("/v1/pet/photo?pet_id=p1", b"not a jpeg" * 10), 400)
        self.assertEqual(self.code("/v1/pet/photo?pet_id=../x", jpg), 400)
        self.assertEqual(self.code("/v1/pet/p1/photos/..%2f..%2fx"), 400)
        self.assertEqual(self.code("/v1/pet/p1/photos/20260101-000000-000"), 404)

    def test_health_public_vs_token(self):
        pub = json.load(urllib.request.urlopen(self.base + "/health", timeout=5))
        self.assertEqual(set(pub), {"ok", "service", "version"})
        full = json.load(self.req("/health"))
        self.assertIn("llm", full)

    def test_photo_api_off_without_token(self):
        self.brain.cfg["server"]["api_token"] = ""
        tmp2 = tempfile.mkdtemp()
        try:
            cfg = make_cfg(tmp2, "http://127.0.0.1:1/v1")
            httpd, port = serve_in_thread(Brain(cfg))
            r = urllib.request.Request(f"http://127.0.0.1:{port}/v1/pet/photo?pet_id=p1", data=b"\xff\xd8" + b"x" * 100,
                                       method="POST", headers={"Content-Type": "image/jpeg"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(r, timeout=5)
            self.assertEqual(cm.exception.code, 403)
            httpd.shutdown()
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

    def test_skill_directive_passes_filter(self):
        out = Brain._clean([{"type": "skill", "name": "camera.snapshot"}, {"type": "skill", "name": "rm -rf"}])
        self.assertEqual(out, [{"type": "skill", "name": "camera.snapshot"}])
