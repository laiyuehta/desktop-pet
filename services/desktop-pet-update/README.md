# desktop-pet-update：Pi 自動更新服務

依 PROPOSAL.md 16（v2.9 規格，備份＋覆蓋設計）實作的 Pi 端拉取式（pull-based）自動更新程式與排程服務。

## 元件說明

- **`tools/deploy/autoupdate.py`**：自動更新主程式（僅使用 Python 標準函式庫）。
  - 以 Git smart HTTP 查詢遠端 `main` 分支最新 commit（免 GitHub API 配額限制）。
  - 驗證 GitHub Actions CI check-runs（`tests (Python 3.13)` 與 `secrets & personal-info scan` 必過）。
  - 從 GitHub codeload 下載 tarball 並解壓至 `.staging/`，嚴格防止目錄穿越。
  - 執行切換前檢查（`compileall`、`main.py --help`、`autoupdate.py --self-test`）。
  - 檢查手動步驟（`tools/deploy/manual-steps.txt`）與 16.7 項目（有改動時標記為 `held` 不自動切換）。
  - **原地備份＋覆蓋更新流程（In-place Backup & Overwrite）**：
    1. 拿鎖並通過 `PAUSE` / `PET_AUTOUPDATE=0` / dry-run 檢查。
    2. 將現有檔案複製至暫存備份資料夾，完成後原子取代舊備份；絕不預先清空舊備份，確保中途斷電或錯誤時備份完整。
    3. 下載新版只覆蓋清單內檔案；`packs/` 與 `deploy.env` 全程不動；新版已刪除之檔案自目錄中移除。
    4. 比對檔案變更雜湊，僅重啟有改動的服務（`desktop-pet`、`picam`），無關變更不重啟。
    5. 重啟服務並於 120 秒內驗證 `/v1/status`、幀數遞增與後端連線，若驗證失敗自動從備份還原並重啟。
    6. 還原失敗記錄警示；失敗版本記錄至 `failed.json`，後續週期不再自動重試同一 commit。
- **`desktop-pet-boot-check.service`**：`Type=oneshot` 開機自檢服務，開機時於 `desktop-pet.service` 啟動後執行 `autoupdate.py --boot-check`。
  - 偵測覆蓋中途中斷（如斷電導致 `update_state.json` 停留於 `updating`）自動從備份還原。
  - 更新後第一次開機自檢失敗（狀態為 `restarting`）自動從備份還原。
  - 舊版本檔案若本來就有缺失則僅記錄警告，絕不修改或覆蓋檔案。
- **`desktop-pet-update.service`**：`Type=oneshot` systemd 服務，以 `@USER@` 執行。
- **`desktop-pet-update.timer`**：定時器（開機後 5 分鐘、每 5 分鐘觸發一次，隨機抖動 60 秒）。
- **`51-desktop-pet-update.rules`**：polkit 規則，允許 `@USER@` 無密碼重啟 `desktop-pet`、`desktop-pet-admin`、`picam`、`piwifi`。嚴格禁止碰觸 `piscreen.service`，嚴格禁止重開機與 `daemon-reload`。

## 安裝與啟用

在 Pi 上執行（需 sudo 權限安裝 systemd 與 polkit 規則）：

```sh
sh services/desktop-pet-update/install.sh
```

檢查 timer 與開機自檢服務狀態：

```sh
systemctl is-active desktop-pet-update.timer
systemctl list-timers desktop-pet-update.timer
systemctl is-enabled desktop-pet-boot-check.service
```

## 移除

若需停用並移除自動更新服務與計時器：

```sh
sh services/desktop-pet-update/install.sh --uninstall
```

## 手動操作與除錯指令

手動執行單次檢查（不等待計時器）：

```sh
python3 tools/deploy/autoupdate.py --once
```

模擬檢查（Dry Run，僅印出計畫操作而不進行下載或覆蓋）：

```sh
python3 tools/deploy/autoupdate.py --dry-run
```

開機自我檢查與中斷復原：

```sh
python3 tools/deploy/autoupdate.py --boot-check
```

手動從備份還原上一版本：

```sh
python3 tools/deploy/autoupdate.py --rollback
```

查詢目前更新狀態：

```sh
python3 tools/deploy/autoupdate.py --status
```

模組自我檢測：

```sh
python3 tools/deploy/autoupdate.py --self-test
```

手動清除失敗紀錄並重試先前失敗的 commit：

```sh
python3 tools/deploy/autoupdate.py --retry
```

## 暫停更新

有三種方式可暫停自動更新（現場除錯用）：

1. **環境檔暫停**：在 `/etc/desktop-pet/env` 設定 `PET_AUTOUPDATE=0`。
2. **暫停檔暫停**（無需 sudo）：建立暫停檔：
   ```sh
   touch ~/.local/state/desktop-pet/update/PAUSE
   ```
   刪除該檔案即可恢復自動更新。
3. **本地檔案修改自動暫停**：若手動修改了目錄內的檔案，更新程式比對 `MANIFEST.sha256` 不符將自動暫停更新，避免覆蓋現場除錯程式碼。

## 日誌檢視

- **systemd 執行日誌**：
  ```sh
  journalctl -u desktop-pet-update -n 50 --no-pager
  journalctl -u desktop-pet-boot-check -n 50 --no-pager
  ```
- **更新歷史紀錄檔**：
  ```sh
  tail -n 20 ~/.local/state/desktop-pet/update/updates.jsonl
  ```
- **目前狀態檔**：
  ```sh
  cat ~/.local/state/desktop-pet/update/status.json
  ```
