"""燈眼與嘴巴：全部用程式畫（不是貼圖）。

EyeController 管理表情參數（開合、形狀、亮度、眼神方向、自動眨眼），
FaceRenderer 依參數畫出發光的燈眼、額頭菱形和三角形嘴巴。
繪製結果會快取成小圖（sprite），同樣參數不重畫，減輕 Pi 3B 負擔。
"""

from __future__ import annotations

import math
import random
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter

from .skeleton import Affine, mat_apply, mat_rotation_deg, mat_scale_factor

# 顏色（燈眼黃）
CORE = (255, 232, 96)
CORE_HI = (255, 252, 214)
GLOW = (255, 200, 40)


@dataclass
class EyeState:
    shape: str = "round"     # round（圓眼）/ happy（^ ^ 瞇眼笑）/ closed（睡覺 ︶）
    open: float = 1.0        # 開合 0~1（round 才有意義；眨眼、半睜）
    glow: float = 1.0        # 亮度倍率
    size: float = 1.0        # 大小倍率（睜大 >1）
    look_x: float = 0.0      # 眼神左右 -1~1
    look_y: float = 0.0      # 眼神上下 -1~1
    mouth: str = "small"     # small / open / flat / tiny


# 表情預設（狀態機的 "eyes" 欄位填這裡的名稱）
EXPRESSIONS: Dict[str, EyeState] = {
    "normal": EyeState(),
    "happy": EyeState(shape="happy", glow=1.15, mouth="open"),
    "bored": EyeState(open=0.5, glow=0.75, look_y=0.3, mouth="flat"),
    "sleep": EyeState(shape="closed", glow=0.5, mouth="tiny"),
    "squint": EyeState(open=0.35, glow=0.9),
    "surprised": EyeState(size=1.15, glow=1.3, mouth="open"),
}


class EyeController:
    """表情控制：目標表情 + 自動眨眼 + 眼神飄移 + 平滑過渡。"""

    def __init__(self, seed: Optional[int] = None):
        self.rng = random.Random(seed)
        self.expression = "normal"
        self.target = replace(EXPRESSIONS["normal"])
        self.current = replace(self.target)
        self.blink_enabled = True
        self.wander_enabled = True
        self._next_blink = self.rng.uniform(2.0, 5.0)
        self._blink_t = -1.0          # <0 表示沒在眨眼
        self._blink_dur = 0.16
        self._next_look = self.rng.uniform(1.5, 3.5)
        self._look_target = (0.0, 0.0)
        self._override: Optional[Tuple[str, float]] = None  # (表情, 剩餘秒數)

    # -- 外部 API ----------------------------------------------------------
    def set_expression(self, name: str) -> None:
        if name not in EXPRESSIONS:
            raise KeyError(f"未知的表情 {name}")
        self.expression = name
        self.target = replace(EXPRESSIONS[name])
        # 睡覺、開心時不自動眨眼／飄眼神
        self.blink_enabled = self.target.shape == "round"
        self.wander_enabled = name in ("normal", "bored")
        if not self.wander_enabled:
            self._look_target = (0.0, 0.0)

    def override(self, name: str, seconds: float) -> None:
        """暫時換表情（例如擴充模組的動作請求），時間到回到原表情。"""
        if name not in EXPRESSIONS:
            raise KeyError(f"未知的表情 {name}")
        self._override = (name, seconds)

    def look(self, x: float, y: float = 0.0) -> None:
        self._look_target = (max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y)))
        self._next_look = self.rng.uniform(2.0, 4.0)

    def blink(self) -> None:
        if self._blink_t < 0:
            self._blink_t = 0.0

    # -- 每格更新 ------------------------------------------------------------
    def update(self, dt: float) -> EyeState:
        tgt = self.target
        if self._override:
            name, left = self._override
            left -= dt
            self._override = (name, left) if left > 0 else None
            tgt = EXPRESSIONS[name]

        # 自動眨眼
        if self.blink_enabled:
            self._next_blink -= dt
            if self._next_blink <= 0 and self._blink_t < 0:
                self._blink_t = 0.0
                self._next_blink = self.rng.uniform(2.5, 6.0)
                if self.rng.random() < 0.15:     # 偶爾連眨兩下
                    self._next_blink = 0.35
        blink_factor = 1.0
        if self._blink_t >= 0:
            self._blink_t += dt
            u = self._blink_t / self._blink_dur
            if u >= 1.0:
                self._blink_t = -1.0
            else:
                blink_factor = abs(1.0 - 2.0 * u)  # 1 → 0 → 1

        # 眼神飄移
        if self.wander_enabled:
            self._next_look -= dt
            if self._next_look <= 0:
                self._next_look = self.rng.uniform(1.5, 4.0)
                if self.rng.random() < 0.45:
                    self._look_target = (0.0, 0.0)
                else:
                    self._look_target = (self.rng.choice([-1, 1]) * self.rng.uniform(0.5, 1.0),
                                         self.rng.uniform(-0.3, 0.3))

        # 平滑趨近目標（指數平滑）
        k = 1.0 - math.exp(-dt * 12.0)
        c = self.current
        c.open += (tgt.open - c.open) * k
        c.glow += (tgt.glow - c.glow) * k
        c.size += (tgt.size - c.size) * k
        lx = self._look_target[0] if self.wander_enabled or self._look_target != (0, 0) else 0.0
        ly = self._look_target[1] + tgt.look_y
        kl = 1.0 - math.exp(-dt * 8.0)
        c.look_x += (lx - c.look_x) * kl
        c.look_y += (ly - c.look_y) * kl
        c.shape = tgt.shape
        c.mouth = tgt.mouth
        out = replace(c)
        out.open = max(0.0, min(1.0, c.open * blink_factor))
        return out


class FaceRenderer:
    """畫燈眼、額頭菱形、嘴巴。座標都是臉部骨頭的局部座標。"""

    def __init__(self, face_cfg: dict, cache_size: int = 400):
        self.cfg = face_cfg
        self.eye_r = float(face_cfg.get("eye_radius", 42))
        self.eye_pos = [tuple(p) for p in face_cfg.get("eyes", [[-66, 8], [66, 8]])]
        self.mouth_pos = tuple(face_cfg.get("mouth", [0, 70]))
        self.diamond_pos = tuple(face_cfg.get("diamond", [0, -52]))
        self.look_px = float(face_cfg.get("look_px", 14))
        self.cache: "OrderedDict[tuple, Image.Image]" = OrderedDict()
        self.cache_size = cache_size

    # -- 快取 ----------------------------------------------------------------
    def _cached(self, key, fn) -> Image.Image:
        img = self.cache.get(key)
        if img is None:
            img = fn()
            self.cache[key] = img
            if len(self.cache) > self.cache_size:
                self.cache.popitem(last=False)
        else:
            self.cache.move_to_end(key)
        return img

    @staticmethod
    def _glowify(mask: Image.Image, glow: float, blur: float, core=CORE) -> Image.Image:
        """把白色遮罩變成「核心 + 外光暈」的 RGBA 圖。"""
        size = mask.size
        halo = mask.filter(ImageFilter.GaussianBlur(blur))
        halo = halo.point(lambda v: min(255, int(v * 0.85 * glow)))
        out = Image.new("RGBA", size, GLOW + (0,))
        out.putalpha(halo)
        core_img = Image.new("RGBA", size, core + (0,))
        core_img.putalpha(mask.point(lambda v: min(255, int(v * min(1.0, 0.55 + 0.45 * glow)))))
        out.alpha_composite(core_img)
        return out

    def _eye_sprite(self, shape: str, open_q: int, glow_q: int, r: int) -> Image.Image:
        def make():
            ss = 2                       # 2 倍超取樣做反鋸齒
            pad = int(r * 0.6)
            S = (2 * r + 2 * pad) * ss
            cx = cy = S / 2
            R = r * ss
            mask = Image.new("L", (S, S), 0)
            dr = ImageDraw.Draw(mask)
            openness = open_q / 20.0
            if shape == "happy":        # ^ ^：上半圓弧
                w = max(2, int(R * 0.32))
                dr.arc((cx - R * 0.9, cy - R * 0.55, cx + R * 0.9, cy + R * 1.05), 200, 340, fill=255, width=w)
            elif shape == "closed":     # ︶：下半圓弧
                w = max(2, int(R * 0.22))
                dr.arc((cx - R * 0.85, cy - R * 0.9, cx + R * 0.85, cy + R * 0.45), 25, 155, fill=255, width=w)
            else:
                if openness < 0.12:     # 幾乎閉上：一條橫線
                    h = R * 0.14
                    dr.rounded_rectangle((cx - R * 0.9, cy - h, cx + R * 0.9, cy + h), radius=h, fill=255)
                else:
                    dr.ellipse((cx - R, cy - R, cx + R, cy + R), fill=255)
                    if openness < 0.999:  # 平的眼皮從上往下蓋（半睜／眨眼）
                        lid = cy - R + 2 * R * (1.0 - openness)
                        # 眨眼時下緣也略收，看起來像燈在縮
                        dr.rectangle((0, 0, S, lid), fill=0)
                    # 亮點
            core = mask.resize((S // ss, S // ss), Image.LANCZOS)
            sprite = self._glowify(core, glow_q / 10.0, blur=r * 0.28)
            if shape == "round" and openness >= 0.4:
                hi = Image.new("L", core.size, 0)
                hd = ImageDraw.Draw(hi)
                c2 = core.size[0] / 2
                hd.ellipse((c2 - r * 0.55, c2 - r * 0.6, c2 - r * 0.05, c2 - r * 0.15), fill=150)
                hi = hi.filter(ImageFilter.GaussianBlur(r * 0.12))
                hi = Image.composite(hi, Image.new("L", core.size, 0), core)
                layer = Image.new("RGBA", core.size, CORE_HI + (0,))
                layer.putalpha(hi)
                sprite.alpha_composite(layer)
            return sprite
        return self._cached(("eye", shape, open_q, glow_q, r), make)

    def _mouth_sprite(self, kind: str, glow_q: int, s: int) -> Image.Image:
        def make():
            ss = 2
            S = s * 4 * ss
            c = S / 2
            u = s * ss
            mask = Image.new("L", (S, S), 0)
            d = ImageDraw.Draw(mask)
            if kind == "open":
                d.polygon([(c - u * 1.0, c - u * 0.55), (c + u * 1.0, c - u * 0.55), (c, c + u * 0.9)], fill=255)
            elif kind == "flat":
                d.rounded_rectangle((c - u * 0.7, c - u * 0.12, c + u * 0.7, c + u * 0.12), radius=u * 0.12, fill=255)
            elif kind == "tiny":
                d.polygon([(c - u * 0.35, c - u * 0.2), (c + u * 0.35, c - u * 0.2), (c, c + u * 0.3)], fill=255)
            else:  # small：小三角形（尖端朝下）
                d.polygon([(c - u * 0.6, c - u * 0.35), (c + u * 0.6, c - u * 0.35), (c, c + u * 0.55)], fill=255)
            core = mask.resize((S // ss, S // ss), Image.LANCZOS)
            return self._glowify(core, glow_q / 10.0, blur=s * 0.35)
        return self._cached(("mouth", kind, glow_q, s), make)

    def _diamond_sprite(self, s: int, glow_q: int) -> Image.Image:
        def make():
            ss = 2
            S = s * 4 * ss
            c = S / 2
            u = s * ss
            mask = Image.new("L", (S, S), 0)
            ImageDraw.Draw(mask).polygon([(c, c - u * 0.75), (c + u * 0.5, c), (c, c + u * 0.75), (c - u * 0.5, c)], fill=255)
            core = mask.resize((S // ss, S // ss), Image.LANCZOS)
            return self._glowify(core, glow_q / 10.0, blur=s * 0.3, core=(255, 226, 120))
        return self._cached(("diamond", s, glow_q), make)

    ROT_STEP = 2   # 旋轉角量化（度），讓旋轉後的小圖可以快取

    def _rotated(self, key, sprite: Image.Image, angle_q: int) -> Image.Image:
        """跟著身體轉（滾動時燈眼、嘴、菱形一起轉）。"""
        if angle_q % 360 == 0:
            return sprite
        return self._cached(("rot", key, angle_q),
                            lambda: sprite.rotate(-angle_q, resample=Image.BICUBIC, expand=True))

    @staticmethod
    def _paste_center(frame: Image.Image, sprite: Image.Image, x: float, y: float) -> None:
        w, h = sprite.size
        frame.paste(sprite, (int(round(x - w / 2)), int(round(y - h / 2))), sprite)

    def draw(self, frame: Image.Image, face_world: Affine, st: EyeState) -> None:
        scale = mat_scale_factor(face_world)
        glow_q = max(1, min(15, int(round(st.glow * 10))))
        r = max(4, int(round(self.eye_r * scale * st.size)))
        open_q = int(round(st.open * 20))
        dx = st.look_x * self.look_px
        dy = st.look_y * self.look_px * 0.6
        # 位置用臉部骨頭的矩陣換算（含旋轉），小圖本身也依同一角度轉
        angle_q = int(round(mat_rotation_deg(face_world) / self.ROT_STEP)) * self.ROT_STEP
        # 額頭菱形
        ds = max(4, int(round(12 * scale)))
        x, y = mat_apply(face_world, self.diamond_pos[0] + dx * 0.3, self.diamond_pos[1])
        self._paste_center(frame, self._rotated(("diamond", ds, glow_q), self._diamond_sprite(ds, glow_q), angle_q), x, y)
        # 兩顆燈眼
        eye = self._rotated(("eye", st.shape, open_q, glow_q, r), self._eye_sprite(st.shape, open_q, glow_q, r), angle_q)
        for ex, ey in self.eye_pos:
            x, y = mat_apply(face_world, ex + dx, ey + dy)
            self._paste_center(frame, eye, x, y)
        # 嘴巴
        ms = max(3, int(round(13 * scale)))
        x, y = mat_apply(face_world, self.mouth_pos[0] + dx * 0.6, self.mouth_pos[1] + dy * 0.4)
        self._paste_center(frame, self._rotated(("mouth", st.mouth, glow_q, ms), self._mouth_sprite(st.mouth, glow_q, ms), angle_q), x, y)
