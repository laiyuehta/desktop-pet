"""桌面預覽用的輸出端：寫 PNG 或收集成 GIF。"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

from PIL import Image

from .base import OutputSink

log = logging.getLogger("pet.preview")


class FileSink(OutputSink):
    """每 every 格把畫面寫成 latest.png（用圖片檢視器開著就能當預覽），
    也可以 save_all=True 存下每一格 frame_000001.png。"""
    name = "preview"

    def __init__(self, out_dir: str = "preview/live", every: int = 1, save_all: bool = False):
        self.out_dir = out_dir
        self.every = max(1, every)
        self.save_all = save_all
        self.count = 0

    def open(self) -> None:
        os.makedirs(self.out_dir, exist_ok=True)
        log.info("預覽畫面寫到 %s/latest.png", self.out_dir)

    def send(self, frame: Image.Image) -> None:
        self.count += 1
        if self.count % self.every:
            return
        if self.save_all:
            frame.save(os.path.join(self.out_dir, f"frame_{self.count:06d}.png"))
        tmp = os.path.join(self.out_dir, ".latest.tmp.png")
        frame.save(tmp)
        os.replace(tmp, os.path.join(self.out_dir, "latest.png"))   # 原子替換，檢視器不會讀到半張


class GifSink(OutputSink):
    """收集畫面，close() 時存成 GIF（可縮小、可抽格以控制檔案大小）。"""
    name = "gif"

    def __init__(self, path: str, fps: float = 20, keep_every: int = 2, scale: Optional[tuple] = (400, 240)):
        self.path = path
        self.fps = fps
        self.keep_every = max(1, keep_every)
        self.scale = scale
        self.frames: List[Image.Image] = []
        self.count = 0

    def send(self, frame: Image.Image) -> None:
        self.count += 1
        if (self.count - 1) % self.keep_every:
            return
        f = frame.resize(self.scale, Image.LANCZOS) if self.scale else frame.copy()
        self.frames.append(f.convert("RGB"))

    def close(self) -> None:
        if not self.frames:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        # 用第一格產生共用調色盤，減少閃爍與檔案大小
        pal_src = Image.new("RGB", (self.frames[0].width, self.frames[0].height * 3))
        for i, idx in enumerate((0, len(self.frames) // 2, len(self.frames) - 1)):
            pal_src.paste(self.frames[idx], (0, i * self.frames[0].height))
        pal = pal_src.quantize(colors=128, method=Image.Quantize.MEDIANCUT)
        frames = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in self.frames]
        duration = int(round(1000 * self.keep_every / self.fps))
        frames[0].save(self.path, save_all=True, append_images=frames[1:], duration=duration,
                       loop=0, optimize=False, disposal=1)
        log.info("GIF 已存：%s（%d 格）", self.path, len(frames))
