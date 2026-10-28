#!/bin/sh
# 相機預覽 → 螢幕（transient unit，不開機啟動）。需要 picam.service 與 piscreen.service 在跑。
sudo systemctl is-active --quiet camcast && { echo already running; exit 0; }
sudo systemctl is-active --quiet picam || { echo "picam 沒在跑（sudo systemctl start picam）"; exit 1; }
sudo systemctl reset-failed camcast 2>/dev/null
sudo systemd-run --unit=camcast --description="camcast (picam subscriber -> piscreen client)" -p User=pi-user /usr/bin/python3 /home/pi-user/camcast/camcast.py --fps 15
