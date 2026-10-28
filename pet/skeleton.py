"""自訂極簡 2D 骨架格式與 runtime。

JSON 格式（詳見 README「骨架格式」）：

{
  "name": "seed",
  "canvas": [800, 480],
  "bones": [
    {"name": "root", "parent": null, "pivot": [400, 440],
     "rest": {"rot": 0, "sx": 1, "sy": 1}},
    {"name": "body", "parent": "root", "pivot": [0, -160], "z": 10,
     "part": {"image": "../parts/seed/body.png", "anchor": [170, 170]}}
  ],
  "face": {"bone": "face", ...},          # 燈眼／嘴巴的位置（程式繪製，不是貼圖）
  "animations": {
    "idle": {"duration": 3.2, "loop": true,
             "tracks": {"body": {"y": [[0, 0, "ease"], [1.6, -6, "ease"], [3.2, 0]]}}}
  }
}

- pivot：骨頭原點在「父骨頭座標系」中的位置（根骨頭就是螢幕座標）。
- rest：靜止姿勢的旋轉（度）與縮放。
- part：掛在骨頭上的零件圖，anchor 是圖片中對齊骨頭原點的像素。
- 動畫通道：x、y（位移，加在 pivot 上）、rot（旋轉，加在 rest.rot 上）、
  sx、sy（縮放，乘在 rest 上）。關鍵幀格式 [時間, 數值, 緩動]，
  緩動是「從這一幀到下一幀」的插值方式：linear（預設）、ease、step。
"""

from __future__ import annotations

import json
import math
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PIL import Image

# ---------------------------------------------------------------------------
# 2x3 仿射矩陣工具：(a, b, c, d, e, f) 代表 x' = a*x + b*y + c, y' = d*x + e*y + f
# ---------------------------------------------------------------------------
Affine = Tuple[float, float, float, float, float, float]
IDENTITY: Affine = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)


def mat_mul(m: Affine, n: Affine) -> Affine:
    """回傳 m * n（先套 n 再套 m）。"""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * D, a * B + b * E, a * C + b * F + c,
            d * A + e * D, d * B + e * E, d * C + e * F + f)


def mat_local(x: float, y: float, rot_deg: float, sx: float, sy: float) -> Affine:
    """T(x, y) * R(rot) * S(sx, sy)。"""
    r = math.radians(rot_deg)
    cs, sn = math.cos(r), math.sin(r)
    return (cs * sx, -sn * sy, x, sn * sx, cs * sy, y)


def mat_apply(m: Affine, x: float, y: float) -> Tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + b * y + c, d * x + e * y + f


def mat_inverse(m: Affine) -> Affine:
    a, b, c, d, e, f = m
    det = a * e - b * d
    if abs(det) < 1e-12:
        raise ValueError("矩陣不可逆")
    ia, ib, id_, ie = e / det, -b / det, -d / det, a / det
    return (ia, ib, -(ia * c + ib * f), id_, ie, -(id_ * c + ie * f))


def mat_scale_factor(m: Affine) -> float:
    """矩陣的平均縮放倍率（用來決定燈眼大小）。"""
    return math.sqrt(abs(m[0] * m[4] - m[1] * m[3]))


# ---------------------------------------------------------------------------
# 插值
# ---------------------------------------------------------------------------
def ease_in_out(u: float) -> float:
    """平滑的 ease-in-out（smoothstep 的三次版）。"""
    return u * u * (3.0 - 2.0 * u)


EASINGS = {
    "linear": lambda u: u,
    "ease": ease_in_out,
    "step": lambda u: 0.0,
}


def sample_track(keys: List[list], t: float) -> float:
    """在一條關鍵幀軌道上取 t 時刻的值。keys 已依時間排序。"""
    if not keys:
        raise ValueError("空的軌道")
    if t <= keys[0][0]:
        return float(keys[0][1])
    if t >= keys[-1][0]:
        return float(keys[-1][1])
    for i in range(len(keys) - 1):
        k0, k1 = keys[i], keys[i + 1]
        t0, t1 = k0[0], k1[0]
        if t0 <= t <= t1:
            if t1 <= t0:
                return float(k1[1])
            ease = k0[2] if len(k0) > 2 else "linear"
            u = EASINGS.get(ease, EASINGS["linear"])((t - t0) / (t1 - t0))
            return float(k0[1]) + (float(k1[1]) - float(k0[1])) * u
    return float(keys[-1][1])


CHANNELS = ("x", "y", "rot", "sx", "sy")
# 每個通道在「沒動畫」時的中性值（位移/旋轉 0，縮放 1）
NEUTRAL = {"x": 0.0, "y": 0.0, "rot": 0.0, "sx": 1.0, "sy": 1.0}


# ---------------------------------------------------------------------------
# 資料結構
# ---------------------------------------------------------------------------
@dataclass
class Part:
    image_path: str
    anchor: Tuple[float, float]
    image: Optional[Image.Image] = None


@dataclass
class Bone:
    name: str
    parent: Optional[str]
    pivot: Tuple[float, float]
    rot: float = 0.0
    sx: float = 1.0
    sy: float = 1.0
    z: int = 0
    part: Optional[Part] = None


@dataclass
class Animation:
    name: str
    duration: float
    loop: bool
    tracks: Dict[str, Dict[str, List[list]]] = field(default_factory=dict)

    def sample(self, t: float) -> Dict[str, Dict[str, float]]:
        """回傳 {骨頭: {通道: 值}}（值是相對 rest 的偏移／倍率）。"""
        if self.duration > 0:
            if self.loop:
                t = t % self.duration
            else:
                t = min(t, self.duration)
        out: Dict[str, Dict[str, float]] = {}
        for bone, chans in self.tracks.items():
            out[bone] = {ch: sample_track(keys, t) for ch, keys in chans.items()}
        return out


Pose = Dict[str, Dict[str, float]]


def blend_poses(a: Pose, b: Pose, w: float) -> Pose:
    """線性混合兩個姿勢（w=0 全 a，w=1 全 b），用於狀態切換時的淡入。"""
    out: Pose = {}
    for bone in set(a) | set(b):
        ca, cb = a.get(bone, {}), b.get(bone, {})
        out[bone] = {}
        for ch in set(ca) | set(cb):
            va = ca.get(ch, NEUTRAL[ch])
            vb = cb.get(ch, NEUTRAL[ch])
            out[bone][ch] = va + (vb - va) * w
    return out


class Skeleton:
    """載入後的骨架：骨頭、零件圖、動畫、臉部設定。"""

    def __init__(self, data: dict, base_dir: str = "."):
        self.data = data
        self.name = data.get("name", "unnamed")
        self.canvas = tuple(data.get("canvas", [800, 480]))
        self.base_dir = base_dir
        self.bones: "OrderedDict[str, Bone]" = OrderedDict()
        for b in data["bones"]:
            rest = b.get("rest", {})
            part = None
            if b.get("part"):
                p = b["part"]
                part = Part(os.path.join(base_dir, p["image"]), tuple(p.get("anchor", [0, 0])))
            bone = Bone(name=b["name"], parent=b.get("parent"), pivot=tuple(b.get("pivot", [0, 0])),
                        rot=float(rest.get("rot", 0)), sx=float(rest.get("sx", 1)),
                        sy=float(rest.get("sy", 1)), z=int(b.get("z", 0)), part=part)
            if bone.parent is not None and bone.parent not in self.bones:
                raise ValueError(f"骨頭 {bone.name} 的父骨頭 {bone.parent} 必須先定義")
            self.bones[bone.name] = bone
        self.animations: Dict[str, Animation] = {}
        for name, a in data.get("animations", {}).items():
            tracks = {}
            for bone, chans in a.get("tracks", {}).items():
                if bone not in self.bones:
                    raise ValueError(f"動畫 {name} 用到不存在的骨頭 {bone}")
                tracks[bone] = {}
                for ch, keys in chans.items():
                    if ch not in CHANNELS:
                        raise ValueError(f"動畫 {name} 有未知通道 {ch}")
                    tracks[bone][ch] = sorted(keys, key=lambda k: k[0])
            self.animations[name] = Animation(name, float(a.get("duration", 1.0)),
                                              bool(a.get("loop", True)), tracks)
        self.face = data.get("face", {})
        self.effects = data.get("effects", {})

    @classmethod
    def load(cls, path: str, load_images: bool = True) -> "Skeleton":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        sk = cls(data, base_dir=os.path.dirname(os.path.abspath(path)))
        if load_images:
            sk.load_images()
        return sk

    def load_images(self) -> None:
        for bone in self.bones.values():
            if bone.part and bone.part.image is None:
                bone.part.image = Image.open(bone.part.image_path).convert("RGBA")

    # -- 姿勢計算 --------------------------------------------------------
    def world_transforms(self, pose: Optional[Pose] = None) -> Dict[str, Affine]:
        """依姿勢計算每根骨頭的世界矩陣（父骨頭一定先算，因為定義順序保證）。"""
        pose = pose or {}
        world: Dict[str, Affine] = {}
        for name, bone in self.bones.items():
            ch = pose.get(name, {})
            local = mat_local(bone.pivot[0] + ch.get("x", 0.0),
                              bone.pivot[1] + ch.get("y", 0.0),
                              bone.rot + ch.get("rot", 0.0),
                              bone.sx * ch.get("sx", 1.0),
                              bone.sy * ch.get("sy", 1.0))
            world[name] = mat_mul(world[bone.parent], local) if bone.parent else local
        return world

    def draw_order(self) -> List[Bone]:
        bones = [b for b in self.bones.values() if b.part]
        return sorted(bones, key=lambda b: b.z)  # sorted 是穩定排序：同 z 依定義順序


class AnimationPlayer:
    """播放一個動畫，切換時用短暫交叉淡入（crossfade）讓動作不跳。"""

    def __init__(self, skeleton: Skeleton, fade: float = 0.25):
        self.skeleton = skeleton
        self.fade = fade
        self.current: Optional[str] = None
        self.time = 0.0
        self.prev: Optional[str] = None
        self.prev_time = 0.0
        self.fade_t = 1.0

    def play(self, name: str, restart: bool = False) -> None:
        if name not in self.skeleton.animations:
            raise KeyError(f"骨架 {self.skeleton.name} 沒有動畫 {name}")
        if name == self.current and not restart:
            return
        self.prev, self.prev_time = self.current, self.time
        self.current, self.time = name, 0.0
        self.fade_t = 0.0 if self.prev else 1.0

    def update(self, dt: float) -> None:
        self.time += dt
        self.prev_time += dt
        if self.fade > 0:
            self.fade_t = min(1.0, self.fade_t + dt / self.fade)
        else:
            self.fade_t = 1.0

    def pose(self) -> Pose:
        if not self.current:
            return {}
        cur = self.skeleton.animations[self.current].sample(self.time)
        if self.prev and self.fade_t < 1.0 and self.prev in self.skeleton.animations:
            old = self.skeleton.animations[self.prev].sample(self.prev_time)
            return blend_poses(old, cur, ease_in_out(self.fade_t))
        return cur


class PartCompositor:
    """把零件圖依世界矩陣貼到畫面上。

    為了 Pi 3B 的效能：純平移直接 paste；有旋轉／縮放時只轉換零件的包圍框，
    並以量化後的矩陣做 LRU 快取（呼吸、擺動是循環動作，命中率高）。
    """

    def __init__(self, cache_size: int = 48):
        self.cache: "OrderedDict[tuple, Image.Image]" = OrderedDict()
        self.cache_size = cache_size

    def draw(self, frame: Image.Image, part: Part, world: Affine) -> None:
        img = part.image
        if img is None:
            return
        m = mat_mul(world, (1.0, 0.0, -part.anchor[0], 0.0, 1.0, -part.anchor[1]))
        a, b, c, d, e, f = m
        if abs(a - 1) < 1e-3 and abs(e - 1) < 1e-3 and abs(b) < 1e-3 and abs(d) < 1e-3:
            frame.paste(img, (int(round(c)), int(round(f))), img)
            return
        # 量化線性部分（平移只取整數）→ 快取 key
        q = 400.0
        qa, qb, qd, qe = (round(a * q), round(b * q), round(d * q), round(e * q))
        key = (id(img), qa, qb, qd, qe)
        sprite_off = self.cache.get(key)
        if sprite_off is None:
            lin = (qa / q, qb / q, 0.0, qd / q, qe / q, 0.0)
            w, h = img.size
            pts = [mat_apply(lin, x, y) for x, y in ((0, 0), (w, 0), (0, h), (w, h))]
            x0 = math.floor(min(p[0] for p in pts)) - 1
            y0 = math.floor(min(p[1] for p in pts)) - 1
            x1 = math.ceil(max(p[0] for p in pts)) + 1
            y1 = math.ceil(max(p[1] for p in pts)) + 1
            shifted = mat_mul((1.0, 0.0, -x0, 0.0, 1.0, -y0), lin)
            inv = mat_inverse(shifted)
            sprite = img.transform((x1 - x0, y1 - y0), Image.AFFINE, inv, Image.BILINEAR)
            sprite_off = (sprite, x0, y0)
            self.cache[key] = sprite_off
            if len(self.cache) > self.cache_size:
                self.cache.popitem(last=False)
        else:
            self.cache.move_to_end(key)
        sprite, x0, y0 = sprite_off
        frame.paste(sprite, (int(round(c + x0)), int(round(f + y0))), sprite)
