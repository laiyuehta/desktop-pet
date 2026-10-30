"""預錄動畫包（animation pack）：格式、讀取、Pi 播放器。

動畫包由 tools/bake.py 用骨架系統離線產生（見 README「動畫包」），Pi 上只負責播放：
依狀態機目前狀態挑片段，把事先編好的 800×480 baseline JPEG 直接送給 piscreen，
不繪圖、不重新編碼；只有對話框出現時才解碼該格、畫上對話框、重新編碼。

目錄結構（~/.local/share/desktop-pet/packs/<外觀>/，在 repo 外）：
    manifest.json   片段、狀態對照、過渡片段、每格在 frames.bin 的位置
    frames.bin      所有 JPEG 幀串在一起（重複的幀只存一次）
    preview/        預覽 GIF / PNG（給人看的，Pi 用不到）

manifest.json（format 1）重點欄位：
    fps, size, skeleton（骨架名稱）, look（外觀名稱）
    clips:  {片段 ID: {state, kind: loop|expr|transition, loop, frames: [[offset, length, anchor_x, anchor_y], ...],
                       cuts: [可以切出去的格], busy: [[開始, 結束), ...] 眼睛不在基本表情的區間, weight, expression}}
    states: {狀態: {frames: 一輪格數, clips: [變化版片段 ID], expressions: {表情: 片段 ID}, cut_every}}
    transitions: {"A>B": {"<A 的切換格>": {clip: 片段 ID, enter: 進入 B 的第幾格}}}
"""

from __future__ import annotations

import io
import json
import logging
import mmap
import os
import random
import time
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("pet.pack")

FORMAT = 1
MANIFEST = "manifest.json"
FRAMES = "frames.bin"

FrameRef = Tuple[str, int]


class PackError(Exception):
    pass


class AnimationPack:
    """讀取一個動畫包；幀資料用 mmap（交給作業系統快取，不佔 Python 記憶體）。"""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        mpath = os.path.join(self.path, MANIFEST)
        try:
            with open(mpath, "r", encoding="utf-8") as fh:
                m = json.load(fh)
        except (OSError, ValueError) as exc:
            raise PackError(f"讀不到動畫包 {mpath}：{exc}") from exc
        if m.get("format") != FORMAT:
            raise PackError(f"動畫包格式 {m.get('format')} 不支援（需要 {FORMAT}）")
        self.manifest = m
        self.fps = float(m.get("fps", 20))
        self.size = tuple(m.get("size", [800, 480]))
        self.skeleton = m.get("skeleton")
        self.look = m.get("look", self.skeleton)
        self.clips: Dict[str, dict] = m.get("clips", {})
        self.states: Dict[str, dict] = m.get("states", {})
        self.transitions: Dict[str, Dict[str, dict]] = m.get("transitions", {})
        for cid, c in self.clips.items():
            c["_cuts"] = set(c.get("cuts", []))
            if not c.get("frames"):
                raise PackError(f"片段 {cid} 沒有幀")
        self._fh = open(os.path.join(self.path, FRAMES), "rb")
        size = os.fstat(self._fh.fileno()).st_size
        self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ) if size else None
        self.bytes_total = size

    @classmethod
    def try_load(cls, path: Optional[str]) -> Optional["AnimationPack"]:
        if not path or not os.path.exists(os.path.join(path, MANIFEST)):
            return None
        try:
            return cls(path)
        except PackError as exc:
            log.warning("%s；改用即時繪圖", exc)
            return None

    def frame(self, clip: str, i: int) -> bytes:
        off, n = self.clips[clip]["frames"][i][:2]
        return self._mm[off:off + n]

    def anchor(self, clip: str, i: int) -> Tuple[float, float]:
        f = self.clips[clip]["frames"][i]
        return (f[2], f[3]) if len(f) >= 4 else (self.size[0] / 2, 120.0)

    def frame_count(self) -> int:
        return sum(len(c["frames"]) for c in self.clips.values())

    def close(self) -> None:
        if self._mm is not None:
            self._mm.close()
            self._mm = None
        self._fh.close()


def _busy(clip: dict, i: int) -> bool:
    return any(a <= i < b for a, b in clip.get("busy", []))


class PackPlayer:
    """純邏輯：每格決定要播哪個片段的第幾格（不碰畫面），方便測試。

    - 進入狀態：從該狀態的變化版中隨機挑一個（依 weight），每播完一輪再隨機挑一次。
    - 換狀態：等目前片段播到切換點（cuts），有 A→B 過渡片段就播過渡，沒有就直接從 B 的第 0 格開始。
    - 暫時表情（後端 emotion、擴充模組 expression）：有烘好的表情片段就在同一格換過去（動作不跳），
      表情結束再換回基本版；沒有就忽略表情。
    - 動畫包沒有這個狀態、或骨架（外觀）不同：回傳 None，由呼叫端退回即時繪圖。
    """

    def __init__(self, pack: AnimationPack, rng: Optional[random.Random] = None):
        self.pack = pack
        self.rng = rng or random.Random()
        self.state: Optional[str] = None
        self.clip: Optional[str] = None
        self.i = 0
        self.mode: Optional[str] = None          # "state" | "trans"
        self._target: Optional[str] = None
        self._enter = 0
        self.live = False
        self.reaction_ms: Optional[float] = None
        self.waited_frames = 0
        self.switches = 0

    # -- 查詢 -----------------------------------------------------------------
    def has_state(self, state: str) -> bool:
        st = self.pack.states.get(state)
        return bool(st and [c for c in st.get("clips", []) if c in self.pack.clips])

    def supports(self, state: str, skeleton: Optional[str] = None) -> bool:
        if skeleton is not None and self.pack.skeleton and skeleton != self.pack.skeleton:
            return False
        return self.has_state(state)

    # -- 內部 -----------------------------------------------------------------
    def _pick_variant(self, state: str, idx: int) -> str:
        st = self.pack.states[state]
        cands = [c for c in st["clips"] if c in self.pack.clips and idx < len(self.pack.clips[c]["frames"])
                 and not _busy(self.pack.clips[c], idx)]
        if not cands:
            cands = [st["clips"][0]]
        weights = [float(self.pack.clips[c].get("weight", 1.0)) for c in cands]
        return self.rng.choices(cands, weights=weights, k=1)[0]

    def _start_state(self, state: str, idx: int = 0, override: Optional[str] = None) -> None:
        self.state, self.mode, self.i = state, "state", idx
        expr = self.pack.states[state].get("expressions", {}).get(override) if override else None
        self.clip = expr if expr in self.pack.clips else self._pick_variant(state, idx)

    def _len(self, clip: Optional[str] = None) -> int:
        return len(self.pack.clips[clip or self.clip]["frames"])

    def _mark_switch(self, change_ts: Optional[float]) -> None:
        self.switches += 1
        if change_ts:
            self.reaction_ms = max(0.0, (time.time() - change_ts) * 1000.0)

    # -- 每格 -----------------------------------------------------------------
    def next(self, state: str, skeleton: Optional[str] = None, override: Optional[str] = None,
             change_ts: Optional[float] = None) -> Optional[FrameRef]:
        """推進一格，回傳 (片段 ID, 格數)；None 表示這格要退回即時繪圖。"""
        if not self.supports(state, skeleton):
            if not self.live:
                log.info("動畫包沒有狀態 %s（或外觀 %s 不同），改用即時繪圖", state, skeleton)
            self.live, self.state, self.mode = True, None, None
            return None
        if self.live or self.state is None:
            first = self.state is None and not self.live and self.switches == 0
            self.live = False
            self._start_state(state, 0, override)
            if not first:
                self._mark_switch(change_ts)
            return self.clip, self.i

        if self.mode == "trans":
            self.i += 1
            if self.i < self._len():
                return self.clip, self.i
            tgt = self._target
            if not self.has_state(tgt):
                self._start_state(state, 0, override)
            else:
                n = len(self.pack.clips[self.pack.states[tgt]["clips"][0]]["frames"])
                self._start_state(tgt, min(self._enter, n - 1), override)
            return self.clip, self.i

        # 一般播放中
        cur = self.pack.clips[self.clip]
        if state != self.state:
            at_end = not cur.get("loop", True) and self.i >= self._len() - 1
            if self.i in cur["_cuts"] or not cur["_cuts"] or at_end:
                key = f"{self.state}>{state}"
                tr = self.pack.transitions.get(key, {}).get(str(self.i))
                self.waited_frames = 0
                if tr and tr.get("clip") in self.pack.clips:
                    self.mode, self.clip, self.i = "trans", tr["clip"], 0
                    self._target, self._enter = state, int(tr.get("enter", 0))
                else:
                    self._start_state(state, 0, override)
                self._mark_switch(change_ts)
                return self.clip, self.i
            self.waited_frames += 1

        # 暫時表情：同一格換片段，動作不跳
        st = self.pack.states[self.state]
        want_expr = st.get("expressions", {}).get(override) if override else None
        if want_expr in self.pack.clips and self.clip != want_expr:
            self.clip = want_expr
        elif want_expr is None and cur.get("kind") == "expr":
            self.clip = self._pick_variant(self.state, (self.i + 1) % self._len())

        self.i += 1
        if self.i >= self._len():
            if self.pack.clips[self.clip].get("loop", True):
                self.i = 0
                if self.pack.clips[self.clip].get("kind") != "expr":
                    self.clip = self._pick_variant(self.state, 0)
            else:
                self.i = self._len() - 1
        return self.clip, self.i


class PackDriver:
    """把播放器接到 PetApp 與輸出端：每格呼叫 emit()。"""

    def __init__(self, pack: AnimationPack, app, sink, quality: int = 80, rng: Optional[random.Random] = None):
        self.pack, self.app, self.sink, self.quality = pack, app, sink, quality
        self.player = PackPlayer(pack, rng=rng)
        self.stats = {"pack": 0, "bubble": 0, "live": 0}
        self.mode = "pack"
        self._ref: Optional[FrameRef] = None
        missing = [s for s in app.fsm.states if not self.player.has_state(s)]
        if missing:
            log.warning("動畫包 %s 缺少狀態 %s，這些狀態會用即時繪圖（請重新 bake）", pack.look, missing)
        log.info("動畫包：%s（%d 段片段、%d 格、%.1f MB、%g fps）", pack.path, len(pack.clips),
                 pack.frame_count(), pack.bytes_total / 1e6, pack.fps)

    def emit(self) -> str:
        app = self.app
        ref = self.player.next(app.fsm.state, app.renderer.skeleton.name, app.eyes.active_override,
                               app.state_changed_ts)
        self._ref = ref
        if ref is None:
            self.mode = "live"
            self.stats["live"] += 1
            self.sink.frame_meta = {"mode": "live"}
            self.sink.send(app.render())
        else:
            data = self.pack.frame(*ref)
            bubble = app.renderer._bubble
            if bubble is not None and bubble.active:
                from PIL import Image
                img = Image.open(io.BytesIO(data))
                img.load()
                if img.mode != "RGB":
                    img = img.convert("RGB")
                ax, ay = self.pack.anchor(*ref)
                bubble.draw(img, ax, ay)
                self.mode = "bubble"
                self.stats["bubble"] += 1
                self.sink.frame_meta = {"mode": "bubble", "clip": ref[0], "frame": ref[1]}
                self.sink.send(img)
            else:
                self.mode = "pack"
                self.stats["pack"] += 1
                self.sink.frame_meta = {"mode": "pack", "clip": ref[0], "frame": ref[1]}
                self.sink.send_jpeg(data)
        app.render_info = self.info()
        return self.mode

    def info(self) -> dict:
        p = self.player
        return {"mode": self.mode, "pack": self.pack.look, "clip": p.clip if self._ref else None,
                "frame": p.i if self._ref else None,
                "reaction_ms": round(p.reaction_ms, 1) if p.reaction_ms is not None else None,
                "frames": dict(self.stats)}
