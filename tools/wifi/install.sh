#!/bin/sh
# 在 Pi 上安裝 Wi‑Fi 設定需要的系統檔（要 sudo，只在部署時跑一次；桌寵程式本身不用 sudo）。
#   sudo sh tools/wifi/install.sh [使用者]
# 1. polkit 規則 → /etc/polkit-1/rules.d/50-desktop-pet-wifi.rules
# 2. 設定熱點密碼檔 → /etc/desktop-pet/wifi-hotspot.conf（root:<使用者> 0640；已存在就不動；密碼不會印出來）
set -eu
USER_NAME="${1:-pi-user}"
HERE="$(cd "$(dirname "$0")" && pwd)"
sed "s/\"pi-user\"/\"$USER_NAME\"/" "$HERE/50-desktop-pet-wifi.rules" > /etc/polkit-1/rules.d/50-desktop-pet-wifi.rules
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
