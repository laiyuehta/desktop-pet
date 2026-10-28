#!/bin/sh
# 安裝／更新前端後台的開機自動啟動服務（在 Pi 上執行）：sh services/desktop-pet-admin/install.sh
# 停用：sudo systemctl disable --now desktop-pet-admin
set -eu
cd "$(dirname "$0")"
UNIT=/etc/systemd/system/desktop-pet-admin.service
PET_REPO_ROOT="$(cd ../.. && pwd)"
. "$PET_REPO_ROOT/tools/deploy/common.sh"
load_deploy_env
tmp=$(mktemp)
render_template desktop-pet-admin.service > "$tmp" || { rm -f "$tmp"; exit 1; }
sudo install -m 644 "$tmp" "$UNIT"
rm -f "$tmp"
echo "== systemd-analyze verify"
sudo systemd-analyze verify "$UNIT" && echo "verify OK"
if systemctl is-active --quiet desktop-pet-admin; then
  echo "停止目前的 desktop-pet-admin"
  sudo systemctl stop desktop-pet-admin
fi
sudo systemctl reset-failed desktop-pet-admin 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable desktop-pet-admin
sudo systemctl start desktop-pet-admin
sleep 2
systemctl is-enabled desktop-pet-admin
systemctl is-active desktop-pet-admin

