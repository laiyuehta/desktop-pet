#!/usr/bin/env python3
"""量測繪圖效能：純繪圖 fps、繪圖 + JPEG 編碼 fps（= 送 piscreen 前的成本）。

用法：python3 tools/bench.py [秒數]
在 Pi 3B 上跑一次就知道實際能到多少 fps。
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pet.app import PetApp  # noqa: E402
from pet.events import make_event  # noqa: E402
from pet.outputs.piscreen import encode_jpeg, pack_frame  # noqa: E402


def run(seconds: float, jpeg: bool, state: str) -> float:
    app = PetApp(use_clock=False, eye_seed=1)
    app.post(make_event("control", "set_state", state=state))
    dt = 1 / 20
    for _ in range(40):      # 暖機（填快取）
        app.step(dt)
        app.render()
    n, t0 = 0, time.perf_counter()
    size = 0
    while time.perf_counter() - t0 < seconds:
        app.step(dt)
        fr = app.render()
        if jpeg:
            size = len(pack_frame(encode_jpeg(fr)))
        n += 1
    fps = n / (time.perf_counter() - t0)
    return fps, size


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
    print(f"Python {sys.version.split()[0]}，每項量 {secs:.0f} 秒")
    for state in ("idle", "happy", "sleep"):
        f1, _ = run(secs, False, state)
        f2, sz = run(secs, True, state)
        print(f"{state:6s} 純繪圖 {f1:6.1f} fps | 繪圖+JPEG {f2:6.1f} fps（每格 {sz / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
