#!/usr/bin/env python3
"""量測效能。

用法：
    python3 tools/bench.py [秒數]                 # 即時骨架繪圖：純繪圖 fps、繪圖 + JPEG fps
    python3 tools/bench.py --stages [秒數]        # 即時繪圖分段毫秒：step / 姿勢 / 繪圖 / 編碼
    python3 tools/bench.py --pack packs/seed [秒數]   # 動畫包播放：每格取幀成本、對話框合成成本
在 Pi 3B 上跑一次就知道實際能到多少 fps。
"""

import argparse
import io
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pet.app import PetApp  # noqa: E402
from pet.events import make_event  # noqa: E402
from pet.outputs.piscreen import encode_jpeg, pack_frame  # noqa: E402

STATES = ("idle", "happy", "bored", "sleep")


def run(seconds: float, jpeg: bool, state: str):
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


def stages(seconds: float):
    for state in STATES:
        app = PetApp(use_clock=False, eye_seed=1)
        app.post(make_event("control", "set_state", state=state))
        for _ in range(30):
            app.step(0.05)
            app.render()
        t = {"step": 0.0, "render": 0.0, "encode": 0.0}
        n, end = 0, time.perf_counter() + seconds
        while time.perf_counter() < end:
            a = time.perf_counter()
            app.step(0.05)
            b = time.perf_counter()
            fr = app.render()
            c = time.perf_counter()
            encode_jpeg(fr)
            d = time.perf_counter()
            t["step"] += b - a
            t["render"] += c - b
            t["encode"] += d - c
            n += 1
        print(f"{state:6s} " + " ".join(f"{k} {v / n * 1000:6.1f} ms" for k, v in t.items()) + f" | {n / seconds:5.1f} fps")


def pack_bench(path: str, seconds: float):
    from pet.outputs.base import OutputSink
    from pet.pack import AnimationPack, PackDriver

    class EncodeSink(OutputSink):       # 模擬送 piscreen：JPEG 直接打包、影像要重新編碼
        def send(self, frame):
            pack_frame(encode_jpeg(frame))

        def send_jpeg(self, data):
            pack_frame(data)

    pk = AnimationPack(path)
    print(f"動畫包 {pk.path}：{len(pk.clips)} 段、{pk.frame_count()} 格、{pk.bytes_total / 1e6:.1f} MB")
    for state in STATES + ("bubble",):
        app = PetApp(use_clock=False, eye_seed=1)
        drv = PackDriver(pk, app, EncodeSink(), rng=random.Random(1))
        app.post(make_event("control", "set_state", state="bored" if state == "bubble" else state))
        if state == "bubble":
            app.renderer.say("你好～今天要一起玩嗎？", 9999)
        n, t0, c0 = 0, time.perf_counter(), time.process_time()
        while time.perf_counter() - t0 < seconds:
            app.step(0.05)
            drv.emit()
            n += 1
        wall = time.perf_counter() - t0
        print(f"{state:6s} 每格 {wall / n * 1000:6.2f} ms（CPU {(time.process_time() - c0) / n * 1000:6.2f} ms）"
              f" → 最高 {n / wall:7.1f} fps；20 fps 時約佔 1 核 {(time.process_time() - c0) / n * 20 * 100:5.1f}%"
              f"  畫面 {drv.mode}")
    pk.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("seconds", nargs="?", type=float, default=4.0)
    ap.add_argument("--stages", action="store_true")
    ap.add_argument("--pack", default=None)
    a = ap.parse_args()
    print(f"Python {sys.version.split()[0]}，每項量 {a.seconds:.0f} 秒")
    if a.pack:
        return pack_bench(a.pack, a.seconds)
    if a.stages:
        return stages(a.seconds)
    for state in STATES:
        f1, _ = run(a.seconds, False, state)
        f2, sz = run(a.seconds, True, state)
        print(f"{state:6s} 純繪圖 {f1:6.1f} fps | 繪圖+JPEG {f2:6.1f} fps（每格 {sz / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
