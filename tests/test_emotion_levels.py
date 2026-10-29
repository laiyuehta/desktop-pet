"""情緒強度動畫（happy_1~3、surprised_1~3、sad_1~3、shy、annoyed_1~2）：設定、狀態機回 idle、動畫包內容。

這些狀態目前只能用 set_state（遙控頁／伺服器指令）預覽，還沒有觸發事件（見 docs/emotion-levels.md）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pet.app import PetApp  # noqa: E402
from pet.baker import bake  # noqa: E402
from pet.events import make_event  # noqa: E402
from pet.eyes import EXPRESSIONS  # noqa: E402
from pet.pack import AnimationPack  # noqa: E402
from pet.skeleton import Skeleton  # noqa: E402
from pet.state_machine import StateMachine  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_STATES = ["happy_1", "happy_2", "happy_3", "surprised_1", "surprised_2", "surprised_3",
              "sad_1", "sad_2", "sad_3", "shy", "annoyed_1", "annoyed_2"]
OLD_STATES = ["idle", "happy", "bored", "sleep"]


def load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return json.load(fh)


STATES = load("config/states.json")
BAKE = load("config/bake.json")
SK = Skeleton.load(os.path.join(ROOT, "assets", "skeletons", "seed.json"))


class EmotionConfigTest(unittest.TestCase):
    def test_states_defined(self):
        for name in NEW_STATES:
            with self.subTest(name):
                st = STATES["states"][name]
                self.assertIn(st["eyes"], EXPRESSIONS)
                self.assertIn(st["animation"], SK.animations)
                a = SK.animations[st["animation"]]
                self.assertTrue(a.loop)
                self.assertAlmostEqual(a.duration * 20, round(a.duration * 20), places=6)

    def test_body_motion_and_seamless_loop(self):
        for name in NEW_STATES:
            a = SK.animations[STATES["states"][name]["animation"]]
            with self.subTest(name):
                moving = set()
                for bone, chans in a.tracks.items():
                    for ch, keys in chans.items():
                        self.assertEqual(keys[0][0], 0)
                        self.assertAlmostEqual(keys[-1][0], a.duration)
                        diff = float(keys[-1][1]) - float(keys[0][1])
                        if ch == "rot":
                            diff = (diff + 180) % 360 - 180
                        self.assertAlmostEqual(diff, 0.0, places=6, msg=f"{bone}.{ch} 頭尾不接")
                        if len({k[1] for k in keys}) > 1:
                            moving.add(bone)
                self.assertTrue(moving & {"root", "body", "spin"}, "要有身體動作，不能只有眼睛")

    def test_levels_are_distinct_animations(self):
        anims = [json.dumps(SK.animations[STATES["states"][n]["animation"]].tracks, sort_keys=True)
                 for n in NEW_STATES]
        self.assertEqual(len(set(anims)), len(anims))
        self.assertNotIn("happy", [STATES["states"][n]["animation"] for n in NEW_STATES])

    def test_no_transitions_from_existing_states(self):
        for tr in STATES["transitions"]:
            if tr["from"] in OLD_STATES or tr["from"] == "*":
                self.assertNotIn(tr["to"], NEW_STATES, tr)

    def test_variants(self):
        for name in NEW_STATES:
            cfg = BAKE["states"][name]
            with self.subTest(name):
                self.assertGreaterEqual(len(cfg["variants"]), 2)
                n = round(SK.animations[name].duration * BAKE["fps"])
                self.assertEqual(cfg["cut_every"], n)   # 只在第 0 格切出去（動作做完整輪才回 idle）


class EmotionStateMachineTest(unittest.TestCase):
    def test_force_then_back_to_idle(self):
        for name in NEW_STATES:
            with self.subTest(name):
                fsm = StateMachine(STATES)
                fsm.force(name)
                self.assertEqual(fsm.state, name)
                timer = STATES["states"][name]["timers"][0]
                self.assertEqual(timer["event"], f"{name}_done")
                # 計時比整數輪稍短一點：播放器在下一個第 0 格切回 idle
                dur = SK.animations[name].duration
                self.assertLess(timer["after"] % dur, dur)
                self.assertGreater(timer["after"] % dur, dur - 0.2)
                t = 0.0
                while fsm.state == name and t < 30:
                    fsm.update(0.05)
                    t += 0.05
                self.assertEqual(fsm.state, "idle")
                self.assertAlmostEqual(t, timer["after"], delta=0.06)


class EmotionBakeTest(unittest.TestCase):
    """產生一個只含 idle + 新狀態的小動畫包（fps 10、無過渡、無表情片段）。"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.out = os.path.join(cls.dir, "seed")
        cls.m = bake(cls.out, only_states=["idle"] + NEW_STATES, fps=10, transitions=False, expressions=[],
                     preview=False, log=lambda *_: None)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_states_in_manifest(self):
        pk = AnimationPack(self.out)
        try:
            for name in NEW_STATES:
                with self.subTest(name):
                    st = pk.states[name]
                    self.assertEqual(st["frames"], round(SK.animations[name].duration * 10))
                    self.assertEqual(len(st["clips"]), len(BAKE["states"][name]["variants"]))
                    self.assertEqual(st["eyes"], STATES["states"][name]["eyes"])
                    for cid in st["clips"]:
                        self.assertEqual(pk.clips[cid]["cuts"], [0])
                        self.assertEqual(len(pk.clips[cid]["frames"]), st["frames"])
        finally:
            pk.close()


PACK = os.path.join(ROOT, "packs", "seed")


@unittest.skipUnless(os.path.exists(os.path.join(PACK, "manifest.json")), "沒有 packs/seed（先跑 tools/bake.py）")
class EmotionFullPackTest(unittest.TestCase):
    """tools/bake.py 產生的完整動畫包：新狀態都在、都有回 idle 的過渡片段。"""

    def test_full_pack(self):
        with open(os.path.join(PACK, "manifest.json"), encoding="utf-8") as fh:
            m = json.load(fh)
        for name in NEW_STATES:
            with self.subTest(name):
                self.assertIn(name, m["states"])
                self.assertIn(f"{name}>idle", m["transitions"])
                self.assertIn(f"idle>{name}", m["transitions"])
                self.assertEqual(list(m["transitions"][f"{name}>idle"]), ["0"])


class EmotionAppIntegrationTest(unittest.TestCase):
    """主程式情緒事件觸發、同系列升級到上限、計時回 idle、sleep 只接受 surprised、觸控輸入對應與遙控頁狀態清單。"""

    def test_trigger_all_emotions_escalate_to_cap_and_return_to_idle(self):
        cases = [
            ("emotion_happy", ["happy_1", "happy_2", "happy_3", "happy_3"]),
            ("emotion_surprised", ["surprised_1", "surprised_2", "surprised_3", "surprised_3"]),
            ("emotion_sad", ["sad_1", "sad_2", "sad_3", "sad_3"]),
            ("emotion_annoyed", ["annoyed_1", "annoyed_2", "annoyed_2"]),
            ("emotion_shy", ["shy", "shy"]),
        ]
        for ev_name, expected_seq in cases:
            with self.subTest(ev_name):
                app = PetApp(use_clock=False, eye_seed=0)
                for expected_state in expected_seq:
                    app.step(0.5)
                    app.post(make_event("sim", ev_name))
                    app.step(0.05)
                    self.assertEqual(app.fsm.state, expected_state)
                    # 升級（或到頂重觸發）時計時器會重設
                    self.assertLessEqual(app.fsm.time_in_state, 0.06)

                # 沒有新事件時照計時器自動回 idle
                after = STATES["states"][expected_seq[-1]]["timers"][0]["after"]
                steps = int(after / 0.05) + 5
                for _ in range(steps):
                    if app.fsm.state == "idle":
                        break
                    app.step(0.05)
                self.assertEqual(app.fsm.state, "idle")

    def test_trigger_with_explicit_level(self):
        app = PetApp(use_clock=False, eye_seed=0)
        app.post(make_event("sim", "emotion_sad", level=2))
        app.step(0.05)
        self.assertEqual(app.fsm.state, "sad_2")
        app.post(make_event("sim", "emotion_sad"))
        app.step(0.05)
        self.assertEqual(app.fsm.state, "sad_3")

    def test_sleep_only_accepts_surprised(self):
        app = PetApp(use_clock=False, eye_seed=0)
        app.post(make_event("control", "set_state", state="sleep"))
        app.step(0.05)
        self.assertEqual(app.fsm.state, "sleep")

        for ignored in ("emotion_happy", "emotion_sad", "emotion_shy", "emotion_annoyed"):
            app.post(make_event("sim", ignored, level=2))
            app.step(0.05)
            self.assertEqual(app.fsm.state, "sleep", ignored)

        # 睡覺時收到 surprised 會被吵醒進入 surprised_*
        app.post(make_event("sim", "emotion_surprised", level=2))
        app.step(0.05)
        self.assertEqual(app.fsm.state, "surprised_2")

        # 睡覺時被摸頭會照轉移規則叫醒到 idle（而非卡在 sleep），醒來後再摸進 happy_1
        app.post(make_event("control", "set_state", state="sleep"))
        app.step(0.05)
        self.assertEqual(app.fsm.state, "sleep")
        app.post(make_event("touch", "pat"))
        app.step(0.05)
        self.assertEqual(app.fsm.state, "idle")
        app.post(make_event("touch", "pat"))
        app.step(0.05)
        self.assertEqual(app.fsm.state, "happy_1")

    def test_pat_and_poke_input_mapping_and_window(self):
        app = PetApp(use_clock=False, eye_seed=0)
        # 摸頭一次 happy_1、短時間連續摸升級到 happy_2 -> happy_3
        app.post(make_event("touch", "pat"))
        app.step(0.1)
        self.assertEqual(app.fsm.state, "happy_1")
        app.post(make_event("touch", "pat"))
        app.step(0.1)
        self.assertEqual(app.fsm.state, "happy_2")
        app.post(make_event("touch", "pat"))
        app.step(0.1)
        self.assertEqual(app.fsm.state, "happy_3")

        # 超過時間窗並回到 idle 後，再摸頭重新從 happy_1 開始
        for _ in range(100):
            app.step(0.1)
        self.assertEqual(app.fsm.state, "idle")
        app.post(make_event("touch", "pat"))
        app.step(0.1)
        self.assertEqual(app.fsm.state, "happy_1")

        # 戳一下 surprised_1；一直戳進 annoyed_1 -> annoyed_2
        app.post(make_event("touch", "poke"))
        app.step(0.1)
        self.assertEqual(app.fsm.state, "surprised_1")
        app.post(make_event("touch", "poke"))
        app.step(0.1)
        self.assertEqual(app.fsm.state, "surprised_1")
        app.post(make_event("touch", "poke"))
        app.step(0.1)
        self.assertEqual(app.fsm.state, "annoyed_1")
        app.post(make_event("touch", "poke"))
        app.step(0.1)
        self.assertEqual(app.fsm.state, "annoyed_2")
        app.post(make_event("touch", "poke"))
        app.step(0.1)
        self.assertEqual(app.fsm.state, "annoyed_2")

    def test_status_exposes_all_states_and_labels_for_remote(self):
        app = PetApp(use_clock=False, eye_seed=0)
        st = app.status()
        for name in OLD_STATES + NEW_STATES:
            self.assertIn(name, st["states"])
            self.assertEqual(st["state_labels"][name], STATES["states"][name]["label"])


if __name__ == "__main__":
    unittest.main()
