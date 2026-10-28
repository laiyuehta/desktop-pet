"""camera.snapshot 能力模組測試（picam client 版）：picam、相機、上傳都用假的。"""
import io
import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest
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


@pytest.fixture
def cam(tmp_path):
    bus = EventBus()
    events = []
    bus.subscribe("*", events.append)
    up = FakeUploader()
    fp = FakePicam()
    ext = CameraSnapshot(up, pending_dir=str(tmp_path / "pending"), capture=fp, retry_delay=0, max_pending=3,
                         upload_grace=0)
    ext.sleep = lambda s: None
    ext.attach(bus)
    os.makedirs(ext.pending_dir)
    yield ext, bus, events, up, fp
    ext.stop()


def job(ext, **kw):
    p = ext._params(kw)
    with ext._lock:
        ext._req_seq += 1
        ext._in_flight += 1
        rid = ext._req_seq
    j = dict(p, source=kw.get("source", "api"), request_id=rid, requested=time.time())
    res = ext.run_job(j)
    ext._finish(j, res)
    return res


def names(bus, events):
    bus.dispatch_pending()
    return [(e["type"], e["name"]) for e in events]


def test_snapshot_uploads_and_reacts(cam):
    ext, bus, events, up, fp = cam
    r = ext.trigger({"reason": "manual"})
    assert r["ok"] and r["request_id"] == 1
    res = ext.wait(1, 5)
    assert res["ok"] and res["uploaded"] and res["backend_photo_id"] == "photo-1"
    assert fp.calls[0][:3] == (1280, 960, 85) and fp.calls[0][3] == "desktop-pet:api"
    assert up.got[0][1]["reason"] == "manual" and up.got[0][1]["taken"] == 1793193001.0
    n = names(bus, events)
    assert ("action", "say") in n and ("action", "expression") in n
    assert ("skill", "camera.snapshot.done") in n
    assert ext.pending() == [] and ext.last_thumb[:2] == b"\xff\xd8"


def test_params_and_validation(cam):
    ext, bus, events, up, fp = cam
    job(ext, width=640, height=480, quality=70, reason="timer")
    assert fp.calls[-1][:3] == (640, 480, 70) and fp.calls[-1][4] == "timer"
    r = ext.trigger({"width": 99999})
    assert not r["ok"] and r["kind"] == "bad_params"
    assert not ext.trigger({"reason": "../x"})["ok"]


def test_no_cooldown_and_same_photo_id_uploads_once(cam):
    ext, bus, events, up, fp = cam
    fp.next_id = "20261028-213000-500"           # picam 合併成同一張
    rids = [ext.trigger({"reason": "manual"}, source=s)["request_id"] for s in ("web", "touch", "backend")]
    res = [ext.wait(r, 5) for r in rids]
    assert all(r["ok"] for r in res)
    assert {r["photo_id"] for r in res} == {"20261028-213000-500"}
    assert [r["trigger"] for r in res] == ["web", "touch", "backend"]
    assert len(up.got) == 1                       # 只上傳一次
    bus.dispatch_pending()
    done = [e for e in events if e["name"] == "camera.snapshot.done"]
    assert sorted(e["trigger"] for e in done) == ["backend", "touch", "web"]
    assert {e["request_id"] for e in done} == set(rids)
    says = [e for e in events if e["type"] == "action" and e["name"] == "say"]
    assert len(says) == 1                         # 喀嚓只演一次


def test_service_down(cam):
    ext, bus, events, up, fp = cam
    fp.up = False
    r = ext.trigger()
    assert not r["ok"] and r["kind"] == "service_down" and r["error"] == "相機服務未啟動"
    assert ("skill", "camera.snapshot.failed") in names(bus, events)
    fp.up = True
    fp.error = CameraError("service_down", "相機服務未啟動")   # 檢查後才停掉
    res = job(ext)
    assert not res["ok"] and res["kind"] == "service_down"
    fp.error = CameraError("camera_error", "相機打不開：找不到相機")
    res = job(ext)
    assert res["kind"] == "camera_error" and ext.pending() == []


def test_backend_down_queue_and_resend(cam):
    ext, bus, events, up, fp = cam
    up.down = True
    for i in range(5):
        r = job(ext, reason="r%d" % i)
        assert r["ok"] and r["queued"] and not r["uploaded"]
    assert len(ext.pending()) == 3                 # 上限 3 張（測試設定），最舊的刪掉
    assert ext.flush_pending() == 0
    up.down = False
    assert ext.flush_pending() == 3
    assert ext.pending() == [] and [m["reason"] for _, m in up.got] == ["r2", "r3", "r4"]
    assert ext.last["uploaded"]


def test_success_flushes_older_pending(cam):
    ext, bus, events, up, fp = cam
    up.down = True
    job(ext, reason="old")
    up.down = False
    job(ext, reason="new")
    assert [m["reason"] for _, m in up.got] == ["new", "old"] and ext.pending() == []


def test_retry_once(cam):
    ext, bus, events, up, fp = cam
    calls = {"n": 0}
    real = up.upload

    def flaky(data, meta):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("timeout")
        return real(data, meta)
    up.upload = flaky
    assert job(ext)["uploaded"] and calls["n"] == 2


def test_event_trigger_and_backend_directive(cam):
    ext, bus, events, up, fp = cam
    ext.start()
    ev = directive_to_event({"type": "skill", "name": "camera.snapshot", "width": 320, "height": 240})
    assert ev["type"] == "skill" and ev["params"]["reason"] == "backend"
    bus.publish(ev)
    bus.dispatch_pending()
    for _ in range(300):
        if ext.last:
            break
        time.sleep(0.01)
    assert ext.last and ext.last["uploaded"] and fp.calls[-1][:2] == (320, 240)
    assert ext.last["trigger"] == "backend"


def test_web_api_wait_and_thumbnail(cam):
    from pet.inputs import WebRemoteInput
    ext, bus, events, up, fp = cam
    api = CameraWebAPI(ext)
    w = WebRemoteInput(lambda e: None, "127.0.0.1", 0, routes=[api])
    w.start()
    try:
        base = f"http://127.0.0.1:{w.port}"
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(base + "/skill/camera.snapshot/last.jpg")
        assert ei.value.code == 404

        def post(body):
            return urllib.request.Request(base + "/skill/camera.snapshot", data=json.dumps(body).encode(),
                                          headers={"Content-Type": "application/json"})
        assert urllib.request.urlopen(post({"reason": "manual"})).status == 202
        r = json.loads(urllib.request.urlopen(post({"reason": "manual", "wait": True})).read())
        assert r["ok"] and r["uploaded"] and r["request_id"] == 2   # 不再有冷卻
        st = json.loads(urllib.request.urlopen(base + "/skill/camera.snapshot").read())
        assert st["last"]["uploaded"] and st["has_thumb"] and st["service"] and len(st["recent"]) == 2
        r = urllib.request.urlopen(base + "/skill/camera.snapshot/last.jpg")
        assert r.headers["Content-Type"] == "image/jpeg" and r.read()[:2] == b"\xff\xd8"
        fp.up = False
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(post({}))
        assert ei.value.code == 503 and "相機服務未啟動" in json.loads(ei.value.read())["error"]
    finally:
        w.stop()


def test_end_to_end_with_picam_three_triggers_one_photo(tmp_path):
    """真的 picamd（假相機）+ 真的 client：3 個來源 0.5 秒內觸發 → 拍一次、同一個 photo_id、後端收到一張。"""
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
        assert all(r and r["ok"] for r in res), res
        assert len({r["photo_id"] for r in res}) == 1 and len(be.captures) == 1 and len(up.got) == 1
        assert all(r["shared"] == 3 for r in res)
        bus.dispatch_pending()
        assert len([e for e in events if e["name"] == "camera.snapshot.done"]) == 3
    finally:
        ext.stop()
        srv.stop()
        p.stop()


def test_app_ignores_skill_events_in_fsm():
    from pet.app import PetApp
    app = PetApp(use_clock=False)
    st = app.fsm.state
    app.post({"type": "skill", "name": "camera.snapshot", "source": "t", "ts": 0})
    app.step(0.05)
    assert app.fsm.state == st
