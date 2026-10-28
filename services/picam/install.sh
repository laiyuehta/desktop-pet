#!/bin/sh
# 安裝／更新 picam（在 Pi 上，從 repo 的 services/picam 執行）：sh install.sh
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null
python3 -c "import picamera2" 2>/dev/null || { echo "需要先 sudo apt install --no-install-recommends python3-picamera2"; exit 1; }
install -d -m 755 /home/pi-user/picam
install -m 755 picamd.py /home/pi-user/picam/picamd.py
install -m 644 picam_client.py README.md /home/pi-user/picam/
sudo install -m 644 picam.service /etc/systemd/system/picam.service
sudo systemctl daemon-reload
sudo systemctl enable picam.service
sudo systemctl restart picam.service
sleep 4
systemctl is-active picam && cat /run/picam/status.json && echo
