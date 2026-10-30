# desktop-pet-admin（前端後台）

獨立運行的前端管理程式（Python），對外提供管理網頁（預設 `:8080`），包含遙控、螢幕畫面同步、Wi‑Fi 設定與救援熱點。

規格見 `PROPOSAL.md`「整體架構」1-2 前端後台與遷移步驟第 1 步。

> **注意**：第 1 步切換後，Wi‑Fi 管理移至後台程式內執行；若後台沒在跑時將沒有救援熱點（此限制將於第 2 步由獨立常駐的 Rust `piwifi` 徹底解決）。

---

## 為什麼拆出前端後台？
1. **網頁掛掉不影響寵物**：Web 伺服器若異常終止，桌寵本體（動畫、狀態機、情緒表現）仍持續運作。
2. **主程式重啟時網頁與救援不中斷**：更新或重啟桌寵主程式時，使用者依然能開啟網頁與使用 Wi‑Fi 救援熱點。
3. **分工明確**：主程式只管寵物狀態與演出，不再開 Web 伺服器與管理 Wi‑Fi。

---

## 單一 Wi‑Fi 管理保證機制
為了確保系統任何時候只有一個 Wi‑Fi 管理實例在運作：
1. **檔案鎖互斥與自動重試**：`WifiManager` 啟動背景檢查時，會以非阻塞（`LOCK_EX | LOCK_NB`）鎖定 `/run/desktop-pet/wifi.lock`。若已有實例持有此鎖，另一個實例不會啟動執行緒，並在背景以 10 秒為週期自動重試，待前一個實例結束釋放鎖後無縫接手管理；若因權限或目錄尚未建立等系統例外，則記 WARNING 並直接管理 Wi‑Fi，避免救援熱點被意外停用。
2. **安裝指令防呆**：`frontend/services/desktop-pet-admin/install.sh` 安裝前會先檢查 `desktop-pet` 服務是否仍帶有 `--web` 或未指定 `--wifi off`。若有衝突會立刻終止並提示使用一鍵切換腳本；若後台已在執行，則先停止舊實例後再確認埠號，避免更新時被自身佔用擋住。

---

## 介面與 Socket
後台透過兩條 Unix Domain Socket 與桌寵主程式通訊：
- **控制 API**：`/run/desktop-pet/control.sock`（HTTP/1.1 JSON）
  - 前綴 `/v1`，包含 `/v1/status`、`/v1/event`、`/v1/control`、`/v1/actuator`、`/v1/talk`、`/v1/notice`、`/v1/skill/camera.snapshot*`。
  - 後台背景每 2 秒主動以 `GET /v1/health` 探測連線狀態。
  - 主程式離線時，後台轉送路由回應 `503 {"ok": false, "error": "桌寵主程式未執行"}`，網頁顯示「主程式重新啟動中」。
- **畫面供應**：`/run/desktop-pet/frames.sock`（串流）
  - 後台以 20 fps 訂閱主程式畫面（`subscribe` 命令，上限 2 個訂閱者）。
  - 將收到的幀同步給多個網頁觀看者（`/screen.mjpg`、`/screen.jpg`、`/screen.info`），依各觀看者請求之 fps 進行降頻分送。

---

## 常用參數
```bash
python3 admin.py [參數]
  --listen 0.0.0.0:8080         # 網頁監聽位址（測試可用 127.0.0.1:8081）
  --control-socket /run/...     # 主程式 control.sock 路徑
  --frames-socket /run/...      # 主程式 frames.sock 路徑
  --html pet/web/remote.html    # 遙控頁 HTML 路徑
  --wifi [auto|on|off]          # Wi‑Fi 管理（預設 auto）
  --wifi-iface wlan0            # Wi‑Fi 介面
  --wifi-hotspot [on|off]       # 救援熱點開關
  --wifi-hotspot-ssid PetRescue-AP
```

---

## 部署與切換步驟（給桌寵專案機器人1）

> **前置條件**：主程式與後台皆依賴 `/etc/desktop-pet/env` 載入設定。切換或安裝前建議先建立 `/etc/desktop-pet/env`（權限 `root:<使用者> 0640`，至少設定 `PET_BACKEND_URL`）；若完全無環境檔，`switch.sh` 與 `install.sh` 會阻擋，僅有舊版 `deploy.env` 則印警告相容；緊急回退 `rollback.sh` 僅警示不阻擋。

### 方式一：一鍵切換（建議）
切換時同一次操作完成主程式與後台設定：
```bash
sh frontend/services/desktop-pet-admin/switch.sh
```
此腳本會：
1. 將 `desktop-pet.service` 切換為 sockets 模式（使用 `frontend/services/desktop-pet/desktop-pet-split.service` 範本，帶有 `--web off --wifi off`）。
2. 安裝並啟用 `desktop-pet-admin.service`（監聽 `:8080` 並接管 Wi‑Fi）。
3. 一併重啟並檢查兩服務狀態。

### 方式二：手動切換
1. 更新主程式設定並重啟：
   - 確保包含 `RuntimeDirectory=desktop-pet`、`RuntimeDirectoryMode=0750`、`RuntimeDirectoryPreserve=yes`。
   - 修改 ExecStart 參數加上 `--web off --wifi off`（或套用 `frontend/services/desktop-pet/desktop-pet-split.service` 範本）。
   - 重新載入並重啟主程式，釋放 `:8080` 並開啟 Unix sockets：
     ```bash
     sudo systemctl daemon-reload
     sudo systemctl restart desktop-pet
     ```
2. 安裝並啟動後台：
   ```bash
   sh frontend/services/desktop-pet-admin/install.sh
   ```

### 驗證切換結果
- 瀏覽器開啟 `http://<Pi-IP>:8080/` 確認狀態與畫面正常（20 fps）。
- 執行狀態檢查指令：
  ```bash
  curl -s http://127.0.0.1:8080/admin/status
  ```
  確認 `control.connected` 為 `true` 且 `frames.connected` 為 `true`。

### 回退步驟（Rollback）
若需回到單體模式，執行一鍵回退腳本：
```bash
sh frontend/services/desktop-pet-admin/rollback.sh
```
或手動停用後台並將 `desktop-pet.service` 參數復原為 `--web 0.0.0.0:8080 --wifi auto` 後重啟。
