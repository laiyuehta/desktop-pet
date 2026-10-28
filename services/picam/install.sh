#!/bin/sh
# 安裝／更新 picam（在 Pi 上，從 repo 的 services/picam 執行）：sh install.sh
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null
python3 -c "import picamera2" 2>/dev/null || { echo "需要先 sudo apt install --no-install-recommends python3-picamera2"; exit 1; }
# 部署值從 repo 根目錄的 deploy.env 讀（不進 repo；只讀不寫）
PET_REPO_ROOT="$(cd ../.. && pwd)"
. "$PET_REPO_ROOT/tools/deploy/common.sh"
load_deploy_env
install -d -m 755 "$PET_HOME/picam"
install -m 755 picamd.py "$PET_HOME/picam/picamd.py"
install -m 644 picam_client.py README.md "$PET_HOME/picam/"
tmp=$(mktemp)
render_template picam.service > "$tmp" || { rm -f "$tmp"; exit 1; }
sudo install -m 644 "$tmp" /etc/systemd/system/picam.service
rm -f "$tmp"
sudo systemctl daemon-reload
sudo systemctl enable picam.service
sudo systemctl restart picam.service
sleep 4
systemctl is-active picam && cat /run/picam/status.json && echo
