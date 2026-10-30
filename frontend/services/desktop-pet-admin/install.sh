#!/bin/sh
# 安裝／更新前端後台的開機自動啟動服務（在 Pi 上執行）：sh frontend/services/desktop-pet-admin/install.sh
# 停用：sudo systemctl disable --now desktop-pet-admin
set -eu
cd "$(dirname "$0")"

# 先準備 venv（失敗就中止，不動任何服務）
UNIT=/etc/systemd/system/desktop-pet-admin.service
PET_REPO_ROOT="$(cd ../../.. && pwd)"
. "$PET_REPO_ROOT/frontend/tools/deploy/common.sh"
load_deploy_env
require_sys_env
ensure_frontend_venv

# 檢查桌寵主程式是否仍在執行內建網頁或 Wi-Fi（若有，提示改用 switch.sh）
if systemctl is-active --quiet desktop-pet 2>/dev/null; then
    CMD=$(ps -o args= -C python3 2>/dev/null | grep "desktop-pet/frontend/main.py" | grep -v grep || true)
    if [ -z "$CMD" ]; then
        CMD=$(systemctl show -p ExecStart --value desktop-pet 2>/dev/null || true)
    fi
    if printf '%s\n' "$CMD" | grep -qv -- '--web off'; then
        echo "錯誤：desktop-pet.service 目前仍在監聽網頁埠（未加上 --web off）。" >&2
        echo "為避免連接埠衝突並確保任何時候只有一個 Wi-Fi 管理，請改以切換腳本一次完成：" >&2
        echo "  sh frontend/services/desktop-pet-admin/switch.sh" >&2
        exit 1
    fi
    if printf '%s\n' "$CMD" | grep -qv -- '--wifi off'; then
        echo "錯誤：desktop-pet.service 目前未設定 --wifi off。" >&2
        echo "為避免 Wi-Fi 管理衝突，請先將 desktop-pet 改為 --wifi off，或執行 switch.sh：" >&2
        echo "  sh frontend/services/desktop-pet-admin/switch.sh" >&2
        exit 1
    fi
fi

# 如果目前已有後台在跑，先停止以便更新（避免 8080 檢查擋到自己）
if systemctl is-active --quiet desktop-pet-admin 2>/dev/null; then
    echo "停止目前的 desktop-pet-admin"
    sudo systemctl stop desktop-pet-admin
fi

# 檢查是否仍有其他非後台程序佔用 8080
if command -v ss >/dev/null 2>&1; then
    if ss -ltn | grep -q ':8080 '; then
        echo "錯誤：連接埠 8080 已被佔用。請先確認佔用程序或改用 switch.sh 切換。" >&2
        exit 1
    fi
fi

tmp=$(mktemp)
render_template desktop-pet-admin.service > "$tmp" || { rm -f "$tmp"; exit 1; }
sudo install -m 644 "$tmp" "$UNIT"
rm -f "$tmp"
echo "== systemd-analyze verify"
sudo systemd-analyze verify "$UNIT" && echo "verify OK"
sudo systemctl reset-failed desktop-pet-admin 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable desktop-pet-admin
sudo systemctl start desktop-pet-admin
sleep 2
systemctl is-enabled desktop-pet-admin
systemctl is-active desktop-pet-admin
