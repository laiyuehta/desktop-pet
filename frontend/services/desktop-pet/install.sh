#!/bin/sh
# 安裝／更新桌寵的開機自動啟動服務（在 Pi 上執行）：sh frontend/services/desktop-pet/install.sh
# 會：裝 unit → systemd-analyze verify → 停掉舊的（含 systemd-run 臨時 unit）→ daemon-reload → enable → start。
# 不碰 piscreen.service、不碰 USB。停用：sudo systemctl disable --now desktop-pet
set -eu
cd "$(dirname "$0")"
UNIT=/etc/systemd/system/desktop-pet.service
# 部署值從 /etc/desktop-pet/env（或舊版 deploy.env）讀（不進 repo；只讀不寫）
PET_REPO_ROOT="$(cd ../../.. && pwd)"
. "$PET_REPO_ROOT/frontend/tools/deploy/common.sh"
load_deploy_env
require_sys_env
[ -n "$PET_BACKEND_URL" ] || { echo "環境檔（${PET_SYS_ENV:-/etc/desktop-pet/env} 或 deploy.env）沒設 PET_BACKEND_URL（見 deploy.env.example）" >&2; exit 1; }
ensure_frontend_venv
tmp=$(mktemp)
render_template desktop-pet.service > "$tmp" || { rm -f "$tmp"; exit 1; }
sudo install -m 644 "$tmp" "$UNIT"
rm -f "$tmp"
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
# 硬體開關（deploy.env）：picam 要另外裝；這裡只提示，不自動裝／停
if pet_flag_on "${PET_ENABLE_PICAM:-0}" && [ ! -f /etc/systemd/system/picam.service ]; then
  echo "PET_ENABLE_PICAM=1 但還沒裝 picam：sh $PET_REPO_ROOT/frontend/services/picam/install.sh"
fi
