#!/bin/sh
# 回退前端後台切換（在 Pi 上執行）：sh frontend/services/desktop-pet-admin/rollback.sh
# 停用 desktop-pet-admin，並將 desktop-pet 改回單體模式（--web 0.0.0.0:8080 --wifi auto）
set -eu
cd "$(dirname "$0")"

PET_REPO_ROOT="$(cd ../../.. && pwd)"
. "$PET_REPO_ROOT/frontend/tools/deploy/common.sh"
load_deploy_env
require_sys_env --warn || true

if [ -z "$PET_BACKEND_URL" ]; then
    echo "警告：環境檔未設定 PET_BACKEND_URL，回退後將以離線單機模式執行。" >&2
fi

PET_UNIT=/etc/systemd/system/desktop-pet.service
ADMIN_UNIT=/etc/systemd/system/desktop-pet-admin.service

echo "== 正在回退至單體模式 =="

# 先準備好、驗證完再動服務：venv 或 unit 有問題就中止，desktop-pet-admin 與目前的 desktop-pet 都維持原狀
ensure_frontend_venv || { echo "錯誤：frontend/.venv 無法就緒，未做任何變更（desktop-pet-admin 仍在執行）" >&2; exit 1; }
tmp_pet=$(mktemp --suffix=.service)
render_template "$PET_REPO_ROOT/frontend/services/desktop-pet/desktop-pet.service" > "$tmp_pet" || { rm -f "$tmp_pet"; echo "錯誤：unit 範本產生失敗，未做任何變更" >&2; exit 1; }
echo "== 驗證 systemd unit"
sudo systemd-analyze verify "$tmp_pet" || { rm -f "$tmp_pet"; echo "錯誤：unit 驗證失敗，未做任何變更" >&2; exit 1; }

if systemctl is-active --quiet desktop-pet-admin; then
    sudo systemctl stop desktop-pet-admin
fi
sudo systemctl disable desktop-pet-admin 2>/dev/null || true

sudo install -m 644 "$tmp_pet" "$PET_UNIT"
rm -f "$tmp_pet"

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
