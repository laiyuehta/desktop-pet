#!/bin/sh
# 相機預覽 → 螢幕（transient unit，不開機啟動）。需要 picam.service 與 piscreen.service 在跑。
# 部署值：repo 裡跑就讀 repo 根目錄 deploy.env；裝在 ~/camcast 時用目前使用者
_root="$(cd "$(dirname "$0")/../../.." 2>/dev/null && pwd)"
if [ -f "$_root/frontend/tools/deploy/common.sh" ]; then PET_REPO_ROOT="$_root"; . "$_root/frontend/tools/deploy/common.sh"; load_deploy_env; fi
PET_USER="${PET_USER:-$(id -un)}"; PET_HOME="${PET_HOME:-$HOME}"
# 硬體開關：環境檔 PET_ENABLE_CAMCAST=1 才啟動（裝在 ~/camcast、讀不到環境檔時照舊不擋）
if command -v pet_flag_on >/dev/null 2>&1 && ! pet_flag_on "${PET_ENABLE_CAMCAST:-0}"; then
  echo "PET_ENABLE_CAMCAST 沒開（環境檔設 PET_ENABLE_CAMCAST=1），不啟動 camcast"; exit 0
fi
sudo systemctl is-active --quiet camcast && { echo already running; exit 0; }
sudo systemctl is-active --quiet picam || { echo "picam 沒在跑（sudo systemctl start picam）"; exit 1; }
sudo systemctl reset-failed camcast 2>/dev/null
sudo systemd-run --unit=camcast --description="camcast (picam subscriber -> piscreen client)" -p User="$PET_USER" /usr/bin/python3 "$PET_HOME/camcast/camcast.py" --fps 15
