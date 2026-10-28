#!/bin/sh
# 安裝／更新桌寵的開機自動啟動服務（在 Pi 上執行）：sh services/desktop-pet/install.sh
# 會：裝 unit → systemd-analyze verify → 停掉舊的（含 systemd-run 臨時 unit）→ daemon-reload → enable → start。
# 不碰 piscreen.service、不碰 USB。停用：sudo systemctl disable --now desktop-pet
set -eu
cd "$(dirname "$0")"
UNIT=/etc/systemd/system/desktop-pet.service
sudo install -m 644 desktop-pet.service "$UNIT"
echo "== systemd-analyze verify"
sudo systemd-analyze verify "$UNIT" && echo "verify OK"
frag=$(systemctl show -p FragmentPath --value desktop-pet 2>/dev/null || true)
if systemctl is-active --quiet desktop-pet; then
  echo "停止目前的 desktop-pet（$frag）"
  sudo systemctl stop desktop-pet
fi
sudo systemctl reset-failed desktop-pet 2>/dev/null || true
sudo systemctl daemon-reload
frag=$(systemctl show -p FragmentPath --value desktop-pet)
[ "$frag" = "$UNIT" ] || { echo "unit 路徑不對：$frag（臨時 unit 還在？）"; exit 1; }
sudo systemctl enable desktop-pet
sudo systemctl start desktop-pet
sleep 3
systemctl is-enabled desktop-pet
systemctl is-active desktop-pet
