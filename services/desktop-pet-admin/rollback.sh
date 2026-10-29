#!/bin/sh
# 回退前端後台切換（在 Pi 上執行）：sh services/desktop-pet-admin/rollback.sh
# 停用 desktop-pet-admin，並將 desktop-pet 改回單體模式（--web 0.0.0.0:8080 --wifi auto）
set -eu
cd "$(dirname "$0")"

PET_REPO_ROOT="$(cd ../.. && pwd)"
. "$PET_REPO_ROOT/tools/deploy/common.sh"
load_deploy_env

[ -n "$PET_BACKEND_URL" ] || { echo "deploy.env 沒設 PET_BACKEND_URL（cp deploy.env.example deploy.env 再改）"; exit 1; }

PET_UNIT=/etc/systemd/system/desktop-pet.service
ADMIN_UNIT=/etc/systemd/system/desktop-pet-admin.service

echo "== 正在回退至單體模式 =="

if systemctl is-active --quiet desktop-pet-admin; then
    sudo systemctl stop desktop-pet-admin
fi
sudo systemctl disable desktop-pet-admin 2>/dev/null || true

tmp_pet=$(mktemp)
render_template "$PET_REPO_ROOT/services/desktop-pet/desktop-pet.service" > "$tmp_pet" || { rm -f "$tmp_pet"; exit 1; }
sudo install -m 644 "$tmp_pet" "$PET_UNIT"
rm -f "$tmp_pet"

echo "== 驗證 systemd unit"
sudo systemd-analyze verify "$PET_UNIT"

if systemctl is-active --quiet desktop-pet; then
    sudo systemctl stop desktop-pet
fi

sudo systemctl daemon-reload
sudo systemctl enable desktop-pet
sudo systemctl start desktop-pet

sleep 3
echo "== 服務狀態檢查"
systemctl is-active desktop-pet

echo "回退完成！桌寵主程式已恢復監聽 :8080 及內建 Wi-Fi 管理。"
