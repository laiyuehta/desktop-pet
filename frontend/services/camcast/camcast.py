#!/usr/bin/env python3
"""camcast（picam 的預覽訂閱 client）：picam 預覽 800x480 → /run/piscreen/piscreen.sock。
不碰相機（相機由 picam 獨占）、不碰 USB（螢幕由 piscreen 獨占）。picam 重啟時自動重連。
python3 camcast.py [--fps 15] [--seconds N] [--dry-run]   （--dry-run：只收預覽、不送螢幕，測試用）"""
import argparse
import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.expanduser("~/picam"))
sys.path.insert(0, os.path.expanduser("~/piscreen"))
from picam_client import Subscriber  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=int, default=15)
    ap.add_argument("--quality", type=int, default=80, help="（保留相容；畫質由 picam 決定）")
    ap.add_argument("--seconds", type=float, default=0)
    ap.add_argument("--dry-run", action="store_true", help="不送到螢幕，只統計收到的預覽")
    ap.add_argument("--socket", default="/run/picam/picam.sock")
    a = ap.parse_args()
    out = None
    if not a.dry_run:
        from piscreen_client import Client
        out = Client()

    def state(s, detail):
        print("%s camcast picam %s: %s" % (time.strftime("%T"), s, detail), flush=True)
    sub = Subscriber(fps=a.fps, width=800, height=480, client="camcast", path=a.socket, on_state=state)
    for sg in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sg, lambda *_: sub.stop())
    if a.seconds:
        threading.Timer(a.seconds, sub.stop).start()
    n = wn = nbytes = 0
    t0 = tl = time.time()
    gaps = []
    last = None
    try:
        for jpg in sub.frames():
            now = time.time()
            if last:
                gaps.append(now - last)
            last = now
            if out is not None:
                out.send(jpg)
            n += 1
            wn += 1
            nbytes += len(jpg)
            if now - tl >= 10:
                print("%s camcast fps=%.1f total=%d avg=%dB" % (time.strftime("%T"), wn / (now - tl), n, nbytes // max(n, 1)),
                      flush=True)
                tl, wn = now, 0
    finally:
        if out is not None:
            out.close()
        el = time.time() - t0
        print("%s camcast done frames=%d in %.1fs (%.1f fps) max_gap=%.2fs reconnects=%d" %
              (time.strftime("%T"), n, el, n / max(el, 1e-6), max(gaps or [0]), sub.reconnects), flush=True)


if __name__ == "__main__":
    main()
