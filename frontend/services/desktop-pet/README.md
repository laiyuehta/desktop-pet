# desktop-pet.service — 桌寵開機自動啟動（Pi）

- `desktop-pet.service`：正式 systemd unit，`Restart=always`。檔案裡的 `@USER@`／`@HOME@` 是佔位，`install.sh` 依 `/etc/desktop-pet/env`（過渡期也相容 repo 根目錄的 `deploy.env`；範本 `deploy.env.example`）代換；執行期設定（`PET_BACKEND_URL`、`PET_BACKEND_TOKEN_FILE`、`PET_PACK_DIR`、`PET_WEB_LISTEN` 等）由 `EnvironmentFile=-/etc/desktop-pet/env` 載入，`install.sh` 只讀不寫環境檔。
  - 排在 `piscreen.service`、`picam.service`、`network-online.target` 之後。
  - 依賴只用 `Wants=`，所以桌寵停止或失敗時永遠不會停止或重啟 piscreen。
- `install.sh`：安裝或更新。它會檢查 `/etc/desktop-pet/env` 是否已建立、裝 unit、跑 `systemd-analyze verify`、停掉舊的 desktop-pet（包含以前用 systemd-run 開的臨時 unit），再 enable 並啟動。

## 安裝步驟（必要前置）

1. **建立環境檔 `/etc/desktop-pet/env`（必要步驟）**：
   `desktop-pet.service` 不在命令列帶後端參數，完全透過 `EnvironmentFile=-/etc/desktop-pet/env` 載入設定；未建立此檔會無法連上後端（以離線單機模式執行），且自動更新（從 release 目錄啟動）亦依賴此檔。因此 `install.sh` 會檢查此檔是否存在：
   ```sh
   sudo mkdir -p /etc/desktop-pet
   sudo cp ~/desktop-pet/deploy.env.example /etc/desktop-pet/env
   # 若有舊版 deploy.env 亦可直接複製：sudo cp ~/desktop-pet/deploy.env /etc/desktop-pet/env
   sudo chown root:$USER /etc/desktop-pet/env
   sudo chmod 640 /etc/desktop-pet/env
   # 編輯設定（至少填寫 PET_BACKEND_URL）
   sudo nano /etc/desktop-pet/env
   ```

2. **執行安裝／更新**：
   ```sh
   sh ~/desktop-pet/frontend/services/desktop-pet/install.sh    # 安裝／更新（改 unit 參數後也跑這個）
   ```

常用指令：
```sh
sudo systemctl disable --now desktop-pet             # 停用開機啟動並停止
sudo systemctl enable --now desktop-pet              # 恢復
sudo systemctl restart desktop-pet                   # 更新程式後重啟（不會動到 piscreen）
journalctl -u desktop-pet -f                         # 日誌
```
Wi‑Fi 管理用 polkit 規則 `/etc/polkit-1/rules.d/50-desktop-pet-wifi.rules`（看執行桌寵的使用者，`sudo sh frontend/tools/wifi/install.sh <使用者>` 安裝），在 systemd 服務裡照樣有效。

## #38 升級（前後端分資料夾，一次性手動步驟）

程式搬到 `frontend/` 之後，**沒有進 git 的檔案不會自動搬**，部署時手動做一次：

```bash
sudo apt install python3-venv                     # install.sh 要建 frontend/.venv
cd ~/desktop-pet
# 動畫包：repo 內唯一位置是 frontend/packs/（--pack auto 先找 ~/.local/share/desktop-pet/packs/，再找 frontend/packs/；不再找根目錄 packs/）
[ -d packs ] && [ ! -e frontend/packs ] && mv packs frontend/packs
# deploy.env：位置不變，唯一位置是 repo 根目錄 ~/desktop-pet/deploy.env（建議改用 /etc/desktop-pet/env）
# 硬體開關（環境檔）：有相機的 Pi 加 PET_ENABLE_PICAM=1；要用 camcast 加 PET_ENABLE_CAMCAST=1
sh frontend/services/desktop-pet/install.sh       # 建 venv、裝依賴（含 shared/）、重裝 unit（ExecStart 改用 frontend/.venv/bin/python）
# split 模式（有 desktop-pet-admin）改跑：sh frontend/services/desktop-pet-admin/switch.sh
```
