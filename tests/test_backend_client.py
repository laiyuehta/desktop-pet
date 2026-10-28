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
        self.assertEqual(directive_to_event({"type": "actuator", "motion": "nod"})["params"], {"motion": "nod"})
        self.assertIsNone(directive_to_event({"type": "say", "text": "  "}))
        self.assertIsNone(directive_to_event({"type": "rm -rf"}))
        self.assertIsNone(directive_to_event({"type": "emotion", "emotion": "angry"}))


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
            self.assertEqual(body["state"], "happy")
            frame = app.render()                 # 有泡泡也能正常繪圖
            self.assertEqual(frame.size, (800, 480))
        finally:
            app.stop()
            fb.close()

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


if __name__ == "__main__":
    unittest.main()
