"""動畫包：產生工具（bake）的 manifest、播放器的片段選擇／切換點／過渡／退路、驅動器與輸出端。"""
import io
import json
import os
import random
import shutil
import socket
import struct
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from pet.app import PetApp  # noqa: E402
from pet.baker import CORE_STATES, bake, transition_pairs  # noqa: E402
from pet.events import make_event  # noqa: E402
from pet.outputs import PiscreenSink  # noqa: E402
from pet.outputs.base import OutputSink  # noqa: E402
from pet.pack import AnimationPack, PackDriver, PackPlayer  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_baseline_jpeg(data: bytes) -> bool:
    return data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9" and b"\xff\xc0" in data and b"\xff\xc2" not in data


class BakeTest(unittest.TestCase):
    """用真的骨架產生一個小動畫包（fps 10、兩個狀態、一個表情）檢查格式。"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.out = os.path.join(cls.dir, "seed")
        cls.m = bake(cls.out, only_states=["idle", "happy"], fps=10, expressions=["surprised"],
                     preview=True, log=lambda *_: None)
        cls.pk = AnimationPack(cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.pk.close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_manifest_basics(self):
        m = self.m
        self.assertEqual(m["format"], 1)
        self.assertEqual(m["skeleton"], "seed")
        self.assertEqual(m["fps"], 10)
        self.assertEqual(set(m["states"]), {"idle", "happy"})
        with open(os.path.join(self.out, "manifest.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["states"].keys(), m["states"].keys())
        self.assertTrue(os.path.exists(os.path.join(self.out, "preview", "idle.gif")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "preview", "states.png")))
        self.assertIn("hash", m["source"])

    def test_clip_lengths_and_variants(self):
        sk = json.load(open(os.path.join(ROOT, "assets", "skeletons", "seed.json"), encoding="utf-8"))
        for name in ("idle", "happy"):
            st = self.m["states"][name]
            n = round(sk["animations"][st["animation"]]["duration"] * 10)
            self.assertEqual(st["frames"], n)
            for cid in st["clips"] + list(st["expressions"].values()):
                self.assertEqual(len(self.m["clips"][cid]["frames"]), n, cid)
        self.assertGreaterEqual(len(self.m["states"]["idle"]["clips"]), 2)   # 有變化版
        self.assertIn("surprised", self.m["states"]["idle"]["expressions"])

    def test_frames_are_baseline_jpeg_800x480(self):
        for cid in list(self.pk.clips)[:20]:
            data = self.pk.frame(cid, 0)
            self.assertTrue(is_baseline_jpeg(data), cid)
        img = Image.open(io.BytesIO(self.pk.frame("idle/base", 0)))
        self.assertEqual(img.size, (800, 480))

    def test_cuts_avoid_busy_eyes(self):
        for cid, c in self.m["clips"].items():
            if c["kind"] != "loop":
                continue
            for i in c["cuts"]:
                self.assertFalse(any(a <= i < b for a, b in c["busy"]), (cid, i))
                self.assertEqual(i % self.m["states"][c["state"]]["cut_every"], 0)
        blink = self.m["clips"]["idle/blink"]
        self.assertTrue(blink["busy"], "眨眼版應該有眼睛不同的區間")

    def test_transitions_cover_cuts(self):
        tr = self.m["transitions"]
        self.assertIn("idle>happy", tr)
        self.assertIn("happy>idle", tr)
        base = self.m["clips"]["idle/base"]
        self.assertEqual(sorted(int(k) for k in tr["idle>happy"]), sorted(base["cuts"]))
        for key, entry in tr.items():
            b = key.split(">")[1]
            for k, t in entry.items():
                self.assertIn(t["clip"], self.m["clips"])
                self.assertLess(t["enter"], self.m["states"][b]["frames"])

    def test_dedupe(self):
        st = self.m["stats"]
        self.assertGreaterEqual(st["frame_refs"], st["unique_frames"])
        self.assertEqual(os.path.getsize(os.path.join(self.out, "frames.bin")), st["bytes"])

    # -- 驅動器（接 PetApp）------------------------------------------------------
    def _driver(self):
        class Cap(OutputSink):
            def __init__(self):
                self.jpegs, self.images = [], []

            def send(self, frame):
                self.images.append(frame)

            def send_jpeg(self, data):
                self.jpegs.append(bytes(data))

        app = PetApp(use_clock=False, eye_seed=1)
        sink = Cap()
        return app, sink, PackDriver(self.pk, app, sink, rng=random.Random(1))

    def test_driver_sends_baked_jpeg_without_reencode(self):
        app, sink, drv = self._driver()
        for _ in range(5):
            app.step(0.1)
            self.assertEqual(drv.emit(), "pack")
        self.assertEqual(len(sink.jpegs), 5)
        self.assertFalse(sink.images)
        self.assertTrue(all(is_baseline_jpeg(d) for d in sink.jpegs))
        self.assertEqual(app.render_info["mode"], "pack")

    def test_driver_bubble_composites(self):
        app, sink, drv = self._driver()
        app.renderer.say("你好", 5)
        app.step(0.1)
        self.assertEqual(drv.emit(), "bubble")
        self.assertEqual(sink.images[-1].size, (800, 480))
        self.assertFalse(sink.jpegs)

    def test_driver_falls_back_to_live_for_missing_state(self):
        app, sink, drv = self._driver()
        app.step(0.1)
        drv.emit()
        app.post(make_event("control", "set_state", state="sleep"))   # 小動畫包沒有 sleep
        app.step(0.1)
        self.assertEqual(drv.emit(), "live")
        self.assertEqual(sink.images[-1].size, (800, 480))
        app.post(make_event("control", "set_state", state="idle"))
        app.step(0.1)
        self.assertEqual(drv.emit(), "pack")


def make_fake_pack(d, states, transitions=None, skeleton="seed"):
    """手工做一個假的動畫包（幀是假的 bytes），只測播放邏輯。"""
    clips, blob = {}, b""
    st_out = {}
    for name, spec in states.items():
        n = spec["n"]
        ids = []
        for v in spec.get("variants", [{"name": "base"}]):
            cid = f"{name}/{v['name']}"
            frames = []
            for i in range(n):
                data = f"{cid}#{i}".encode()
                frames.append([len(blob), len(data), 400, 100])
                blob += data
            clips[cid] = {"state": name, "kind": "loop", "loop": True, "weight": v.get("weight", 1),
                          "frames": frames, "cuts": spec.get("cuts", list(range(n))), "busy": v.get("busy", [])}
            ids.append(cid)
        exprs = {}
        for ex in spec.get("exprs", []):
            cid = f"{name}/expr_{ex}"
            frames = []
            for i in range(n):
                data = f"{cid}#{i}".encode()
                frames.append([len(blob), len(data), 400, 100])
                blob += data
            clips[cid] = {"state": name, "kind": "expr", "loop": True, "weight": 0, "frames": frames,
                          "cuts": spec.get("cuts", list(range(n))), "busy": [[0, n]]}
            exprs[ex] = cid
        st_out[name] = {"frames": n, "clips": ids, "expressions": exprs}
    tr_out = {}
    for key, spec in (transitions or {}).items():
        tr_out[key] = {}
        for k in spec["cuts"]:
            cid = f"trans/{key}@{k}"
            frames = []
            for i in range(spec["n"]):
                data = f"{cid}#{i}".encode()
                frames.append([len(blob), len(data), 400, 100])
                blob += data
            clips[cid] = {"state": key.split(">")[1], "kind": "transition", "loop": False, "frames": frames,
                          "cuts": [], "busy": []}
            tr_out[key][str(k)] = {"clip": cid, "enter": spec["enter"]}
    with open(os.path.join(d, "frames.bin"), "wb") as fh:
        fh.write(blob)
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"format": 1, "fps": 20, "size": [800, 480], "skeleton": skeleton, "look": skeleton,
                   "clips": clips, "states": st_out, "transitions": tr_out}, fh)
    return AnimationPack(d)


class PlayerTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_random_variant_each_loop(self):
        pk = make_fake_pack(self.d, {"idle": {"n": 4, "variants": [{"name": "a"}, {"name": "b"}]}})
        p = PackPlayer(pk, rng=random.Random(3))
        seen = set()
        for _ in range(4 * 30):
            clip, i = p.next("idle")
            seen.add(clip)
            self.assertLess(i, 4)
        self.assertEqual(seen, {"idle/a", "idle/b"})
        pk.close()

    def test_frame_bytes(self):
        pk = make_fake_pack(self.d, {"idle": {"n": 3}})
        self.assertEqual(bytes(pk.frame("idle/base", 2)), b"idle/base#2")
        pk.close()

    def test_waits_for_cut_then_plays_transition(self):
        pk = make_fake_pack(self.d, {"idle": {"n": 8, "cuts": [0, 4]}, "happy": {"n": 6}},
                            {"idle>happy": {"cuts": [0, 4], "n": 3, "enter": 4}})
        p = PackPlayer(pk, rng=random.Random(0))
        self.assertEqual(p.next("idle"), ("idle/base", 0))
        self.assertEqual(p.next("idle"), ("idle/base", 1))
        # 在第 1 格換狀態：要播到切換點（第 4 格）才走
        self.assertEqual(p.next("happy"), ("idle/base", 2))
        self.assertEqual(p.next("happy"), ("idle/base", 3))
        self.assertEqual(p.next("happy"), ("idle/base", 4))
        self.assertEqual(p.next("happy"), ("trans/idle>happy@4", 0))
        self.assertEqual(p.next("happy"), ("trans/idle>happy@4", 1))
        self.assertEqual(p.next("happy"), ("trans/idle>happy@4", 2))
        self.assertEqual(p.next("happy"), ("happy/base", 4))      # 接到 B 的 enter 格
        self.assertEqual(p.next("happy"), ("happy/base", 5))
        self.assertEqual(p.next("happy"), ("happy/base", 0))
        self.assertEqual(p.switches, 1)
        pk.close()

    def test_no_transition_switches_at_cut_to_frame0(self):
        pk = make_fake_pack(self.d, {"idle": {"n": 6, "cuts": [0, 3]}, "sleep": {"n": 5}})
        p = PackPlayer(pk)
        p.next("idle")
        self.assertEqual(p.next("idle"), ("idle/base", 1))
        self.assertEqual(p.next("sleep"), ("idle/base", 2))
        self.assertEqual(p.next("sleep"), ("idle/base", 3))
        self.assertEqual(p.next("sleep"), ("sleep/base", 0))
        pk.close()

    def test_reaction_time_recorded(self):
        import time
        pk = make_fake_pack(self.d, {"idle": {"n": 4}, "happy": {"n": 4}})
        p = PackPlayer(pk)
        p.next("idle")
        p.next("happy", change_ts=time.time() - 0.2)
        self.assertGreaterEqual(p.reaction_ms, 200)
        pk.close()

    def test_fallback_when_state_missing_or_skeleton_differs(self):
        pk = make_fake_pack(self.d, {"idle": {"n": 4}})
        p = PackPlayer(pk)
        self.assertEqual(p.next("idle", skeleton="seed"), ("idle/base", 0))
        self.assertIsNone(p.next("dance", skeleton="seed"))           # 動畫包沒有的新狀態
        self.assertTrue(p.live)
        self.assertEqual(p.next("idle", skeleton="seed"), ("idle/base", 0))   # 回來了
        self.assertIsNone(p.next("idle", skeleton="legs"))            # 換了外觀但沒有新動畫包
        pk.close()

    def test_expression_override_same_frame(self):
        pk = make_fake_pack(self.d, {"idle": {"n": 6, "exprs": ["surprised"]}})
        p = PackPlayer(pk)
        self.assertEqual(p.next("idle"), ("idle/base", 0))
        self.assertEqual(p.next("idle", override="surprised"), ("idle/expr_surprised", 1))
        self.assertEqual(p.next("idle", override="surprised"), ("idle/expr_surprised", 2))
        self.assertEqual(p.next("idle", override="sad"), ("idle/base", 3))      # 沒烘的表情就忽略
        self.assertEqual(p.next("idle"), ("idle/base", 4))
        pk.close()

    def test_entering_avoids_busy_variant(self):
        pk = make_fake_pack(self.d, {"idle": {"n": 4},
                                     "happy": {"n": 6, "variants": [{"name": "base"}, {"name": "blink", "weight": 100,
                                                                                         "busy": [[1, 3]]}]}},
                            {"idle>happy": {"cuts": [0], "n": 1, "enter": 2}})
        for seed in range(10):
            p = PackPlayer(pk, rng=random.Random(seed))
            p.next("idle")
            self.assertEqual(p.next("happy")[0], "trans/idle>happy@0")
            self.assertEqual(p.next("happy"), ("happy/base", 2))   # blink 版第 2 格眼睛在眨，不能從這裡進
        pk.close()

    def test_try_load_missing(self):
        self.assertIsNone(AnimationPack.try_load(os.path.join(self.d, "nope")))

    def test_unbaked_transition_from_non_looping_clip_never_gets_stuck(self):
        pk = make_fake_pack(self.d, {"sad_1": {"n": 3, "cuts": [0]}, "shy": {"n": 4, "cuts": [0]}})
        pk.clips["sad_1/base"]["loop"] = False
        p = PackPlayer(pk)
        self.assertEqual(p.next("sad_1"), ("sad_1/base", 0))
        self.assertEqual(p.next("sad_1"), ("sad_1/base", 1))
        self.assertEqual(p.next("shy"), ("sad_1/base", 2))
        self.assertEqual(p.next("shy"), ("shy/base", 0))
        pk.close()


class SelectiveTransitionBakeTest(unittest.TestCase):
    """驗證只替狀態機與情緒流程會走到的轉移烘過渡片段，未烘轉移退回第 0 格。"""

    def test_transition_pairs_cover_fsm_core_and_emotions(self):
        with open(os.path.join(ROOT, "config", "states.json"), encoding="utf-8") as fh:
            sc = json.load(fh)
        states = list(sc["states"])
        pairs = transition_pairs(sc, states)

        # 1. config/states.json 轉移表（含 * 規則展開）與計時器 *_done
        for tr in sc["transitions"]:
            src, dst = tr["from"], tr["to"]
            if src == "*":
                for a in states:
                    if a != dst:
                        self.assertIn((a, dst), pairs)
            elif src != dst:
                self.assertIn((src, dst), pairs)
        for s, info in sc["states"].items():
            for tm in info.get("timers", []):
                if str(tm.get("event", "")).endswith("_done") and s != "idle":
                    self.assertIn((s, "idle"), pairs)

        # 2. 既有四個狀態之間所有配對不變
        for a in CORE_STATES:
            for b in CORE_STATES:
                if a != b:
                    self.assertIn((a, b), pairs)

        # 3. 情緒狀態：idle 進入、同系列升級、連戳升級、回 idle
        for s in states:
            if s not in CORE_STATES:
                self.assertIn(("idle", s), pairs)
                self.assertIn((s, "idle"), pairs)
        for fam, hi in (("happy", 3), ("surprised", 3), ("sad", 3), ("annoyed", 2)):
            for i in range(1, hi):
                for j in range(i + 1, hi + 1):
                    self.assertIn((f"{fam}_{i}", f"{fam}_{j}"), pairs)
        for i in (1, 2, 3):
            self.assertIn((f"surprised_{i}", "annoyed_1"), pairs)

        # 4. 不會走到的跨情緒／非核心轉移不烘
        self.assertNotIn(("bored", "sad_2"), pairs)
        self.assertNotIn(("sleep", "happy_1"), pairs)
        self.assertNotIn(("sad_3", "shy"), pairs)
        self.assertNotIn(("happy_2", "sad_1"), pairs)

    def test_bake_omits_unused_transitions_and_player_falls_back_to_frame0(self):
        d = tempfile.mkdtemp()
        try:
            out = os.path.join(d, "seed")
            m = bake(out, only_states=["idle", "bored", "sad_1", "sad_2"], fps=5, expressions=[],
                     preview=False, log=lambda *_: None)
            tr = m["transitions"]
            for used in ("idle>bored", "bored>idle", "idle>sad_1", "idle>sad_2",
                         "sad_1>sad_2", "sad_1>idle", "sad_2>idle"):
                self.assertIn(used, tr)
            for unused in ("bored>sad_1", "bored>sad_2", "sad_1>bored", "sad_2>bored", "sad_2>sad_1"):
                self.assertNotIn(unused, tr)

            pk = AnimationPack(out)
            try:
                p = PackPlayer(pk, rng=random.Random(0))
                # 有烘過渡片段：idle -> sad_1 -> sad_2 -> idle
                self.assertEqual(p.next("idle")[1], 0)
                clip, idx = p.next("sad_1")
                self.assertTrue(clip.startswith("trans/idle>sad_1@"))
                self.assertEqual(idx, 0)
                while p.mode == "trans":
                    clip, idx = p.next("sad_1")
                self.assertEqual(p.state, "sad_1")
                # 等 sad_1 播到切換點第 0 格後升級到 sad_2（有過渡片段）
                while p.i != 0:
                    p.next("sad_1")
                clip, idx = p.next("sad_2")
                self.assertEqual((clip, idx), ("trans/sad_1>sad_2@0", 0))
                while p.mode == "trans":
                    p.next("sad_2")
                self.assertEqual(p.state, "sad_2")

                # 沒烘過渡片段：sad_2 -> bored，在切換點直接從 bored 第 0 格開始播
                while p.i != 0:
                    p.next("sad_2")
                clip, idx = p.next("bored")
                self.assertTrue(clip.startswith("bored/"))
                self.assertEqual(idx, 0)
                self.assertEqual(p.state, "bored")

                # 沒烘過渡片段：bored -> sad_1，等下一個切換點直接從 sad_1 第 0 格開始播
                p.next("bored")
                while p.state != "sad_1":
                    clip, idx = p.next("sad_1")
                self.assertTrue(clip.startswith("sad_1/"))
                self.assertEqual(idx, 0)
            finally:
                pk.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class StateChangeTimestampTest(unittest.TestCase):
    def test_uses_original_event_time(self):
        app = PetApp(use_clock=False)
        app.post(make_event("control", "set_state", state="bored", ts=1000.0))
        app.step(0.05)
        self.assertEqual(app.state_changed_ts, 1000.0)
        app.post(make_event("touch", "pat", ts=2000.0))
        app.step(0.05)
        self.assertEqual(app.fsm.state, "happy_1")
        self.assertEqual(app.state_changed_ts, 2000.0)


class PiscreenJpegPassthroughTest(unittest.TestCase):
    def test_send_jpeg_forwards_bytes(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "s.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        got = []

        def serve():
            conn, _ = srv.accept()
            buf = b""
            while True:
                c = conn.recv(65536)
                if not c:
                    break
                buf += c
            n = struct.unpack(">I", buf[:4])[0]
            got.append(buf[4:4 + n])
            conn.close()

        th = threading.Thread(target=serve, daemon=True)
        th.start()
        buf = io.BytesIO()
        Image.new("RGB", (800, 480), (1, 2, 3)).save(buf, "JPEG")
        sink = PiscreenSink(path)
        sink.open()
        sink.send_jpeg(buf.getvalue())
        sink.close()
        th.join(5)
        srv.close()
        shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(got, [buf.getvalue()])


class MainSmokeTest(unittest.TestCase):
    def test_main_runs_with_pack_and_without(self):
        import main
        d = tempfile.mkdtemp()
        try:
            make_fake_pack(d, {}).close()       # 空的動畫包 → 全部退回即時繪圖也不會當
            self.assertEqual(main.main(["--sink", "null", "--input", "none", "--duration", "0.3",
                                        "--pack", d]), 0)
            self.assertEqual(main.main(["--sink", "null", "--input", "none", "--duration", "0.3",
                                        "--pack", "none"]), 0)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
