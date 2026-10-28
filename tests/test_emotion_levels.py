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

from pet.baker import bake  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
