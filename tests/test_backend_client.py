"""裝置端後端客戶端：指令套用、後端離線/卡住都不能拖慢主迴圈。"""
import json
import os
import sys
import tempfile
import shutil
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_llm import FakeLLM, free_port  # noqa: E402

from pet.actuators import ActuatorHub, LoggingActuator  # noqa: E402
from pet.app import PetApp  # noqa: E402
from pet.backend_client import directive_to_event  # noqa: E402
from pet.events import make_event  # noqa: E402


class FakeBackend:
    """回固定指令的假後端；hang>0 時每個請求先卡住幾秒。"""

    def __init__(self, directives=None, hang=0.0):
        self.requests = []
        fb = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                fb.requests.append((self.path, json.loads(self.rfile.read(n))))
                if hang:
                    time.sleep(hang)
                data = json.dumps({"ok": True, "directives": directives or []}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def make_app(url, **opts):
    act = LoggingActuator()
    app = PetApp(use_clock=False, eye_seed=0, actuators=ActuatorHub([act]), backend_url=url,
                 pet_id="test-pet", backend_opts=dict({"heartbeat_s": 0, "timeout": 0.5, "talk_timeout": 1.0}, **opts))
    app.logger_actuator = act
    app.start()
    return app


def pump(app, until, seconds=3.0, dt=0.05):
    end = time.time() + seconds
    while time.time() < end:
        app.step(dt)
        if until():
            return True
        time.sleep(0.01)
    return False


class DirectiveConversionTest(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(directive_to_event({"type": "set_state", "state": "sleep"})["name"], "set_state")
        self.assertEqual(directive_to_event({"type": "set_stage", "stage": "2"})["stage"], 2)
        e = directive_to_event({"type": "emotion", "emotion": "shy"})
        self.assertEqual(e["params"]["expression"], "squint")
        e_lv = directive_to_event({"type": "emotion", "emotion": "sad", "level": 2})
        self.assertEqual((e_lv["type"], e_lv["name"], e_lv["state"]), ("control", "set_state", "sad_2"))
        e_shy = directive_to_event({"type": "emotion", "emotion": "shy", "level": 1})
        self.assertEqual((e_shy["type"], e_shy["name"], e_shy["state"]), ("control", "set_state", "shy"))
        e_cap = directive_to_event({"type": "emotion", "emotion": "annoyed", "level": 3})
        self.assertEqual(e_cap["state"], "annoyed_2")
        self.assertEqual(directive_to_event({"type": "actuator", "motion": "nod"})["params"], {"motion": "nod"})
        self.assertIsNone(directive_to_event({"type": "say", "text": "  "}))
        self.assertIsNone(directive_to_event({"type": "rm -rf"}))
        self.assertIsNone(directive_to_event({"type": "emotion", "emotion": "angry"}))
        self.assertIsNone(directive_to_event({"type": "emotion", "emotion": "angry", "level": 1}))
        self.assertIsNone(directive_to_event({"type": "emotion", "emotion": "sad", "level": 0}))

    def test_emotion_levels_single_source_of_truth(self):
        from pet.backend_client import EMOTION_MAX_LEVEL, load_emotion_levels
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config", "states.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        expected = cfg["emotions"]["levels"]
        self.assertEqual(EMOTION_MAX_LEVEL, expected)
        self.assertEqual(load_emotion_levels(), expected)

    def test_directive_with_custom_max_levels(self):
        custom = {"happy": 2, "custom": 3}
        e_capped = directive_to_event({"type": "emotion", "emotion": "happy", "level": 3}, max_levels=custom)
        self.assertEqual((e_capped["type"], e_capped["name"], e_capped["state"]), ("control", "set_state", "happy_2"))
        e_custom = directive_to_event({"type": "emotion", "emotion": "custom", "level": 2}, max_levels=custom)
        self.assertEqual((e_custom["type"], e_custom["name"], e_custom["state"]), ("control", "set_state", "custom_2"))
        self.assertIsNone(directive_to_event({"type": "emotion", "emotion": "custom", "level": 2}))

    def test_load_emotion_levels_failure_logs_warning(self):
        from pet.backend_client import load_emotion_levels
        with self.assertLogs("pet.backend", level="WARNING") as cm:
            res = load_emotion_levels("/non/existent/path/states.json")
            self.assertEqual(res, {})
            self.assertTrue(any("載入情緒等級設定失敗" in msg for msg in cm.output))

        tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        try:
            tmp.write("{corrupt_json")
            tmp.close()
            with self.assertLogs("pet.backend", level="WARNING") as cm:
                res = load_emotion_levels(tmp.name)
                self.assertEqual(res, {})
                self.assertTrue(any("載入情緒等級設定失敗" in msg for msg in cm.output))
        finally:
            os.unlink(tmp.name)

        # JSON 有效但無 emotions.levels 時，回傳 {} 且輸出 warning（必修 3）
        tmp_no_levels = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        try:
            json.dump({"states": {"idle": {}}}, tmp_no_levels)
            tmp_no_levels.close()
            with self.assertLogs("pet.backend", level="WARNING") as cm:
                res = load_emotion_levels(tmp_no_levels.name)
                self.assertEqual(res, {})
                self.assertTrue(any("未設定 emotions.levels" in msg for msg in cm.output))
        finally:
            os.unlink(tmp_no_levels.name)

    def test_pet_config_environment_variable_override(self):
        from pet.backend_client import load_emotion_levels, get_default_emotion_levels
        import pet.backend_client as bc
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        try:
            json.dump({"emotions": {"levels": {"env_mood": 4}}}, tmp)
            tmp.close()
            old = os.environ.get("PET_CONFIG")
            try:
                os.environ["PET_CONFIG"] = tmp.name
                self.assertEqual(load_emotion_levels(), {"env_mood": 4})
                self.assertEqual(get_default_emotion_levels(), {"env_mood": 4})
                self.assertEqual(bc.EMOTION_MAX_LEVEL, {"env_mood": 4})
            finally:
                if old is not None:
                    os.environ["PET_CONFIG"] = old
                else:
                    os.environ.pop("PET_CONFIG", None)
        finally:
            os.unlink(tmp.name)

    def test_get_default_emotion_levels_caches_result(self):
        from pet.backend_client import get_default_emotion_levels, emotion_state_name
        import pet.backend_client as bc
        bc._DEFAULT_LEVELS_CACHE = None
        levels1 = get_default_emotion_levels()
        levels2 = get_default_emotion_levels()
        self.assertIs(levels1, levels2)
        # emotion_state_name 在 max_levels is None 時使用快取，不重複讀檔
        st1 = emotion_state_name("happy", 2)
        st2 = emotion_state_name("happy", 2)
        self.assertEqual(st1, "happy_2")
        self.assertEqual(st2, "happy_2")


class DeviceBackendTest(unittest.TestCase):
    def test_directives_applied_through_app(self):
        fb = FakeBackend([{"type": "say", "text": "好舒服～", "seconds": 3},
                          {"type": "emotion", "emotion": "happy"},
                          {"type": "actuator", "motion": "shake"},
                          {"type": "set_state", "state": "sleep"}])
        app = make_app(fb.url)
        try:
            app.post(make_event("touch", "pat", source="test"))
            ok = pump(app, lambda: app.fsm.state == "sleep" and app.renderer.bubble.text == "好舒服～")
            self.assertTrue(ok, app.status())
            self.assertTrue(any("shake" in h for h in app.logger_actuator.history))
            self.assertEqual(app.status()["backend"]["online"], True)
            # 送出去的事件帶 pet_id、目前狀態
            path, body = fb.requests[0]
            self.assertEqual(path, "/v1/pet/event")
            self.assertEqual(body["pet_id"], "test-pet")
            self.assertEqual(body["event"]["name"], "pat")
            self.assertEqual(body["state"], "happy_1")
            frame = app.render()                 # 有泡泡也能正常繪圖
            self.assertEqual(frame.size, (800, 480))
        finally:
            app.stop()
            fb.close()

    def test_emotion_directive_with_and_without_level(self):
        fb_lv = FakeBackend([{"type": "emotion", "emotion": "sad", "level": 2}])
        app = make_app(fb_lv.url)
        try:
            app.post(make_event("touch", "press", source="test"))
            self.assertTrue(pump(app, lambda: app.fsm.state == "sad_2"), app.status())
            self.assertIsNone(app.eyes._override)
        finally:
            app.stop()
            fb_lv.close()

        fb_nolv = FakeBackend([{"type": "emotion", "emotion": "sad", "seconds": 3}])
        app2 = make_app(fb_nolv.url)
        try:
            app2.post(make_event("touch", "press", source="test"))
            self.assertTrue(pump(app2, lambda: app2.eyes._override is not None), app2.status())
            self.assertEqual(app2.fsm.state, "happy")      # 沒帶 level：只換眼睛，不改情緒強度狀態
            self.assertEqual(app2.eyes._override[0], "sad")
        finally:
            app2.stop()
            fb_nolv.close()

    def test_backend_directives_not_echoed_back(self):
        fb = FakeBackend([{"type": "set_state", "state": "bored"}])
        app = make_app(fb.url)
        try:
            app.post(make_event("touch", "poke", source="test"))
            pump(app, lambda: app.fsm.state == "bored")
            pump(app, lambda: False, seconds=0.5)
            names = [b["event"]["name"] for p, b in fb.requests]
            # poke + 兩次 state_changed（happy、bored）是裝置自己的通知；不會把 set_state 指令回送
            self.assertNotIn("set_state", names)
        finally:
            app.stop()
            fb.close()

    def test_talk_end_to_end_with_backend_and_fake_llm(self):
        from backend.config import load_config
        from backend.server import Brain, serve_in_thread
        llm = FakeLLM("valid")
        tmp = tempfile.mkdtemp()
        cfg = load_config(overrides={"data_dir": tmp, "chatter": {"enabled": False},
                                     "llm": {"local": {"base_url": llm.base_url, "timeout_s": 2}}})
        httpd, port = serve_in_thread(Brain(cfg))
        app = make_app(f"http://127.0.0.1:{port}")
        try:
            app.post(make_event("talk", "talk", source="test", text="你好"))
            ok = pump(app, lambda: app.renderer.bubble.text == "你好呀～")
            self.assertTrue(ok, app.status())
            self.assertEqual(app.eyes._override[0] if app.eyes._override else None, "happy")
        finally:
            app.stop()
            httpd.shutdown()
            httpd.server_close()
            llm.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_offline_backend_does_not_block_loop(self):
        app = make_app(f"http://127.0.0.1:{free_port()}")
        try:
            t0 = time.perf_counter()
            for i in range(60):
                app.post(make_event("touch", "pat" if i % 2 else "poke", source="test"))
                app.step(0.05)
                app.render()
            per_frame = (time.perf_counter() - t0) / 60
            self.assertLess(per_frame, 0.05)
            pump(app, lambda: app.status()["backend"]["online"] is False)
            self.assertFalse(app.status()["backend"]["online"])
            # 離線時對話：本機回一句
            app.post(make_event("talk", "talk", source="test", text="在嗎"))
            self.assertTrue(pump(app, lambda: (app.renderer.bubble.text or "").startswith("嗯…")))
        finally:
            app.stop()

    def test_hanging_backend_does_not_block_loop(self):
        fb = FakeBackend([], hang=3.0)
        app = make_app(fb.url)
        try:
            t0 = time.perf_counter()
            worst = 0.0
            for i in range(40):
                f0 = time.perf_counter()
                app.post(make_event("touch", "pat", source="test"))
                app.step(0.05)
                app.render()
                worst = max(worst, time.perf_counter() - f0)
            self.assertLess(worst, 0.1)
            self.assertLess(time.perf_counter() - t0, 2.0)
            self.assertLessEqual(app.backend.q.qsize(), app.backend.q.maxsize)
        finally:
            app.stop()
            fb.close()

    def test_no_backend_talk_shows_hint(self):
        app = PetApp(use_clock=False, eye_seed=0)
        app.post(make_event("talk", "talk", source="test", text="哈囉"))
        app.step(0.05)
        self.assertEqual(app.renderer.bubble.text, "（還沒接上大腦）")
        self.assertIsNone(app.status()["backend"])

    def test_backend_client_max_levels_passed_from_app_config(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config", "states.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg["emotions"]["levels"] = {"custom_emo": 2, "happy": 1}
        cfg["emotions"].setdefault("accept", {}).setdefault("*", []).append("custom_emo")
        cfg["states"]["custom_emo_1"] = dict(cfg["states"]["happy_1"])
        cfg["states"]["custom_emo_2"] = dict(cfg["states"]["happy_2"])

        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        try:
            json.dump(cfg, tmp, ensure_ascii=False)
            tmp.close()
            app = PetApp(config_path=tmp.name, use_clock=False, eye_seed=0,
                         backend_url="http://127.0.0.1:9999", backend_opts={"heartbeat_s": 0})
            try:
                self.assertIsNotNone(app.backend)
                self.assertEqual(app.backend.max_levels, {"custom_emo": 2, "happy": 1})

                events = []
                app.bus.subscribe("control", events.append, owner="test")
                app.backend.apply([{"type": "emotion", "emotion": "custom_emo", "level": 2}])
                app.bus.dispatch_pending()
                self.assertTrue(any(ev.get("state") == "custom_emo_2" for ev in events))
                self.assertEqual(app.fsm.state, "custom_emo_2")

                # happy 上限為 1，level 3 應被截斷為 happy（單一等級不加 _1）
                app.backend.apply([{"type": "emotion", "emotion": "happy", "level": 3}])
                app.bus.dispatch_pending()
                self.assertTrue(any(ev.get("state") == "happy" for ev in events))
                self.assertEqual(app.fsm.state, "happy")
            finally:
                app.stop()
        finally:
            os.unlink(tmp.name)

    def test_app_without_emotions_falls_back_with_warning(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config", "states.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        del cfg["emotions"]

        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        try:
            json.dump(cfg, tmp, ensure_ascii=False)
            tmp.close()
            with self.assertLogs("pet.app", level="WARNING") as cm:
                app = PetApp(config_path=tmp.name, use_clock=False, eye_seed=0,
                             backend_url="http://127.0.0.1:9999", backend_opts={"heartbeat_s": 0})
            try:
                self.assertTrue(any("未設定 emotions.levels" in msg for msg in cm.output))
                self.assertIsNone(app.backend.max_levels)

                # apply 帶等級的情緒指令退回預設等級，不會回傳 0 且被丟掉
                applied = app.backend.apply([{"type": "emotion", "emotion": "happy", "level": 2}])
                self.assertEqual(applied, 1)
                app.bus.dispatch_pending()
                # 驗證 FSM 最終狀態切換至 happy_2
                self.assertEqual(app.fsm.state, "happy_2")
            finally:
                app.stop()
        finally:
            os.unlink(tmp.name)

    def test_sleep_rejects_happy_emotion_directive(self):
        fb = FakeBackend([{"type": "emotion", "emotion": "happy", "level": 2}])
        app = make_app(fb.url)
        try:
            app.fsm.force("sleep")
            self.assertEqual(app.fsm.state, "sleep")
            app.backend.apply([{"type": "emotion", "emotion": "happy", "level": 2}])
            app.bus.dispatch_pending()
            # sleep 中不接受 happy（只接受 surprised），必須維持 sleep
            self.assertEqual(app.fsm.state, "sleep")
        finally:
            app.stop()
            fb.close()

    def test_backend_client_direct_instantiation(self):
        from pet.backend_client import BackendClient
        events = []
        client = BackendClient("http://127.0.0.1:9999", "pet-1", events.append,
                               max_levels={"custom": 2})
        self.assertEqual(client.max_levels, {"custom": 2})
        client.apply([{"type": "emotion", "emotion": "custom", "level": 2}])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["state"], "custom_2")


if __name__ == "__main__":
    unittest.main()
