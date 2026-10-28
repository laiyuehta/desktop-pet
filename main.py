#!/usr/bin/env python3
"""桌寵主程式。

電腦上預覽：
    python3 main.py --sink preview --input keyboard,web
樹莓派（piscreen 服務已在跑；有 packs/seed 動畫包時自動播放預錄動畫）：
    python3 main.py --sink piscreen --input web --web 0.0.0.0:8080 --pack packs/seed
接後端（大腦，跑在 R7）：
    python3 main.py --sink piscreen --input web --backend http://192.0.2.100:8090
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import sys
import time

from pet.app import PetApp
from pet.events import make_event
from pet.extensions import CameraSnapshot, CameraWebAPI, PhotoUploader
from pet.extensions.camera import DEFAULT_PENDING_DIR, DEFAULT_PICAM_SOCKET, picam_snapshot
from pet.inputs import KeyboardInput, WebRemoteInput
from pet.pack import AnimationPack, PackDriver
from pet.outputs import FileSink, NullSink, PiscreenSink
from pet.outputs.mirror import ScreenMirror, ScreenWebAPI
from pet.outputs.piscreen import DEFAULT_SOCKET
from pet.wifi import DEFAULT_HOTSPOT_CONF, HOTSPOT_SSID, WifiManager, WifiWebAPI


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="桌寵第一期原型")
    p.add_argument("--sink", choices=["piscreen", "preview", "null"], default="preview",
                   help="畫面輸出：piscreen（Pi 上的螢幕服務）、preview（寫 PNG）、null（只量 fps）")
    p.add_argument("--fps", type=float, default=None,
                   help="目標 fps（預設：動畫包的 fps；即時繪圖 20）")
    p.add_argument("--pack", default="auto",
                   help="預錄動畫包資料夾；auto = packs/<骨架名稱> 存在就用；none = 只用即時繪圖")
    p.add_argument("--input", default="keyboard",
                   help="輸入轉接器，逗號分隔：keyboard、web、none（預設 keyboard）")
    p.add_argument("--web", default="0.0.0.0:8080", help="網頁遙控監聽位址（--input 含 web 時，預設 0.0.0.0:8080）")
    p.add_argument("--socket", default=DEFAULT_SOCKET, help="piscreen Unix socket 路徑")
    p.add_argument("--jpeg-quality", type=int, default=80, help="即時繪圖／對話框重新編碼的 JPEG 品質（預設 80）")
    p.add_argument("--preview-dir", default="preview/live", help="preview 輸出資料夾（latest.png）")
    p.add_argument("--preview-every", type=int, default=2, help="preview 每幾格寫一次檔")
    p.add_argument("--config", default=None, help="狀態機設定檔（預設 config/states.json）")
    p.add_argument("--idle-timeout", type=float, default=None,
                   help="覆寫閒置秒數（測試用，例如 10；預設用設定檔的 600）")
    p.add_argument("--duration", type=float, default=0, help="跑幾秒後自動結束（0 = 不限）")
    p.add_argument("--backend", default=None,
                   help="後端（大腦）網址，例如 http://192.0.2.100:8090；預設不接")
    p.add_argument("--pet-id", default="seed-001", help="寵物 ID（後端存檔用）")
    p.add_argument("--backend-token", default="", help="後端 api_token（不建議：會出現在 ps；請用 --backend-token-file）")
    p.add_argument("--backend-token-file", default=None,
                   help="從檔案讀後端 api_token（建議，例如 ~/.config/desktop-pet/backend-token，權限 600）")
    p.add_argument("--camera", choices=["auto", "on", "off"], default="auto",
                   help="拍照能力模組 camera.snapshot（picam 的 client；auto = 有 picam socket 或 rpicam-still 才開）")
    p.add_argument("--camera-socket", default=DEFAULT_PICAM_SOCKET, help="相機常駐服務 picam 的 socket")
    p.add_argument("--camera-pending-dir", default=None, help="上傳失敗的照片暫存資料夾（預設 ~/.local/share/desktop-pet/pending-photos）")
    p.add_argument("--mirror", choices=["on", "off"], default="on",
                   help="網頁遙控的螢幕畫面同步（/screen.mjpg、/screen.jpg）")
    p.add_argument("--mirror-viewers", type=int, default=3, help="螢幕畫面同時觀看人數上限（預設 3）")
    p.add_argument("--font", default=None, help="對話框中文字型路徑（預設自動尋找）")
    p.add_argument("--wifi", choices=["auto", "on", "off"], default="auto",
                   help="網頁 Wi‑Fi 設定與自動重連（auto = 有 nmcli 和 Wi‑Fi 介面才開）")
    p.add_argument("--wifi-iface", default="wlan0", help="要管理的 Wi‑Fi 介面（只動這個介面）")
    p.add_argument("--wifi-hotspot", choices=["on", "off"], default="on",
                   help="連不上已記錄的網路時開設定熱點（預設 on）")
    p.add_argument("--wifi-hotspot-ssid", default=HOTSPOT_SSID, help="設定熱點名稱")
    p.add_argument("--wifi-hotspot-conf", default=DEFAULT_HOTSPOT_CONF, help="熱點密碼設定檔（password=...）")
    p.add_argument("--wifi-hotspot-after", type=float, default=120, help="幾秒連不上就開熱點（預設 120）")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def make_sink(args, mirror=None):
    if args.sink == "piscreen":
        sink = PiscreenSink(args.socket, quality=args.jpeg_quality)
    elif args.sink == "preview":
        sink = FileSink(args.preview_dir, every=args.preview_every)
    else:
        sink = NullSink()
    sink.mirror = mirror
    return sink


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("main")
    overrides = {"idle_timeout": args.idle_timeout} if args.idle_timeout else None
    token = args.backend_token
    if args.backend_token_file:
        try:
            with open(os.path.expanduser(args.backend_token_file), "r", encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError as exc:
            log.error("讀不到後端 token 檔 %s：%s", args.backend_token_file, exc.strerror)
            return 2
    camera = None
    if args.camera == "on" or (args.camera == "auto" and (os.path.exists(args.camera_socket)
                                                          or shutil.which("rpicam-still"))):
        uploader = PhotoUploader(args.backend, args.pet_id, token) if args.backend else None
        camera = CameraSnapshot(uploader, pending_dir=args.camera_pending_dir or DEFAULT_PENDING_DIR,
                                capture=picam_snapshot(args.camera_socket))
        log.info("拍照能力模組：開（picam %s；%s）", args.camera_socket,
                 "上傳到後端" if uploader else "沒有後端，只存在本機")
    app = PetApp(args.config, timer_overrides=overrides, backend_url=args.backend, pet_id=args.pet_id,
                 backend_token=token, font_path=args.font, extensions=[camera] if camera else None)
    mirror = ScreenMirror(max_viewers=args.mirror_viewers) if args.mirror == "on" else None
    sink = make_sink(args, mirror)
    pack_dir = args.pack
    if pack_dir == "auto":
        pack_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "packs", app.renderer.skeleton.name)
    pack = AnimationPack.try_load(pack_dir) if pack_dir and pack_dir != "none" else None
    if args.pack not in ("auto", "none") and pack is None:
        log.warning("找不到動畫包 %s，改用即時繪圖", args.pack)
    driver = PackDriver(pack, app, sink, quality=args.jpeg_quality) if pack else None
    if args.fps is None:
        args.fps = pack.fps if pack else 20.0
    elif pack and abs(args.fps - pack.fps) > 0.01:
        log.warning("--fps %g 跟動畫包的 %g fps 不同，動畫速度會跟著變", args.fps, pack.fps)
    if not pack:
        log.info("畫面：即時骨架繪圖")
    stop = {"flag": False}

    def request_stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    wifi = None
    if args.wifi == "on" or (args.wifi == "auto" and WifiManager.available(args.wifi_iface)):
        def wifi_notice(text, seconds):
            app.post(make_event("action", "say", source="wifi", params={"text": text, "seconds": seconds}))
        wifi = WifiManager(iface=args.wifi_iface, notify=wifi_notice, hotspot=args.wifi_hotspot == "on",
                           hotspot_conf=args.wifi_hotspot_conf, hotspot_ssid=args.wifi_hotspot_ssid,
                           hotspot_after=args.wifi_hotspot_after)
        log.info("Wi‑Fi 設定：管理 %s（熱點 %s）", args.wifi_iface, args.wifi_hotspot_ssid if wifi.hotspot_enabled else "關")

    def status_with_wifi():
        st = app.status()
        if wifi:
            st["wifi"] = wifi.brief()
        return st

    inputs = []
    for name in [s.strip() for s in args.input.split(",") if s.strip()]:
        if name == "keyboard":
            inputs.append(KeyboardInput(app.post, on_quit=request_stop))
        elif name == "web":
            host, _, port = args.web.rpartition(":")
            inputs.append(WebRemoteInput(app.post, host or "0.0.0.0", int(port), status_fn=status_with_wifi,
                                          routes=([WifiWebAPI(wifi)] if wifi else [])
                                          + ([CameraWebAPI(camera)] if camera else [])
                                          + ([ScreenWebAPI(mirror, app.status)] if mirror else [])))
        elif name != "none":
            log.error("未知的輸入：%s", name)
            return 2

    app.start()
    sink.open()
    for i in inputs:
        i.start()
    if wifi:
        wifi.start()

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
            if driver:
                driver.emit()
            else:
                sink.frame_meta = {"mode": "live"}
                sink.send(app.render())
            frames += 1
            fps_n += 1
            if now - fps_t0 >= 5.0:
                app.fps = fps_n / (now - fps_t0)
                ri = app.render_info or {}
                log.info("fps %.1f（目標 %.0f）狀態 %s 畫面 %s %s", app.fps, args.fps, app.fsm.state,
                         ri.get("mode", "live"), ri.get("clip") or "")
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
        if wifi:
            wifi.stop()
        sink.close()
        app.stop()
        if pack:
            pack.close()
        log.info("結束，共 %d 格，平均 %.1f fps", frames, frames / max(1e-6, time.monotonic() - t_start))
    return 0


if __name__ == "__main__":
    sys.exit(main())
