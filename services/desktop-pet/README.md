# desktop-pet.service — 桌寵開機自動啟動（Pi）

- `desktop-pet.service`：正式 systemd unit，`Restart=always`。檔案裡的 `@USER@`／`@HOME@`／`@BACKEND_URL@` 是佔位，`install.sh` 依 repo 根目錄的 `deploy.env`（不進 repo；範本 `deploy.env.example`）代換；`install.sh` 只讀不寫 `deploy.env`。
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
Wi‑Fi 管理用 polkit 規則 `/etc/polkit-1/rules.d/50-desktop-pet-wifi.rules`（看執行桌寵的使用者，`sudo sh tools/wifi/install.sh <使用者>` 安裝），在 systemd 服務裡照樣有效。
