import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pet.skeleton import (AnimationPlayer, Skeleton, blend_poses, mat_apply, mat_inverse,  # noqa: E402
                          mat_local, mat_mul, sample_track)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MINI = {
    "name": "mini",
    "bones": [
        {"name": "root", "parent": None, "pivot": [100, 100]},
        {"name": "arm", "parent": "root", "pivot": [10, 0], "rest": {"rot": 90}},
    ],
    "animations": {
        "move": {"duration": 2.0, "loop": True,
                 "tracks": {"root": {"x": [[0, 0], [1, 10], [2, 0]]}}},
        "once": {"duration": 1.0, "loop": False,
                 "tracks": {"root": {"y": [[0, 0, "ease"], [1, 10]]}}},
    },
}


class InterpolationTest(unittest.TestCase):
    def test_linear(self):
        keys = [[0, 0], [1, 10]]
        self.assertAlmostEqual(sample_track(keys, 0.5), 5.0)
        self.assertAlmostEqual(sample_track(keys, 0.25), 2.5)

    def test_clamp_outside(self):
        keys = [[1, 3], [2, 7]]
        self.assertEqual(sample_track(keys, 0), 3)
        self.assertEqual(sample_track(keys, 5), 7)

    def test_ease_is_symmetric_and_slow_at_ends(self):
        keys = [[0, 0, "ease"], [1, 10]]
        self.assertAlmostEqual(sample_track(keys, 0.5), 5.0)
        self.assertLess(sample_track(keys, 0.1), 1.0)       # 線性會是 1.0
        self.assertGreater(sample_track(keys, 0.9), 9.0)

    def test_step(self):
        keys = [[0, 0, "step"], [1, 10]]
        self.assertEqual(sample_track(keys, 0.99), 0)
        self.assertEqual(sample_track(keys, 1.0), 10)

    def test_multi_segment(self):
        keys = [[0, 0], [1, 10], [3, 0]]
        self.assertAlmostEqual(sample_track(keys, 2.0), 5.0)


class SkeletonTest(unittest.TestCase):
    def setUp(self):
        self.sk = Skeleton(MINI)

    def test_loop_wraps(self):
        a = self.sk.animations["move"]
        self.assertAlmostEqual(a.sample(1.0)["root"]["x"], 10)
        self.assertAlmostEqual(a.sample(3.0)["root"]["x"], 10)   # 3 % 2 = 1

    def test_non_loop_holds_last(self):
        a = self.sk.animations["once"]
        self.assertAlmostEqual(a.sample(5.0)["root"]["y"], 10)

    def test_hierarchy_transform(self):
        w = self.sk.world_transforms({"root": {"x": 5}})
        # arm 的原點 = root(105,100) + (10,0)
        x, y = mat_apply(w["arm"], 0, 0)
        self.assertAlmostEqual(x, 115)
        self.assertAlmostEqual(y, 100)
        # arm 旋轉 90 度：局部 (1,0) → 世界 +y 方向
        x2, y2 = mat_apply(w["arm"], 1, 0)
        self.assertAlmostEqual(x2, 115, places=6)
        self.assertAlmostEqual(y2, 101, places=6)

    def test_parent_scale_propagates(self):
        w = self.sk.world_transforms({"root": {"sx": 2.0}})
        x, _ = mat_apply(w["arm"], 0, 0)
        self.assertAlmostEqual(x, 120)

    def test_matrix_inverse(self):
        m = mat_mul(mat_local(3, 4, 30, 2, 0.5), mat_local(-1, 2, -10, 1, 1))
        i = mat_mul(mat_inverse(m), m)
        for got, exp in zip(i, (1, 0, 0, 0, 1, 0)):
            self.assertAlmostEqual(got, exp, places=9)

    def test_blend(self):
        p = blend_poses({"root": {"x": 0}}, {"root": {"x": 10, "sx": 2}}, 0.5)
        self.assertAlmostEqual(p["root"]["x"], 5)
        self.assertAlmostEqual(p["root"]["sx"], 1.5)   # 缺的通道用中性值 1

    def test_player_crossfade(self):
        pl = AnimationPlayer(self.sk, fade=1.0)
        pl.play("move")
        pl.update(1.0)
        pl.play("once")
        pl.update(0.5)
        x = pl.pose()["root"].get("x", 0)
        self.assertTrue(0 < x < 10)   # 淡出中：舊動畫的 x 還沒完全歸零
        pl.update(1.0)
        self.assertAlmostEqual(pl.pose()["root"].get("x", 0), 0)

    def test_bad_parent_order(self):
        bad = {"bones": [{"name": "a", "parent": "b"}, {"name": "b", "parent": None}]}
        with self.assertRaises(ValueError):
            Skeleton(bad)


class SeedAssetTest(unittest.TestCase):
    def test_seed_loads_and_renders(self):
        from pet.eyes import EyeController
        from pet.renderer import Renderer
        sk = Skeleton.load(os.path.join(ROOT, "assets", "skeletons", "seed.json"))
        for name in ("idle", "happy", "bored", "sleep"):
            self.assertIn(name, sk.animations)
        r = Renderer(sk)
        r.player.play("idle")
        r.update(0.3)
        frame = r.render(EyeController(seed=0).update(0.05))
        self.assertEqual(frame.size, (800, 480))
        self.assertEqual(frame.mode, "RGB")
        # 眼睛位置應該是亮黃色（燈眼有畫出來）
        px = frame.getpixel((400 - 66, 440 - 158 + 10))
        self.assertGreater(px[0], 200)
        self.assertGreater(px[1], 180)


if __name__ == "__main__":
    unittest.main()
