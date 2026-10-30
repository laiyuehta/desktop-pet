#!/bin/sh
# 安裝／更新桌寵自動更新服務與定時器（在 Pi 上執行）：sh frontend/services/desktop-pet-update/install.sh
# 支援 --uninstall 參數移除服務、timer 與 polkit 規則。
set -eu
cd "$(dirname "$0")"

PET_REPO_ROOT="$(cd ../../.. && pwd)"
. "$PET_REPO_ROOT/frontend/tools/deploy/common.sh"
load_deploy_env

SVC=/etc/systemd/system/desktop-pet-update.service
TMR=/etc/systemd/system/desktop-pet-update.timer
BOOT_SVC=/etc/systemd/system/desktop-pet-boot-check.service
RULE=/etc/polkit-1/rules.d/51-desktop-pet-update.rules

if [ "${1:-}" = "--uninstall" ]; then
    echo "== 移除 desktop-pet-update"
    if systemctl is-active --quiet desktop-pet-update.timer 2>/dev/null; then
        sudo systemctl stop desktop-pet-update.timer
    fi
    sudo systemctl disable desktop-pet-update.timer 2>/dev/null || true
    sudo systemctl disable desktop-pet-boot-check.service 2>/dev/null || true
    sudo rm -f "$SVC" "$TMR" "$BOOT_SVC" "$RULE"
    sudo systemctl daemon-reload
    echo "已成功移除自動更新服務、開機自檢服務與計時器。"
    exit 0
fi

require_sys_env

USER_NAME="$PET_USER"
[ "$USER_NAME" != root ] || { echo "警告：PET_USER 為 root；建議以非 root 使用者執行以符合 polkit 安全限制。" >&2; }

tmp_svc=$(mktemp)
tmp_tmr=$(mktemp)
tmp_boot_svc=$(mktemp)
tmp_rule=$(mktemp)
trap 'rm -f "$tmp_svc" "$tmp_tmr" "$tmp_boot_svc" "$tmp_rule"' EXIT INT TERM

render_template desktop-pet-update.service > "$tmp_svc"
render_template desktop-pet-update.timer > "$tmp_tmr"
render_template desktop-pet-boot-check.service > "$tmp_boot_svc"
render_template 51-desktop-pet-update.rules > "$tmp_rule"

echo "== 安裝 systemd service 與 timer"
sudo install -m 644 "$tmp_svc" "$SVC"
sudo install -m 644 "$tmp_tmr" "$TMR"
sudo install -m 644 "$tmp_boot_svc" "$BOOT_SVC"

echo "== 安裝 polkit 規則"
sudo install -d -m 755 /etc/polkit-1/rules.d
sudo install -m 644 "$tmp_rule" "$RULE"

echo "== systemd-analyze verify"
sudo systemd-analyze verify "$SVC" "$TMR" "$BOOT_SVC" && echo "verify OK"

echo "== daemon-reload 與啟動 timer"
sudo systemctl daemon-reload
sudo systemctl enable desktop-pet-boot-check.service
sudo systemctl enable desktop-pet-update.timer
sudo systemctl start desktop-pet-update.timer

echo "== 檢查 timer 狀態"
systemctl is-enabled desktop-pet-update.timer
systemctl is-active desktop-pet-update.timer
echo "已成功啟用 desktop-pet-update.timer！"
