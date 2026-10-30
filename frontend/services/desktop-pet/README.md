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

**新位置（每樣只有一個）**
- 動畫包：`~/.local/share/desktop-pet/packs/<骨架>/`（**在 repo 外**）。`PET_PACK_DIR`／`--pack` 可以指定別處；程式不再找 repo 裡的 `packs/` 或 `frontend/packs/`。找不到時會印 warning，列出找過的路徑。
- 部署環境檔：`/etc/desktop-pet/env`（建議）；過渡期相容的 `deploy.env` 只放 repo 根目錄 `~/desktop-pet/deploy.env`（位置不變）。
- 程式：`~/desktop-pet/frontend/`＋`~/desktop-pet/shared/`（**前端需要 `shared/`**）。unit 的 ExecStart 是 `~/desktop-pet/frontend/.venv/bin/python ~/desktop-pet/frontend/main.py`。
- `frontend/.venv`：install.sh 建立（`--system-site-packages` 用 apt 的 python3-pil；shared 用 `.pth` 指到 repo 的 `shared/`）。apt 已經裝好 `python3-venv python3-pil` 時**完全不需要網路**；否則第一次要有網路讓 pip 安裝 Pillow。

### 1. 備份（**一定要在替換程式檔之前**；任何一步失敗就停下來）
動畫包沒進 git，丟了救不回來。先看空間，再一次備份整個 `~/desktop-pet`（含 `packs/`、`deploy.env`）、unit 與環境檔，並驗證備份檔讀得出來：
```bash
set -e                                                 # 任何一步失敗就中止，不要往下做
df -h ~ ; du -sh ~/desktop-pet                         # 可用空間要大於 ~/desktop-pet 的大小
B=~/desktop-pet-backup-$(date +%Y%m%d-%H%M)
mkdir -p "$B"
tar -C ~ -cf "$B"/desktop-pet-full.tar desktop-pet    # 整個舊目錄（程式＋packs＋deploy.env）
tar -tf "$B"/desktop-pet-full.tar >/dev/null           # 驗證備份讀得出來
for u in desktop-pet desktop-pet-admin; do
  [ -f /etc/systemd/system/$u.service ] && sudo cp -a /etc/systemd/system/$u.service "$B"/
done
[ -f /etc/desktop-pet/env ] && sudo cp -a /etc/desktop-pet/env "$B"/etc-desktop-pet-env
ls -l "$B"
echo "備份完成：$B"
```
上面任何一行失敗（例如 SD 卡空間不夠），**不要繼續**，先處理空間再重來。

### 2. 搬動畫包、檢查設定（還在舊程式時做）
```bash
set -e
sudo apt install python3-venv python3-pil              # 之後建 venv 不需要網路
mkdir -p ~/.local/share/desktop-pet/packs
cd ~/desktop-pet
for d in packs/*/; do
  [ -d "$d" ] || continue
  n=$(basename "$d"); dst=~/.local/share/desktop-pet/packs/"$n"
  if [ -e "$dst" ]; then
    echo "略過 $n：$dst 已存在（沒有搬；請人工比對兩份後決定保留哪一份）"
  else
    mv "$d" "$dst" && echo "已搬 $n → $dst"
  fi
done
ls ~/.local/share/desktop-pet/packs/*/manifest.json   # 確認搬到了
# 檢查 PET_PACK_DIR：不可指向舊的 repo 內路徑（例如 ~/desktop-pet/packs/seed），否則會退回較慢的即時繪圖（程式會印 WARNING）
grep -H '^PET_PACK_DIR=' /etc/desktop-pet/env ~/desktop-pet/deploy.env 2>/dev/null || echo "沒有設 PET_PACK_DIR（= auto，OK）"
#   有值且指向 ~/desktop-pet/packs/... 的話改成 ~/.local/share/desktop-pet/packs/seed，或留空（auto）
# 硬體開關寫在環境檔：有相機加 PET_ENABLE_PICAM=1、要用 camcast 加 PET_ENABLE_CAMCAST=1
```

### 3. 放新程式
- **git clone 部署**：`git pull` 即可（`shared/` 跟著來，舊檔案會被 git 移除）。
- **複製／覆蓋檔案部署（Pi 目前的方式）**：要**連 `frontend/` 和 `shared/` 一起放**（sparse-checkout 也要包含 `shared/`），並刪掉根目錄的舊結構，避免誤用舊程式：
  ```bash
  cd ~/desktop-pet
  rm -rf main.py pet services tools/bake.py tools/bench.py tools/gen_seed_parts.py tools/render_demo.py tools/deploy tools/wifi config assets preview requirements.txt tests
  [ -d packs ] && rmdir packs 2>/dev/null || true       # 第 2 步搬完後應該是空的；不是空的就先人工檢查
  ls frontend/main.py shared/shared/__init__.py         # 兩個都要在
  ```
  （`deploy.env` 留在根目錄。）

### 4. 建 venv、重裝 unit
```bash
sh ~/desktop-pet/frontend/services/desktop-pet/install.sh          # 單體模式
# split 模式（有 desktop-pet-admin）改跑：sh ~/desktop-pet/frontend/services/desktop-pet-admin/switch.sh
journalctl -u desktop-pet -n 30 --no-pager | grep -E "動畫包|畫面"   # 確認有讀到動畫包、不是即時繪圖
```

### 退回舊版
只還原 `~/desktop-pet` **不夠**：新的 unit 指向 `frontend/.venv/bin/python` 和 `frontend/main.py`，舊程式沒有這些檔案，服務會起不來。要連 unit 一起還原：
```bash
B=~/desktop-pet-backup-YYYYMMDD-HHMM                       # 第 1 步的備份
sudo systemctl stop desktop-pet desktop-pet-admin 2>/dev/null
mv ~/desktop-pet ~/desktop-pet-new && tar -C ~ -xf "$B"/desktop-pet-full.tar   # 還原整個舊目錄（含舊 packs、deploy.env）
sudo cp -a "$B"/desktop-pet.service /etc/systemd/system/
[ -f "$B"/desktop-pet-admin.service ] && sudo cp -a "$B"/desktop-pet-admin.service /etc/systemd/system/
[ -f "$B"/etc-desktop-pet-env ] && sudo cp -a "$B"/etc-desktop-pet-env /etc/desktop-pet/env
# 動畫包：舊版先找 ~/.local/share/desktop-pet/packs/ 再找 ~/desktop-pet/packs/，兩邊都有，不用搬
sudo systemctl daemon-reload
sudo systemctl start desktop-pet
[ -f /etc/systemd/system/desktop-pet-admin.service ] && sudo systemctl start desktop-pet-admin
```

以後的自動更新（#37）如果會整理檔案，要排除 `frontend/.venv`；動畫包已經在 repo 外，不受影響。
