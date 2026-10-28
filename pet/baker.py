"""動畫包產生工具（核心邏輯；命令列入口是 tools/bake.py）。

用現有的骨架系統（skeleton.py / renderer.py / eyes.py + 骨架 JSON + 零件貼圖 + config/states.json）
把每個狀態的動畫預先算成 800×480 baseline JPEG，寫成動畫包（格式見 pet/pack.py）。

產生的片段：
- 狀態片段（kind=loop）：每個狀態一段或多段變化版（眨眼、看左右…），長度＝骨架動畫一輪。
  同一狀態的所有變化版姿勢逐格相同，只有眼睛不同，所以可以隨機換、也可以在同一格換表情。
- 表情片段（kind=expr）：每個狀態 × 每個暫時表情（後端 emotion），同樣長度、同樣姿勢。
- 過渡片段（kind=transition）：從 A 的每個切換點切到 B，用骨架 runtime 的交叉淡入＋眼睛平滑實際算出來，
  播完接到 B 的對應格，動作不跳。
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import time
from dataclasses import replace
from typing import Callable, Dict, List, Optional

from PIL import Image

from .eyes import EXPRESSIONS, EyeController, EyeState
from .pack import FORMAT, FRAMES, MANIFEST
from .renderer import Renderer
from .skeleton import Skeleton

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_FILES = ["pet/skeleton.py", "pet/renderer.py", "pet/eyes.py", "pet/baker.py"]


def encode(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, progressive=False, optimize=False, subsampling=2)
    return buf.getvalue()


class FrameStore:
    """把 JPEG 串進 frames.bin；完全相同的幀只存一次。"""

    def __init__(self, path: str):
        self.path = path
        self.fh = open(path, "wb")
        self.offset = 0
        self.index: Dict[str, tuple] = {}
        self.unique = 0
        self.refs = 0

    def add(self, data: bytes) -> tuple:
        self.refs += 1
        h = hashlib.sha1(data).hexdigest()
        hit = self.index.get(h)
        if hit:
            return hit
        self.fh.write(data)
        ref = (self.offset, len(data))
        self.offset += len(data)
        self.index[h] = ref
        self.unique += 1
        return ref

    def close(self) -> None:
        self.fh.close()


def eyes_key(st: EyeState, r: float = 1.0) -> tuple:
    """眼睛畫出來會不會一樣（跟 FaceRenderer 的量化一致）。"""
    return (st.shape, st.mouth, int(round(st.open * 20)), int(round(st.glow * 10)), round(st.size, 3),
            round(st.look_x * 16), round(st.look_y * 16 * 0.6))


def scripted_eyes(expr: str, n: int, dt: float, blinks=(), looks=()) -> List[EyeState]:
    """依腳本算每格的眼睛（不隨機）：blinks = [秒]、looks = [[秒, x, y], ...]。"""
    ec = EyeController(seed=0)
    ec.set_expression(expr)
    ec.blink_enabled = ec.wander_enabled = False
    ec.current = replace(ec.target)
    events = sorted([(t, "blink", None) for t in blinks] + [(l[0], "look", (l[1], l[2])) for l in looks],
                    key=lambda e: e[0])
    out, t = [], 0.0
    for i in range(n):
        out.append(ec.update(0.0 if i == 0 else dt))
        t = (i + 1) * dt
        while events and events[0][0] <= t:
            _, kind, arg = events.pop(0)
            if kind == "blink":
                ec.blink()
            else:
                ec._look_target = arg
    return out


def busy_ranges(flags: List[bool]) -> List[List[int]]:
    out, start = [], None
    for i, b in enumerate(flags + [False]):
        if b and start is None:
            start = i
        elif not b and start is not None:
            out.append([start, i])
            start = None
    return out


def _sha(paths: List[str]) -> str:
    h = hashlib.sha256()
    for p in sorted(paths):
        with open(p, "rb") as fh:
            h.update(os.path.relpath(p, ROOT).encode() + b"\0" + fh.read())
    return h.hexdigest()[:16]


def bake(out_dir: str, bake_cfg: Optional[str] = None, states_cfg: Optional[str] = None,
         only_states: Optional[List[str]] = None, fps: Optional[float] = None, quality: Optional[int] = None,
         transitions: Optional[bool] = None, expressions: Optional[List[str]] = None, preview: bool = True,
         log: Callable[[str], None] = print) -> dict:
    t_start = time.time()
    bake_cfg = bake_cfg or os.path.join(ROOT, "config", "bake.json")
    states_cfg = states_cfg or os.path.join(ROOT, "config", "states.json")
    with open(bake_cfg, encoding="utf-8") as fh:
        bc = json.load(fh)
    with open(states_cfg, encoding="utf-8") as fh:
        sc = json.load(fh)
    fps = float(fps or bc.get("fps", 20))
    quality = int(quality or bc.get("quality", 80))
    do_trans = bc.get("transitions", True) if transitions is None else transitions
    exprs = bc.get("expressions", list(EXPRESSIONS)) if expressions is None else expressions
    min_tf = int(bc.get("transition_min_frames", 8))
    sk_path = os.path.join(ROOT, bc.get("skeleton", "assets/skeletons/seed.json"))
    sk = Skeleton.load(sk_path)
    look = bc.get("look", sk.name)
    renderer = Renderer(sk)
    dt = 1.0 / fps

    os.makedirs(out_dir, exist_ok=True)
    tmp_bin = os.path.join(out_dir, FRAMES + ".tmp")
    store = FrameStore(tmp_bin)

    # 每個狀態的資訊
    plan: Dict[str, dict] = {}
    for name, info in sc["states"].items():
        if only_states and name not in only_states:
            continue
        anim = info.get("animation", name)
        if anim not in sk.animations:
            log(f"  ! 狀態 {name} 的動畫 {anim} 不在骨架 {sk.name}，略過（播放時會用即時繪圖）")
            continue
        a = sk.animations[anim]
        n = max(1, int(round(a.duration * fps)))
        effects = list(info.get("effects", []))
        cfg = bc.get("states", {}).get(name, {})
        eyes = info.get("eyes", "normal")
        variants = cfg.get("variants")
        if not variants:
            variants = [{"name": "base"}]
            if EXPRESSIONS[eyes].shape == "round" and a.duration >= 1.5:
                variants.append({"name": "blink", "blinks": [round(a.duration * 0.45, 2)]})
        plan[name] = {"anim": anim, "n": n, "dur": a.duration, "loop": a.loop, "effects": effects, "eyes": eyes,
                      "variants": variants, "cut_every": int(cfg.get("cut_every", bc.get("cut_every", 4)))}

    def zzz_period(dur: float) -> float:
        return dur / max(1, round(dur / 2.4))

    def set_static(st: dict, i: int) -> None:
        p = renderer.player
        p.current, p.prev, p.fade_t = st["anim"], None, 1.0
        p.time = i * st["dur"] / st["n"]
        renderer.effects = set(st["effects"])
        renderer.zzz_period = zzz_period(st["dur"])
        renderer.effect_time = p.time

    def put(img: Image.Image) -> list:
        off, ln = store.add(encode(img, quality))
        ax, ay = renderer.last_bubble_anchor
        return [off, ln, round(ax, 1), round(ay, 1)]

    clips: Dict[str, dict] = {}
    states_out: Dict[str, dict] = {}
    first_frames: Dict[str, Image.Image] = {}
    gif_frames: Dict[str, List[Image.Image]] = {}

    for name, st in plan.items():
        n = st["n"]
        base_eyes = scripted_eyes(st["eyes"], n, dt)
        base_keys = [eyes_key(e) for e in base_eyes]
        grid = list(range(0, n, st["cut_every"]))
        ids = []
        for v in st["variants"]:
            cid = f"{name}/{v['name']}"
            ev = scripted_eyes(st["eyes"], n, dt, v.get("blinks", ()), v.get("looks", ()))
            flags = [eyes_key(e) != k for e, k in zip(ev, base_keys)]
            if flags and (flags[0] or flags[-1]):
                log(f"  ! {cid} 頭尾的眼睛不是基本表情（循環會跳），請把眨眼/看的時間往中間移")
            frames = []
            for i in range(n):
                set_static(st, i)
                img = renderer.render(ev[i])
                frames.append(put(img))
                if v is st["variants"][0]:
                    if i == 0:
                        first_frames[name] = img
                    if preview and i % 2 == 0:
                        gif_frames.setdefault(name, []).append(img.resize((400, 240)))
            clips[cid] = {"state": name, "kind": "loop", "loop": st["loop"], "weight": float(v.get("weight", 1)),
                          "frames": frames, "cuts": [i for i in grid if not flags[i]], "busy": busy_ranges(flags)}
            ids.append(cid)
        expr_ids = {}
        for ex in exprs:
            if ex == st["eyes"] or ex not in EXPRESSIONS:
                continue
            cid = f"{name}/expr_{ex}"
            es = scripted_eyes(ex, 1, dt)[0]
            frames = []
            for i in range(n):
                set_static(st, i)
                frames.append(put(renderer.render(es)))
            clips[cid] = {"state": name, "kind": "expr", "expression": ex, "loop": st["loop"], "weight": 0,
                          "frames": frames, "cuts": grid, "busy": [[0, n]]}
            expr_ids[ex] = cid
        states_out[name] = {"animation": st["anim"], "eyes": st["eyes"], "frames": n, "duration": st["dur"],
                            "loop": st["loop"], "cut_every": st["cut_every"], "clips": ids, "expressions": expr_ids,
                            "effects": st["effects"], "label": sc["states"][name].get("label", name)}
        log(f"  {name:6s} {n:4d} 格 × {len(ids)} 變化版 + {len(expr_ids)} 表情片段")

    trans_out: Dict[str, Dict[str, dict]] = {}
    if do_trans:
        n_tr = 0
        for a_name, A in plan.items():
            for b_name, B in plan.items():
                if a_name == b_name:
                    continue
                anims = sk.animations
                fade = max(c for c in (renderer.player.fade, anims[B["anim"]].fade_in, anims[A["anim"]].fade_out)
                           if c is not None)
                nf = max(min_tf, int(math.ceil(fade * fps)))
                entry = {}
                for k in clips[f"{a_name}/{A['variants'][0]['name']}"]["cuts"]:
                    set_static(A, k)
                    p = renderer.player
                    ec = EyeController(seed=0)
                    ec.set_expression(A["eyes"])
                    ec.current = replace(ec.target)
                    ec.set_expression(B["eyes"])
                    ec.blink_enabled = ec.wander_enabled = False
                    p.play(B["anim"])
                    renderer.effects = set(B["effects"])
                    renderer.zzz_period = zzz_period(B["dur"])
                    frames = []
                    for j in range(1, nf + 1):
                        p.update(dt)
                        renderer.effect_time = p.time
                        frames.append(put(renderer.render(ec.update(dt))))
                    cid = f"trans/{a_name}>{b_name}@{k}"
                    enter = (nf + 1) % B["n"] if B["loop"] else min(nf + 1, B["n"] - 1)
                    clips[cid] = {"state": b_name, "kind": "transition", "loop": False, "weight": 0,
                                  "frames": frames, "cuts": [], "busy": [], "from": a_name, "cut": k}
                    entry[str(k)] = {"clip": cid, "enter": enter}
                    n_tr += 1
                trans_out[f"{a_name}>{b_name}"] = entry
        log(f"  過渡片段 {n_tr} 段")

    store.close()
    os.replace(tmp_bin, os.path.join(out_dir, FRAMES))
    srcs = [sk_path, states_cfg, bake_cfg] + [os.path.join(ROOT, f) for f in SOURCE_FILES]
    for b in sk.bones.values():
        if b.part:
            srcs.append(b.part.image_path)
    manifest = {
        "format": FORMAT, "look": look, "skeleton": sk.name, "fps": fps, "size": list(renderer.size),
        "quality": quality, "states": states_out, "transitions": trans_out, "clips": clips,
        "stats": {"clips": len(clips), "frame_refs": store.refs, "unique_frames": store.unique,
                  "bytes": store.offset, "bake_seconds": round(time.time() - t_start, 1)},
        "source": {"tool": "tools/bake.py", "skeleton": os.path.relpath(sk_path, ROOT),
                   "states": os.path.relpath(states_cfg, ROOT) if states_cfg.startswith(ROOT) else states_cfg,
                   "hash": _sha(srcs), "baked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "pillow": Image.__version__ if hasattr(Image, "__version__") else None},
    }
    tmp = os.path.join(out_dir, MANIFEST + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, os.path.join(out_dir, MANIFEST))

    if preview and gif_frames:
        pdir = os.path.join(out_dir, "preview")
        os.makedirs(pdir, exist_ok=True)
        for name, fr in gif_frames.items():
            fr[0].save(os.path.join(pdir, f"{name}.gif"), save_all=True, append_images=fr[1:],
                       duration=int(2000 / fps), loop=0)
        names = list(first_frames)
        sheet = Image.new("RGB", (400 * min(2, len(names)), 240 * math.ceil(len(names) / 2)))
        for i, name in enumerate(names):
            sheet.paste(first_frames[name].resize((400, 240)), ((i % 2) * 400, (i // 2) * 240))
        sheet.save(os.path.join(pdir, "states.png"))
    log(f"完成：{out_dir}  {len(clips)} 段片段、{store.unique} 格（引用 {store.refs}）、"
        f"{store.offset / 1e6:.1f} MB、{manifest['stats']['bake_seconds']} 秒")
    return manifest
