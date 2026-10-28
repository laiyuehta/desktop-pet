"""畫面合成：背景 → 地面陰影 → 骨架零件 → 燈眼/嘴巴 → 特效（睡覺的 Z）。

輸出 800×480 RGB 的 PIL Image。
"""

from __future__ import annotations

import math
from typing import Dict, Optional

from PIL import Image, ImageDraw, ImageFilter

from .bubble import SpeechBubble
from .eyes import EyeState, FaceRenderer
from .skeleton import AnimationPlayer, PartCompositor, Skeleton, mat_apply


def make_background(size) -> Image.Image:
    """深色漸層背景（燈眼在暗底上比較好看），只算一次。"""
    w, h = size
    top, bottom = (12, 18, 32), (30, 42, 66)
    col = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        col.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    bg = col.resize((w, h))
    # 地面一道很淡的光
    floor = Image.new("L", (w, h), 0)
    ImageDraw.Draw(floor).ellipse((w * 0.1, h * 0.86, w * 0.9, h * 1.12), fill=40)
    floor = floor.filter(ImageFilter.GaussianBlur(30))
    bg.paste((70, 100, 140), (0, 0), floor)
    return bg


class Renderer:
    def __init__(self, skeleton: Skeleton, size=(800, 480), font_path: Optional[str] = None):
        self.size = tuple(size)
        self.font_path = font_path
        self._bubble: Optional[SpeechBubble] = None   # 第一次說話時才載入字型
        self.background = make_background(self.size)
        self.parts = PartCompositor()
        self.set_skeleton(skeleton)
        self.effects = set()          # 例如 {"zzz"}
        self.effect_time = 0.0
        self.zzz_period = 2.4         # Z 飄一輪的秒數（動畫包產生工具會調成能整除片段長度）
        self.last_bubble_anchor = (self.size[0] / 2, 120.0)   # 上一格對話框尾巴指向的點（動畫包存每格的值）
        self._shadow_cache: Dict[int, Image.Image] = {}

    def set_skeleton(self, skeleton: Skeleton) -> None:
        self.skeleton = skeleton
        self.player = AnimationPlayer(skeleton)
        self.face = FaceRenderer(skeleton.face)
        self.parts.cache.clear()

    @property
    def bubble(self) -> SpeechBubble:
        if self._bubble is None:
            self._bubble = SpeechBubble(self.font_path)
        return self._bubble

    def say(self, text: str, seconds: float = 4.0) -> None:
        self.bubble.say(text, seconds)

    def update(self, dt: float) -> None:
        if self._bubble is not None:
            self._bubble.update(dt)
        self.player.update(dt)
        self.effect_time += dt

    # -- 繪製 ----------------------------------------------------------------
    def _shadow(self, width: int, height: int) -> Image.Image:
        key = width // 4
        img = self._shadow_cache.get(key)
        if img is None:
            w = key * 4
            img = Image.new("L", (w + 40, height + 40), 0)
            ImageDraw.Draw(img).ellipse((20, 20, 20 + w, 20 + height), fill=120)
            img = img.filter(ImageFilter.GaussianBlur(9))
            self._shadow_cache[key] = img
        return img

    def _draw_zzz(self, frame: Image.Image, x: float, y: float) -> None:
        d = ImageDraw.Draw(frame)
        period = self.zzz_period
        for i in range(3):
            u = ((self.effect_time + i * period / 3) % period) / period  # 0..1 往上飄
            size = 10 + 14 * u
            zx = x + 30 * u + 8 * math.sin(u * 6.28)
            zy = y - 70 * u
            fade = math.sin(u * math.pi)
            c = tuple(int(v * fade + b * (1 - fade)) for v, b in zip((180, 210, 255), (16, 24, 40)))
            pts = [(zx - size / 2, zy - size / 2), (zx + size / 2, zy - size / 2),
                   (zx - size / 2, zy + size / 2), (zx + size / 2, zy + size / 2)]
            d.line(pts, fill=c, width=max(2, int(size / 6)), joint="curve")

    def render(self, eyes: EyeState) -> Image.Image:
        frame = self.background.copy()
        sk = self.skeleton
        pose = self.player.pose()
        world = sk.world_transforms(pose)

        # 地面陰影：身體跳越高越小越淡
        sh = sk.data.get("shadow")
        if sh:
            rest_y = sk.bones[sh["bone"]].pivot[1]
            _, ry = mat_apply(world[sh["bone"]], 0, 0)
            lift = max(0.0, rest_y - ry)
            k = max(0.5, 1.0 - lift / 120.0)
            w = int(sh["width"] * k)
            img = self._shadow(w, sh["height"])
            rx, _ = mat_apply(world[sh["bone"]], 0, 0)
            frame.paste((4, 8, 16), (int(rx - img.size[0] / 2), int(rest_y - img.size[1] / 2)), img)

        for bone in sk.draw_order():
            self.parts.draw(frame, bone.part, world[bone.name])

        face_bone = sk.face.get("bone")
        if face_bone in world:
            self.face.draw(frame, world[face_bone], eyes)
            if "zzz" in self.effects:
                x, y = mat_apply(world[face_bone], 120, -120)
                self._draw_zzz(frame, x, y)
        # 泡泡尾巴指向球的上緣（跟著球左右移動）
        bx, by = mat_apply(world.get("body", world[sk.draw_order()[0].name]), 0, 0)
        self.last_bubble_anchor = (bx, by - 150)
        if self._bubble is not None and self._bubble.active:
            self._bubble.draw(frame, bx, by - 150)
        return frame
