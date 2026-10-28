# desktop-pet-admin（前端後台）

獨立運行的前端管理程式（Python），對外提供管理網頁（預設 `:8080`），包含遙控、螢幕畫面同步、Wi‑Fi 設定與救援熱點。

規格見 `PROPOSAL.md`「整體架構」1-2 前端後台與遷移步驟第 1 步。

---

## 為什麼拆出前端後台？
1. **網頁掛掉不影響寵物**：Web 伺服器若異常終止，桌寵本體（動畫、狀態機、情緒表現）仍持續運作。
2. **主程式重啟時網頁與救援不中斷**：更新或重啟桌寵主程式時，使用者依然能開啟網頁與使用 Wi‑Fi 救援熱點。
3. **分工明確**：主程式只管寵物狀態與演出，不再開 Web 伺服器與管理 Wi‑Fi。

---

## 介面與 Socket
後台透過兩條 Unix Domain Socket 與桌寵主程式通訊：
- **控制 API**：`/run/desktop-pet/control.sock`（HTTP/1.1 JSON）
  - 前綴 `/v1`，包含 `/v1/status`、`/v1/event`、`/v1/control`、`/v1/actuator`、`/v1/talk`、`/v1/notice`、`/v1/skill/camera.snapshot*`。
  - 主程式離線時，後台轉送路由回應 `503 {"ok": false, "error": "桌寵主程式未執行"}`，網頁顯示「主程式重新啟動中」。
  - 每 2 秒自動重試連線。
- **畫面供應**：`/run/desktop-pet/frames.sock`（串流）
  - 訂閱主程式畫面（`subscribe` 命令，最多 2 個訂閱者）。
  - 將收到的幀同步給多個網頁觀看者（`/screen.mjpg`、`/screen.jpg`、`/screen.info`）。

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

### 切換至後台架構（第 1 步切換）
**切換時須於同一次操作完成，確保任何時間只有一個 Wi‑Fi 管理常駐：**
1. 更新 `desktop-pet.service` ExecStart 參數，加上 `--web off --wifi off`：
   - 主程式不再監聽 `:8080`，也不再執行 `WifiManager`。
2. 安裝並啟動 `desktop-pet-admin.service`：
   ```bash
   sh services/desktop-pet-admin/install.sh
   ```
   - 後台於 `0.0.0.0:8080` 開啟服務，接管 Wi‑Fi 管理與遙控轉送。
3. 驗證服務：
   - 瀏覽器開啟 `http://<Pi-IP>:8080/` 確認狀態與畫面正常。
   - `curl http://127.0.0.1:8080/admin/status` 確認後台與主程式 socket 連線正常。

### 回退步驟（Rollback）
若發生異常需回到主程式一體化模式：
1. 停止並停用後台服務：
   ```bash
   sudo systemctl disable --now desktop-pet-admin
   ```
2. 將 `desktop-pet.service` 參數復原（移除 `--web off --wifi off`，恢復原本 `--web 0.0.0.0:8080 --wifi auto`），重啟主程式：
   ```bash
   sudo systemctl restart desktop-pet
   ```
