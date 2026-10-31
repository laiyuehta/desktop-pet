"""camera.snapshot 能力模組測試（picam client 版）：picam、相機、上傳都用假的。"""
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

from pet.backend_client import directive_to_event
from pet.extensions import CameraSnapshot, CameraWebAPI, EventBus
from pet.extensions.camera import CameraError, picam_snapshot
from services.picam import picamd
from tests.test_picam import FakeBackend


def jpeg_bytes(w=64, h=48):
    out = io.BytesIO()
    Image.new("RGB", (w, h), (200, 100, 50)).save(out, "JPEG")
    return out.getvalue()


class FakePicam:
    """假的 picam：每次呼叫給一張新照片（photo_id 可以由測試指定，模擬合併）。"""

    def __init__(self):
        self.calls = []
        self.error = None
        self.next_id = None
        self.n = 0
        self.up = True
        self.available = lambda: self.up

    def __call__(self, w, h, q, client, reason):
        self.calls.append((w, h, q, client, reason))
        if self.error:
            raise self.error
        self.n += 1
        pid = self.next_id or "20261028-2130%02d-000" % self.n
        return {"ok": True, "photo_id": pid, "ts": 1793193000.0 + self.n, "width": w, "height": h,
                "shared": 1}, jpeg_bytes()


class FakeUploader:
    def __init__(self):
        self.down = False
        self.got = []

    def upload(self, data, meta):
        if self.down:
            raise urllib.error.URLError("connection refused")
        self.got.append((len(data), dict(meta)))
        return "photo-%d" % len(self.got)


class TestCamera(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.bus = EventBus()
        self.events = []
        self.bus.subscribe("*", self.events.append)
        self.up = FakeUploader()
        self.fp = FakePicam()
        self.ext = CameraSnapshot(
            self.up,
            pending_dir=str(self.tmp_path / "pending"),
            capture=self.fp,
            retry_delay=0,
            max_pending=3,
            upload_grace=0,
        )
        self.ext.sleep = lambda s: None
        self.ext.attach(self.bus)
        os.makedirs(self.ext.pending_dir, exist_ok=True)

    def tearDown(self):
        self.ext.stop()
        self._tmp.cleanup()

    def _job(self, **kw):
        p = self.ext._params(kw)
        with self.ext._lock:
            self.ext._req_seq += 1
            self.ext._in_flight += 1
            rid = self.ext._req_seq
        j = dict(p, source=kw.get("source", "api"), request_id=rid, requested=time.time())
        res = self.ext.run_job(j)
        self.ext._finish(j, res)
        return res

    def _names(self):
        self.bus.dispatch_pending()
        return [(e["type"], e["name"]) for e in self.events]

    def test_snapshot_uploads_and_reacts(self):
        r = self.ext.trigger({"reason": "manual"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["request_id"], 1)
        res = self.ext.wait(1, 5)
        self.assertTrue(res["ok"])
        self.assertTrue(res["uploaded"])
        self.assertEqual(res["backend_photo_id"], "photo-1")
        self.assertEqual(self.fp.calls[0][:3], (1280, 960, 85))
        self.assertEqual(self.fp.calls[0][3], "desktop-pet:api")
        self.assertEqual(self.up.got[0][1]["reason"], "manual")
        self.assertEqual(self.up.got[0][1]["taken"], 1793193001.0)
        n = self._names()
        self.assertIn(("action", "say"), n)
        self.assertIn(("action", "expression"), n)
        self.assertIn(("skill", "camera.snapshot.done"), n)
        self.assertEqual(self.ext.pending(), [])
        self.assertEqual(self.ext.last_thumb[:2], b"\xff\xd8")

    def test_params_and_validation(self):
        self._job(width=640, height=480, quality=70, reason="timer")
        self.assertEqual(self.fp.calls[-1][:3], (640, 480, 70))
        self.assertEqual(self.fp.calls[-1][4], "timer")
        r = self.ext.trigger({"width": 99999})
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "bad_params")
        self.assertFalse(self.ext.trigger({"reason": "../x"})["ok"])

    def test_no_cooldown_and_same_photo_id_uploads_once(self):
        self.fp.next_id = "20261028-213000-500"           # picam 合併成同一張
        rids = [self.ext.trigger({"reason": "manual"}, source=s)["request_id"] for s in ("web", "touch", "backend")]
        res = [self.ext.wait(r, 5) for r in rids]
        self.assertTrue(all(r["ok"] for r in res))
        self.assertEqual({r["photo_id"] for r in res}, {"20261028-213000-500"})
        self.assertEqual([r["trigger"] for r in res], ["web", "touch", "backend"])
        self.assertEqual(len(self.up.got), 1)                       # 只上傳一次
        self.bus.dispatch_pending()
        done = [e for e in self.events if e["name"] == "camera.snapshot.done"]
        self.assertEqual(sorted(e["trigger"] for e in done), ["backend", "touch", "web"])
        self.assertEqual({e["request_id"] for e in done}, set(rids))
        says = [e for e in self.events if e["type"] == "action" and e["name"] == "say"]
        self.assertEqual(len(says), 1)                         # 喀嚓只演一次

    def test_service_down(self):
        self.fp.up = False
        r = self.ext.trigger()
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "service_down")
        self.assertEqual(r["error"], "相機服務未啟動")
        self.assertIn(("skill", "camera.snapshot.failed"), self._names())
        self.fp.up = True
        self.fp.error = CameraError("service_down", "相機服務未啟動")   # 檢查後才停掉
        res = self._job()
        self.assertFalse(res["ok"])
        self.assertEqual(res["kind"], "service_down")
        self.fp.error = CameraError("camera_error", "相機打不開：找不到相機")
        res = self._job()
        self.assertEqual(res["kind"], "camera_error")
        self.assertEqual(self.ext.pending(), [])

    def test_backend_down_queue_and_resend(self):
        self.up.down = True
        for i in range(5):
            r = self._job(reason="r%d" % i)
            self.assertTrue(r["ok"])
            self.assertTrue(r["queued"])
            self.assertFalse(r["uploaded"])
        self.assertEqual(len(self.ext.pending()), 3)                 # 上限 3 張（測試設定），最舊的刪掉
        self.assertEqual(self.ext.flush_pending(), 0)
        self.up.down = False
        self.assertEqual(self.ext.flush_pending(), 3)
        self.assertEqual(self.ext.pending(), [])
        self.assertEqual([m["reason"] for _, m in self.up.got], ["r2", "r3", "r4"])
        self.assertTrue(self.ext.last["uploaded"])

    def test_success_flushes_older_pending(self):
        self.up.down = True
        self._job(reason="old")
        self.up.down = False
        self._job(reason="new")
        self.assertEqual([m["reason"] for _, m in self.up.got], ["new", "old"])
        self.assertEqual(self.ext.pending(), [])

    def test_retry_once(self):
        calls = {"n": 0}
        real = self.up.upload

        def flaky(data, meta):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("timeout")
            return real(data, meta)
        self.up.upload = flaky
        self.assertTrue(self._job()["uploaded"])
        self.assertEqual(calls["n"], 2)

    def test_event_trigger_and_backend_directive(self):
        self.ext.start()
        ev = directive_to_event({"type": "skill", "name": "camera.snapshot", "width": 320, "height": 240})
        self.assertEqual(ev["type"], "skill")
        self.assertEqual(ev["params"]["reason"], "backend")
        self.bus.publish(ev)
        self.bus.dispatch_pending()
        for _ in range(300):
            if self.ext.last:
                break
            time.sleep(0.01)
        self.assertTrue(self.ext.last)
        self.assertTrue(self.ext.last["uploaded"])
        self.assertEqual(self.fp.calls[-1][:2], (320, 240))
        self.assertEqual(self.ext.last["trigger"], "backend")

    def test_web_api_wait_and_thumbnail(self):
        from pet.inputs import WebRemoteInput
        api = CameraWebAPI(self.ext)
        w = WebRemoteInput(lambda e: None, "127.0.0.1", 0, routes=[api])
        w.start()
        try:
            base = f"http://127.0.0.1:{w.port}"
            with self.assertRaises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(base + "/skill/camera.snapshot/last.jpg")
            self.assertEqual(ei.exception.code, 404)

            def post(body):
                return urllib.request.Request(base + "/skill/camera.snapshot", data=json.dumps(body).encode(),
                                              headers={"Content-Type": "application/json"})
            self.assertEqual(urllib.request.urlopen(post({"reason": "manual"})).status, 202)
            r = json.loads(urllib.request.urlopen(post({"reason": "manual", "wait": True})).read())
            self.assertTrue(r["ok"])
            self.assertTrue(r["uploaded"])
            self.assertEqual(r["request_id"], 2)   # 不再有冷卻
            st = json.loads(urllib.request.urlopen(base + "/skill/camera.snapshot").read())
            self.assertTrue(st["last"]["uploaded"])
            self.assertTrue(st["has_thumb"])
            self.assertTrue(st["service"])
            self.assertEqual(len(st["recent"]), 2)
            r = urllib.request.urlopen(base + "/skill/camera.snapshot/last.jpg")
            self.assertEqual(r.headers["Content-Type"], "image/jpeg")
            self.assertEqual(r.read()[:2], b"\xff\xd8")
            self.fp.up = False
            with self.assertRaises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(post({}))
            self.assertEqual(ei.exception.code, 503)
            self.assertIn("相機服務未啟動", json.loads(ei.exception.read())["error"])
        finally:
            w.stop()

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "需要 socket.AF_UNIX")
    def test_end_to_end_with_picam_three_triggers_one_photo(self):
        """真的 picamd（假相機）+ 真的 client：3 個來源 0.5 秒內觸發 → 拍一次、同一個 photo_id、後端收到一張。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            be = FakeBackend(capture_s=0.2)
            p = picamd.Picam(be)
            sock = str(tmp_path / "picam.sock")
            srv = picamd.Server(p, sock, None, None)
            p.start()
            srv.start()
            up = FakeUploader()
            bus = EventBus()
            events = []
            bus.subscribe("*", events.append)
            ext = CameraSnapshot(up, pending_dir=str(tmp_path / "pending"), capture=picam_snapshot(sock), retry_delay=0)
            ext.attach(bus)
            ext.start()
            try:
                rids = []
                for s in ("web", "touch", "backend"):
                    rids.append(ext.trigger({}, source=s)["request_id"])
                    time.sleep(0.12)
                res = [ext.wait(r, 10) for r in rids]
                self.assertTrue(all(r and r["ok"] for r in res), res)
                self.assertEqual(len({r["photo_id"] for r in res}), 1)
                self.assertEqual(len(be.captures), 1)
                self.assertEqual(len(up.got), 1)
                self.assertTrue(all(r["shared"] == 3 for r in res))
                bus.dispatch_pending()
                self.assertEqual(len([e for e in events if e["name"] == "camera.snapshot.done"]), 3)
            finally:
                ext.stop()
                srv.stop()
                p.stop()

    def test_app_ignores_skill_events_in_fsm(self):
        from pet.app import PetApp
        app = PetApp(use_clock=False)
        st = app.fsm.state
        app.post({"type": "skill", "name": "camera.snapshot", "source": "t", "ts": 0})
        app.step(0.05)
        self.assertEqual(app.fsm.state, st)


if __name__ == "__main__":
    unittest.main()
