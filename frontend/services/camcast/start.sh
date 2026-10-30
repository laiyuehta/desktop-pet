#!/bin/sh
# 相機預覽 → 螢幕（transient unit，不開機啟動）。需要 picam.service 與 piscreen.service 在跑。
# 部署值：repo 裡跑就讀 repo 根目錄 deploy.env；裝在 ~/camcast 時用目前使用者
_root="$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)"
if [ -f "$_root/tools/deploy/common.sh" ]; then PET_REPO_ROOT="$_root"; . "$_root/tools/deploy/common.sh"; load_deploy_env; fi
PET_USER="${PET_USER:-$(id -un)}"; PET_HOME="${PET_HOME:-$HOME}"
sudo systemctl is-active --quiet camcast && { echo already running; exit 0; }
sudo systemctl is-active --quiet picam || { echo "picam 沒在跑（sudo systemctl start picam）"; exit 1; }
sudo systemctl reset-failed camcast 2>/dev/null
sudo systemd-run --unit=camcast --description="camcast (picam subscriber -> piscreen client)" -p User="$PET_USER" /usr/bin/python3 "$PET_HOME/camcast/camcast.py" --fps 15
