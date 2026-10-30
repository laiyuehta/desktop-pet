#!/usr/bin/env python3
"""產生動畫包：把骨架動畫預先算成 JPEG 片段，給 Pi 播放器用。

用法（在電腦或伺服器上跑，Pi 也能跑但比較慢）：
    python3 tools/bake.py                      # 依 config/bake.json 產生 ~/.local/share/desktop-pet/packs/seed/
    python3 tools/bake.py --out /tmp/test-pack --no-transitions --states idle,happy
    python3 tools/bake.py --check ~/.local/share/desktop-pet/packs/seed    # 檢查動畫包內容

設定：config/bake.json（外觀、骨架、fps、JPEG 品質、每個狀態的變化版、切換點間隔）
狀態：config/states.json（狀態 → 動畫、眼睛表情、特效）
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 動畫包放在 repo 外（#38）：換程式碼／重建 venv 都不會動到它
DEFAULT_PACKS_DIR = "~/.local/share/desktop-pet/packs"
sys.path.insert(0, ROOT)

from pet.baker import bake  # noqa: E402
from pet.pack import AnimationPack  # noqa: E402


def check(path: str) -> int:
    pk = AnimationPack(path)
    print(f"{pk.path}: 外觀 {pk.look}、骨架 {pk.skeleton}、{pk.fps:g} fps、{len(pk.clips)} 段片段、"
          f"{pk.frame_count()} 格引用、{pk.bytes_total / 1e6:.1f} MB")
    for name, st in pk.states.items():
        print(f"  {name:6s} {st['frames']:4d} 格  變化版 {', '.join(c.split('/')[1] for c in st['clips'])}"
              f"  表情 {len(st.get('expressions', {}))}  切換點 {len(pk.clips[st['clips'][0]]['cuts'])}")
    bad = 0
    for cid, c in pk.clips.items():
        for i in range(len(c["frames"])):
            d = pk.frame(cid, i)
            if d[:2] != b"\xff\xd8" or d[-2:] != b"\xff\xd9":
                bad += 1
    print("全部幀都是完整 JPEG" if not bad else f"!! {bad} 格壞掉")
    pk.close()
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="產生桌寵動畫包")
    ap.add_argument("--out", default=None, help="輸出資料夾（預設 ~/.local/share/desktop-pet/packs/<look>，即桌寵 --pack auto 讀的位置）")
    ap.add_argument("--config", default=None, help="產生設定（預設 config/bake.json）")
    ap.add_argument("--states-config", default=None, help="狀態表（預設 config/states.json）")
    ap.add_argument("--states", default=None, help="只產生這些狀態（逗號分隔）")
    ap.add_argument("--fps", type=float, default=None)
    ap.add_argument("--quality", type=int, default=None)
    ap.add_argument("--no-transitions", action="store_true")
    ap.add_argument("--no-expressions", action="store_true")
    ap.add_argument("--no-preview", action="store_true")
    ap.add_argument("--check", metavar="PACK", help="只檢查既有動畫包")
    a = ap.parse_args()
    if a.check:
        return check(a.check)
    import json
    cfg = a.config or os.path.join(ROOT, "config", "bake.json")
    with open(cfg, encoding="utf-8") as fh:
        look = json.load(fh).get("look", "seed")
    default_dir = os.path.join(os.environ["XDG_DATA_HOME"], "desktop-pet", "packs") if os.environ.get("XDG_DATA_HOME") else os.path.expanduser(DEFAULT_PACKS_DIR)
    out = os.path.expanduser(a.out) if a.out else os.path.join(default_dir, look)
    print(f"產生動畫包 {look} → {out}")
    bake(out, bake_cfg=cfg, states_cfg=a.states_config,
         only_states=a.states.split(",") if a.states else None, fps=a.fps, quality=a.quality,
         transitions=False if a.no_transitions else None, expressions=[] if a.no_expressions else None,
         preview=not a.no_preview)
    return check(out)


if __name__ == "__main__":
    sys.exit(main())
