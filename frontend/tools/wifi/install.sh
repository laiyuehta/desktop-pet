#!/bin/sh
# 在 Pi 上安裝 Wi‑Fi 設定需要的系統檔（要 sudo，只在部署時跑一次；桌寵程式本身不用 sudo）。
#   sudo sh tools/wifi/install.sh [使用者]   （預設 = deploy.env 的 PET_USER 或呼叫 sudo 的使用者；規則檔裡的 @USER@ 是佔位）
# 1. polkit 規則 → /etc/polkit-1/rules.d/50-desktop-pet-wifi.rules
# 2. 設定熱點密碼檔 → /etc/desktop-pet/wifi-hotspot.conf（root:<使用者> 0640；已存在就不動；密碼不會印出來）
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
# 使用者：參數 > deploy.env 的 PET_USER > 呼叫 sudo 的使用者
[ -n "${1:-}" ] && PET_USER="$1"
PET_REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
. "$PET_REPO_ROOT/frontend/tools/deploy/common.sh"
load_deploy_env
USER_NAME="$PET_USER"
[ "$USER_NAME" != root ] || { echo "用法：sudo sh tools/wifi/install.sh <執行桌寵的使用者>（或在 deploy.env 設 PET_USER）"; exit 1; }
render_template "$HERE/50-desktop-pet-wifi.rules" > /etc/polkit-1/rules.d/50-desktop-pet-wifi.rules
chmod 0644 /etc/polkit-1/rules.d/50-desktop-pet-wifi.rules
install -d -m 0750 -o root -g "$USER_NAME" /etc/desktop-pet
CONF=/etc/desktop-pet/wifi-hotspot.conf
if [ ! -s "$CONF" ]; then
    umask 077
    python3 - "$CONF" <<'PY'
import secrets, string, sys
alphabet = string.ascii_letters + string.digits
pw = "".join(secrets.choice(alphabet) for _ in range(16))
with open(sys.argv[1], "w") as fh:
    fh.write("# desktop-pet 設定熱點（PetRescue-AP）密碼；只放在這台 Pi，不要進 repo\n")
    fh.write("ssid=PetRescue-AP\npassword=" + pw + "\n")
PY
fi
chown root:"$USER_NAME" "$CONF"
chmod 0640 "$CONF"
echo "已安裝 polkit 規則與 $CONF（$(stat -c '%U:%G %a' "$CONF")）"
