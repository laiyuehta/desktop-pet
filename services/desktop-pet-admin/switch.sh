#!/bin/sh
# 一次完成前端後台切換（在 Pi 上執行）：sh services/desktop-pet-admin/switch.sh
# 1. 將 desktop-pet 改為 --web off --wifi off（sockets 模式）
# 2. 將 desktop-pet-admin 開在 :8080 並啟動 Wi-Fi 管理
# 回退方式：sh services/desktop-pet-admin/rollback.sh
set -eu
cd "$(dirname "$0")"

PET_REPO_ROOT="$(cd ../.. && pwd)"
. "$PET_REPO_ROOT/tools/deploy/common.sh"
load_deploy_env
require_sys_env

[ -n "$PET_BACKEND_URL" ] || { echo "環境檔（${PET_SYS_ENV:-/etc/desktop-pet/env} 或 deploy.env）沒設 PET_BACKEND_URL（見 deploy.env.example）" >&2; exit 1; }

PET_UNIT=/etc/systemd/system/desktop-pet.service
ADMIN_UNIT=/etc/systemd/system/desktop-pet-admin.service

echo "== 準備切換至前端後台架構 (v2.6 第 1 步) =="

tmp_pet=$(mktemp)
tmp_admin=$(mktemp)

render_template "$PET_REPO_ROOT/services/desktop-pet/desktop-pet-split.service" > "$tmp_pet" || { rm -f "$tmp_pet" "$tmp_admin"; exit 1; }
render_template "$PET_REPO_ROOT/services/desktop-pet-admin/desktop-pet-admin.service" > "$tmp_admin" || { rm -f "$tmp_pet" "$tmp_admin"; exit 1; }

sudo install -m 644 "$tmp_pet" "$PET_UNIT"
sudo install -m 644 "$tmp_admin" "$ADMIN_UNIT"
rm -f "$tmp_pet" "$tmp_admin"

echo "== 驗證 systemd unit"
sudo systemd-analyze verify "$PET_UNIT"
sudo systemd-analyze verify "$ADMIN_UNIT"

echo "== 套用並重啟服務"
if systemctl is-active --quiet desktop-pet; then
    sudo systemctl stop desktop-pet
fi
if systemctl is-active --quiet desktop-pet-admin; then
    sudo systemctl stop desktop-pet-admin
fi

sudo systemctl daemon-reload
sudo systemctl enable desktop-pet desktop-pet-admin
sudo systemctl start desktop-pet desktop-pet-admin

sleep 3
echo "== 服務狀態檢查"
systemctl is-active desktop-pet
systemctl is-active desktop-pet-admin

echo "切換完成！前端後台已在 :8080 運作，桌寵主程式已改由 /run/desktop-pet/* 提供服務。"
