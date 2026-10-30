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

程式搬到 `frontend/`、共用模組放在 `shared/` 之後，**沒有進 git 的檔案不會自動搬**，部署時手動做一次。

**位置（每樣只有一個）**
- 動畫包：`~/.local/share/desktop-pet/packs/<骨架>/`（**在 repo 外**；換程式碼、重建 venv、整包換掉 `frontend/` 都不會動到）。`PET_PACK_DIR`／`--pack` 可以指定別處；程式不再找 repo 裡的 `packs/` 或 `frontend/packs/`。
- 部署環境檔：`/etc/desktop-pet/env`（建議）；過渡期相容的 `deploy.env` 只放 repo 根目錄 `~/desktop-pet/deploy.env`（位置不變）。
- `shared/`：**前端需要它**。用 git clone 部署時它本來就在 repo 裡；如果是複製檔案、sparse-checkout 或其他方式部署，一定要連 `shared/`（和 `frontend/`）一起放，否則 install.sh 會中止。
- `frontend/.venv`：install.sh 建立（`--system-site-packages`，直接用 apt 的 python3-pil；shared 用 `.pth` 指到 repo 的 `shared/`）。Pillow 已經由 apt 安裝時**完全不用網路**；否則第一次要有網路讓 pip 安裝 Pillow。

```bash
sudo apt install python3-venv python3-pil         # 先裝好，之後建 venv 不需要網路
cd ~/desktop-pet
# 1) 備份動畫包（沒進 git，丟了救不回來）
[ -d packs ] && tar -cf ~/packs-backup-$(date +%Y%m%d).tar packs
# 2) 搬到 repo 外的唯一位置（目的地已存在就不搬，先人工確認）
mkdir -p ~/.local/share/desktop-pet/packs
for d in packs/*/; do n=$(basename "$d"); [ -e ~/.local/share/desktop-pet/packs/"$n" ] || mv "$d" ~/.local/share/desktop-pet/packs/"$n"; done
# 3) deploy.env 位置不變（repo 根目錄）；硬體開關寫在環境檔：有相機加 PET_ENABLE_PICAM=1、要用 camcast 加 PET_ENABLE_CAMCAST=1
# 4) 建 venv＋重裝 unit（ExecStart 改用 frontend/.venv/bin/python）
sh frontend/services/desktop-pet/install.sh
# split 模式（有 desktop-pet-admin）改跑：sh frontend/services/desktop-pet-admin/switch.sh
```

以後的自動更新（#37）如果會整理檔案，要排除 `frontend/.venv`；動畫包已經在 repo 外，不受影響。
