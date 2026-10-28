#!/usr/bin/env python3
"""離線算出示範動畫（模擬時鐘，結果可重現），不需要螢幕。

流程：idle → 摸頭 → happy → 回 idle → 閒置逾時 → bored（左右滾動一整圈）→ sleep → 摸頭 → 醒來 idle
輸出：
  preview/demo.gif（400×240，10 fps）
  preview/idle.png、preview/happy.png、preview/bored.png（滾動中）、preview/sleep.png（800×480 原尺寸）
用法：python3 tools/render_demo.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import ImageDraw, ImageFont  # noqa: E402

from pet.app import PetApp  # noqa: E402
from pet.events import make_event  # noqa: E402
from pet.outputs import GifSink  # noqa: E402

FPS = 20
IDLE_TIMEOUT = 3.0   # 示範用：閒置 3 秒就逾時（正式設定是 600 秒）
BORED_TIMEOUT = 10.5  # 示範用：無聊滾完一整圈（10 秒）再睡

# (時間, 事件)
SCRIPT = [
    (2.0, make_event("touch", "pat", source="demo")),
    (22.0, make_event("touch", "pat", source="demo")),
]
STILLS = {"idle.png": 1.0, "happy.png": 2.6, "bored.png": 9.4, "sleep.png": 21.0}
END = 25.0


def font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:          # Pillow < 10.1
        return ImageFont.load_default()


def main():
    out_dir = os.path.join(ROOT, "preview")
    os.makedirs(out_dir, exist_ok=True)
    app = PetApp(timer_overrides={"idle_timeout": IDLE_TIMEOUT, "bored.idle_timeout": BORED_TIMEOUT}, use_clock=False, eye_seed=7)
    app.start()
    # 每格先縮成 400×240 再加字幕，所以 GifSink 不再縮放；每 2 格留 1 格 → 10 fps
    gif = GifSink(os.path.join(out_dir, "demo.gif"), fps=FPS, keep_every=2, scale=None)
    f = font(13)
    dt = 1.0 / FPS
    script = list(SCRIPT)
    stills = dict(STILLS)
    last_ev = ""
    n = int(END * FPS)
    for i in range(n):
        t = i * dt
        while script and script[0][0] <= t:
            ev = script.pop(0)[1]
            app.post(ev)
            last_ev = f"event: {ev['name']} @ {t:.1f}s"
        prev = app.fsm.state
        app.step(dt)
        if app.fsm.state != prev:
            cause = (app.fsm.last_event or {}).get("name", "?")
            print(f"{t:5.2f}s  {prev} → {app.fsm.state}（{cause}）")
            last_ev = f"{prev} -> {app.fsm.state}  ({cause} @ {t:.1f}s)"
        frame = app.render()
        for name, ts in list(stills.items()):
            if t >= ts:
                frame.save(os.path.join(out_dir, name))
                del stills[name]
        small = frame.resize((400, 240))
        d = ImageDraw.Draw(small)
        d.text((8, 6), f"{t:4.1f}s  state: {app.fsm.state}", fill=(220, 230, 255), font=f)
        if last_ev:
            d.text((8, 222), last_ev, fill=(255, 220, 110), font=f)
        gif.send(small)
    gif.close()
    app.stop()
    size = os.path.getsize(os.path.join(out_dir, "demo.gif"))
    print(f"demo.gif {size / 1024:.0f} KB, {len(gif.frames)} 格")


if __name__ == "__main__":
    main()
