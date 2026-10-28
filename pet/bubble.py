"""對話框（說話泡泡）：圓角白框 + 小尾巴，中文字型自動尋找。

字型尋找順序：--font 指定的路徑 → Raspberry Pi OS / Debian 常見中文字型 → Windows / macOS → Pillow 預設字型
（預設字型沒有中文，會顯示成方塊，並在 log 提示安裝字型）。
Pi 上安裝中文字型：sudo apt install fonts-wqy-microhei（小）或 fonts-noto-cjk（較大、較好看）
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("pet.bubble")

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "C:/Windows/Fonts/msjh.ttc",
    "C:/Windows/Fonts/mingliu.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


def _pick_tc_index(path: str, size: int) -> int:
    """.ttc 裡有多個字型時，優先選繁中（名稱含 TC）。"""
    if not path.lower().endswith(".ttc"):
        return 0
    for i in range(16):
        try:
            f = ImageFont.truetype(path, size, index=i)
        except (OSError, ValueError):
            break
        name = " ".join(f.getname())
        if " TC" in name or "TC " in name or "JhengHei" in name:
            return i
    return 0


def load_cjk_font(size: int, path: Optional[str] = None) -> Tuple[ImageFont.ImageFont, Optional[str]]:
    paths = ([path] if path else []) + FONT_CANDIDATES
    for p in paths:
        if p and os.path.exists(p):
            try:
                return ImageFont.truetype(p, size, index=_pick_tc_index(p, size)), p
            except (OSError, ValueError) as exc:
                log.warning("字型 %s 載入失敗：%s", p, exc)
    log.warning("找不到中文字型，對話框中文會變方塊。Pi 上可以：sudo apt install fonts-wqy-microhei")
    try:
        return ImageFont.load_default(size=size), None
    except TypeError:       # Pillow < 10.1
        return ImageFont.load_default(), None


def wrap_text(text: str, font, max_width: int, draw: ImageDraw.ImageDraw, max_lines: int = 3) -> List[str]:
    """逐字斷行（中文沒有空白，所以一個字一個字量寬度）。"""
    lines, cur = [], ""
    for ch in text.replace("\n", " "):
        trial = cur + ch
        if draw.textlength(trial, font=font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = ch.lstrip()
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and "".join(lines) != text.replace("\n", " "):
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_width:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


class SpeechBubble:
    def __init__(self, font_path: Optional[str] = None, size: int = 28, max_width: int = 560):
        self.font, self.font_path = load_cjk_font(size, font_path)
        self.size = size
        self.max_width = max_width
        self.text: Optional[str] = None
        self.remaining = 0.0
        self._img: Optional[Image.Image] = None

    def say(self, text: str, seconds: float = 4.0) -> None:
        text = (text or "").strip()
        if not text:
            return
        if text != self.text:
            self._img = None
        self.text = text
        self.remaining = max(0.5, float(seconds))

    def clear(self) -> None:
        self.text, self.remaining, self._img = None, 0.0, None

    def update(self, dt: float) -> None:
        if self.text:
            self.remaining -= dt
            if self.remaining <= 0:
                self.clear()

    @property
    def active(self) -> bool:
        return bool(self.text)

    def image(self) -> Image.Image:
        """框 + 尾巴的 RGBA 圖（尾巴在下方中央）。同一句話只畫一次。"""
        if self._img is not None:
            return self._img
        tmp = ImageDraw.Draw(Image.new("L", (1, 1)))
        lines = wrap_text(self.text, self.font, self.max_width, tmp)
        line_h = int(self.size * 1.3)
        w = int(max(tmp.textlength(l, font=self.font) for l in lines)) + 36
        w = max(w, 80)
        h = line_h * len(lines) + 22
        tail = 16
        img = Image.new("RGBA", (w + 4, h + tail + 4), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        fill, edge = (250, 252, 255, 238), (120, 160, 210, 255)
        d.rounded_rectangle((2, 2, w, h), radius=18, fill=fill, outline=edge, width=3)
        cx = w // 2
        d.polygon([(cx - 12, h - 2), (cx + 12, h - 2), (cx, h + tail)], fill=fill)
        d.line([(cx - 12, h), (cx, h + tail), (cx + 12, h)], fill=edge, width=3, joint="curve")
        y = 12
        for line in lines:
            lw = d.textlength(line, font=self.font)
            d.text(((w - lw) / 2 + 1, y), line, font=self.font, fill=(30, 45, 70, 255))
            y += line_h
        self._img = img
        return img

    def draw(self, frame: Image.Image, anchor_x: float, anchor_y: float) -> None:
        """把泡泡畫在 (anchor_x, anchor_y) 上方（尾巴尖端指向這一點），左右不超出畫面。"""
        if not self.active:
            return
        img = self.image()
        W = frame.size[0]
        x = int(anchor_x - img.size[0] / 2)
        x = max(6, min(W - img.size[0] - 6, x))
        y = int(anchor_y - img.size[1])
        y = max(4, y)
        frame.paste(img, (x, y), img)
