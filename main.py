#!/usr/bin/env python3
"""桌寵主程式。

電腦上預覽：
    python3 main.py --sink preview --input keyboard,web
樹莓派（piscreen 服務已在跑）：
    python3 main.py --sink piscreen --input web --web 0.0.0.0:8080
接後端（大腦，跑在 R7）：
    python3 main.py --sink piscreen --input web --backend http://192.0.2.100:8090
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from pet.app import PetApp
from pet.inputs import KeyboardInput, WebRemoteInput
from pet.outputs import FileSink, NullSink, PiscreenSink
from pet.outputs.piscreen import DEFAULT_SOCKET


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="桌寵第一期原型")
    p.add_argument("--sink", choices=["piscreen", "preview", "null"], default="preview",
                   help="畫面輸出：piscreen（Pi 上的螢幕服務）、preview（寫 PNG）、null（只量 fps）")
    p.add_argument("--fps", type=float, default=20.0, help="目標 fps（預設 20）")
    p.add_argument("--input", default="keyboard",
                   help="輸入轉接器，逗號分隔：keyboard、web、none（預設 keyboard）")
    p.add_argument("--web", default="0.0.0.0:8080", help="網頁遙控監聽位址（--input 含 web 時，預設 0.0.0.0:8080）")
    p.add_argument("--socket", default=DEFAULT_SOCKET, help="piscreen Unix socket 路徑")
    p.add_argument("--jpeg-quality", type=int, default=85)
    p.add_argument("--preview-dir", default="preview/live", help="preview 輸出資料夾（latest.png）")
    p.add_argument("--preview-every", type=int, default=2, help="preview 每幾格寫一次檔")
    p.add_argument("--config", default=None, help="狀態機設定檔（預設 config/states.json）")
    p.add_argument("--idle-timeout", type=float, default=None,
                   help="覆寫閒置秒數（測試用，例如 10；預設用設定檔的 600）")
    p.add_argument("--duration", type=float, default=0, help="跑幾秒後自動結束（0 = 不限）")
    p.add_argument("--backend", default=None,
                   help="後端（大腦）網址，例如 http://192.0.2.100:8090；預設不接")
    p.add_argument("--pet-id", default="seed-001", help="寵物 ID（後端存檔用）")
    p.add_argument("--backend-token", default="", help="後端 api_token（有設才需要）")
    p.add_argument("--font", default=None, help="對話框中文字型路徑（預設自動尋找）")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def make_sink(args):
    if args.sink == "piscreen":
        return PiscreenSink(args.socket, quality=args.jpeg_quality)
    if args.sink == "preview":
        return FileSink(args.preview_dir, every=args.preview_every)
    return NullSink()


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("main")
    overrides = {"idle_timeout": args.idle_timeout} if args.idle_timeout else None
    app = PetApp(args.config, timer_overrides=overrides, backend_url=args.backend, pet_id=args.pet_id,
                 backend_token=args.backend_token, font_path=args.font)
    sink = make_sink(args)
    stop = {"flag": False}

    def request_stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    inputs = []
    for name in [s.strip() for s in args.input.split(",") if s.strip()]:
        if name == "keyboard":
            inputs.append(KeyboardInput(app.post, on_quit=request_stop))
        elif name == "web":
            host, _, port = args.web.rpartition(":")
            inputs.append(WebRemoteInput(app.post, host or "0.0.0.0", int(port), status_fn=app.status))
        elif name != "none":
            log.error("未知的輸入：%s", name)
            return 2

    app.start()
    sink.open()
    for i in inputs:
        i.start()

    frame_time = 1.0 / max(1.0, args.fps)
    t_start = last = time.monotonic()
    next_frame = last
    frames, fps_t0, fps_n = 0, last, 0
    try:
        while not stop["flag"]:
            now = time.monotonic()
            dt = min(0.25, now - last)       # 卡頓時不要一次跳太多
            last = now
            app.step(dt)
            sink.send(app.render())
            frames += 1
            fps_n += 1
            if now - fps_t0 >= 5.0:
                app.fps = fps_n / (now - fps_t0)
                log.info("fps %.1f（目標 %.0f）狀態 %s", app.fps, args.fps, app.fsm.state)
                fps_t0, fps_n = now, 0
            if args.duration and now - t_start >= args.duration:
                break
            next_frame += frame_time
            sleep = next_frame - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_frame = time.monotonic()  # 跑不動就不追趕
    finally:
        for i in inputs:
            i.stop()
        sink.close()
        app.stop()
        log.info("結束，共 %d 格，平均 %.1f fps", frames, frames / max(1e-6, time.monotonic() - t_start))
    return 0


if __name__ == "__main__":
    sys.exit(main())
