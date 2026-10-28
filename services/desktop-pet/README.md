# desktop-pet.service — 桌寵開機自動啟動（Pi）

- `desktop-pet.service`：正式 systemd unit。以 pi-user 身分執行，`Restart=always`。
  - 排在 `piscreen.service`、`picam.service`、`network-online.target` 之後。
  - 依賴只用 `Wants=`，所以桌寵停止或失敗時永遠不會停止或重啟 piscreen。
- `install.sh`：安裝或更新。它會裝 unit、跑 `systemd-analyze verify`、停掉舊的 desktop-pet（包含以前用 systemd-run 開的臨時 unit），再 enable 並啟動。

常用指令：
```sh
sh ~/desktop-pet/services/desktop-pet/install.sh    # 安裝／更新（改 unit 參數後也跑這個）
sudo systemctl disable --now desktop-pet             # 停用開機啟動並停止
sudo systemctl enable --now desktop-pet              # 恢復
sudo systemctl restart desktop-pet                   # 更新程式後重啟（不會動到 piscreen）
journalctl -u desktop-pet -f                         # 日誌
```
Wi‑Fi 管理用 polkit 規則 `/etc/polkit-1/rules.d/50-desktop-pet-wifi.rules`（看使用者 pi-user），在 systemd 服務裡照樣有效。
