"""輸出（piscreen 協定）、事件匯流排、網頁遙控、外部控制器的整合測試。"""
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from pet.actuators import ActuatorHub, LoggingActuator  # noqa: E402
from pet.app import PetApp  # noqa: E402
from pet.events import make_event  # noqa: E402
from pet.extensions import EventBus, Extension, ExtensionManager  # noqa: E402
from pet.inputs import KeyboardInput, WebRemoteInput  # noqa: E402
from pet.outputs import PiscreenSink  # noqa: E402


class PiscreenProtocolTest(unittest.TestCase):
    def test_length_prefixed_baseline_jpeg(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "piscreen.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        got = []

        def serve():
            conn, _ = srv.accept()
            buf = b""
            while len(got) < 2:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 4:
                    n = struct.unpack(">I", buf[:4])[0]
                    if len(buf) < 4 + n:
                        break
                    got.append(buf[4:4 + n])
                    buf = buf[4 + n:]
            conn.close()

        th = threading.Thread(target=serve, daemon=True)
        th.start()
        sink = PiscreenSink(path)
        sink.open()
        sink.send(Image.new("RGB", (800, 480), (10, 20, 30)))
        sink.send(Image.new("RGB", (400, 240), (200, 20, 30)))   # 自動縮放成 800×480
        th.join(5)
        sink.close()
        srv.close()
        self.assertEqual(len(got), 2)
        for data in got:
            self.assertEqual(data[:2], b"\xff\xd8")
            self.assertIn(b"\xff\xc0", data)        # SOF0 = baseline
            self.assertNotIn(b"\xff\xc2", data)     # SOF2 = progressive
            im = Image.open(__import__("io").BytesIO(data))
            self.assertEqual(im.size, (800, 480))

    def test_missing_socket_drops_frames(self):
        sink = PiscreenSink("/nonexistent/piscreen.sock", retry_min=10)
        sink.send(Image.new("RGB", (800, 480)))
        self.assertEqual(sink.dropped, 1)


class BusAndExtensionTest(unittest.TestCase):
    def test_patterns(self):
        bus = EventBus()
        got = []
        bus.subscribe("touch", lambda e: got.append(("t", e["name"])))
        bus.subscribe("*:pat", lambda e: got.append(("p", e["name"])))
        bus.publish(make_event("touch", "pat"))
        bus.publish(make_event("button", "press"))
        bus.dispatch_pending()
        self.assertEqual(got, [("t", "pat"), ("p", "pat")])

    def test_extension_request_action(self):
        bus = EventBus()

        class Ext(Extension):
            name = "x"

            def start(self):
                self.subscribe("pet", lambda e: self.request_action("motion", motion="nod"))

        mgr = ExtensionManager(bus)
        mgr.register(Ext())
        mgr.start_all()
        actions = []
        bus.subscribe("action", actions.append)
        bus.publish(make_event("pet", "state_changed"))
        bus.dispatch_pending()
        bus.dispatch_pending()
        self.assertEqual(actions[0]["params"], {"motion": "nod"})
        mgr.stop_all()


class AppIntegrationTest(unittest.TestCase):
    def test_happy_triggers_nod_and_web_remote(self):
        act = LoggingActuator()
        app = PetApp(use_clock=False, eye_seed=0, actuators=ActuatorHub([act]))
        app.logger_actuator = act
        app.start()
        web = WebRemoteInput(app.post, "127.0.0.1", 0, status_fn=app.status)
        web.start()
        base = f"http://127.0.0.1:{web.port}"

        def post(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=5))

        try:
            self.assertTrue(post("/event", {"type": "touch", "name": "pat"})["ok"])
            app.step(0.05)
            self.assertEqual(app.fsm.state, "happy_1")
            self.assertTrue(post("/control", {"cmd": "set_state", "state": "happy"})["ok"])
            app.step(0.05)
            app.step(0.05)   # on_enter 的動作請求在下一格派送
            self.assertEqual(app.fsm.state, "happy")
            self.assertTrue(any("nod" in h for h in act.history))
            post("/actuator", {"motion": "wheels", "action": "left"})
            post("/control", {"cmd": "set_state", "state": "sleep"})
            app.step(0.05)
            self.assertEqual(app.fsm.state, "sleep")
            self.assertTrue(any("左轉" in h for h in act.history))
            post("/control", {"cmd": "set_stage", "stage": 2})
            app.step(0.05)
            st = json.load(urllib.request.urlopen(base + "/status", timeout=5))
            self.assertEqual(st["state"], "sleep")
            self.assertEqual(st["stage"], 2)
            html = urllib.request.urlopen(base + "/", timeout=5).read().decode("utf-8")
            self.assertIn("桌寵遙控", html)
            with self.assertRaises(urllib.error.HTTPError):
                post("/event", {"type": "touch"})     # 缺 name → 400
        finally:
            web.stop()
            app.stop()

    def test_keyboard_lines(self):
        got = []
        kb = KeyboardInput(got.append)
        for line in ("p", "s", "2", "state bored", "n", "idle_timeout"):
            kb.handle_line(line)
        self.assertEqual([e["name"] for e in got],
                         ["pat", "sleep", "set_stage", "set_state", "motion", "idle_timeout"])


if __name__ == "__main__":
    unittest.main()
