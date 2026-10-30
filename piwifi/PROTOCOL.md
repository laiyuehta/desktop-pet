# piwifi Protocol v1 規格

本文件定義 `piwifi` 守護程式對外提供的 Unix domain socket 協定（Protocol v1）與狀態快照檔格式。

- **端點路徑**：`/run/piwifi/piwifi.sock`（可由 `PIWIFI_SOCKET` 或 `--socket` 覆寫）
- **狀態快照檔**：`/run/piwifi/status.json`（可由 `PIWIFI_STATUS` 或 `--status` 覆寫）
- **協定版本**：`1`

---

## 1. 傳輸與封包格式（Framing）

1. **連線模型**：Unix domain stream socket（`SOCK_STREAM`）。
2. **編碼與分隔**：UTF-8 編碼、以單一換行字元（`\n`，NDJSON / JSON Lines）分隔每一筆訊息。
3. **版本欄位（`"v": 1`）**：
   - 所有客戶端請求與伺服器回應皆必須包含整數欄位 `"v": 1`。
   - 若請求缺少 `"v"` 或 `"v"` 不為 `1`，伺服器會拒絕執行並回傳：
     ```json
     {"v":1,"ok":false,"error":"不支援的協定版本","error_kind":"bad_version"}
     ```
4. **互動模式**：
   - **單次請求／回應（Request / Response）**：客戶端送出一行 JSON 指令（`status`、`scan`、`saved`、`connect`、`forget`、`priority`、`hotspot`），伺服器回傳一行 JSON 結果後即可關閉連線（或在同一連線上繼續發送下一個指令）。
   - **長連線事件訂閱（Subscribe Stream）**：客戶端送出 `{"v":1,"cmd":"subscribe"}`，伺服器先回傳目前狀態快照，之後每當網路狀態變更或定期輪詢偵測到差異時，持續推送單行 JSON 事件，直到客戶端斷線。

---

## 2. 回應通用結構與錯誤分類

### 成功回應

```json
{
  "v": 1,
  "ok": true,
  "...": "依各指令定義之欄位"
}
```

### 失敗回應

```json
{
  "v": 1,
  "ok": false,
  "error": "供人類閱讀的繁體中文錯誤訊息",
  "error_kind": "錯誤分類代碼"
}
```

### 錯誤分類（`error_kind`）

| `error_kind` | 說明 | 典型觸發情境 |
|---|---|---|
| `bad_request` | 請求格式錯誤、JSON 解析失敗、未知指令或參數不合法 | 缺少 `cmd`、SSID 長度不在 1..=32 bytes、UUID 格式不合法、優先順序方向錯誤 |
| `bad_version` | 協定版本不支援 | `"v"` 缺失或不等於 `1` |
| `observe_mode` | 守護程式處於 `--observe` 唯讀觀察模式 | 收到 `connect`、`forget`、`priority`、`hotspot` 等會修改網路狀態的指令 |
| `auth` | Wi‑Fi 密碼錯誤或認證失敗 | `nmcli` 回報 `Secrets were required`、`no-secrets`、`802-11-wireless-security` 錯誤 |
| `not_found` | 找不到目標網路或已儲存連線 | `SSID not found`、`No network with SSID`、指定的 `uuid` 不存在或不是 `wlan0` 的 Wi‑Fi 設定檔 |
| `timeout` | 連線或 `nmcli` 執行逾時 | `nmcli` 逾時或回報 `Connection activation failed` / `timed out` |
| `nm_unavailable` | `NetworkManager` 未執行或無法通訊 | `NetworkManager is not running` 或無法啟動 `nmcli` |
| `command_failed` | 其他 `nmcli` 執行錯誤或系統錯誤 | 熱點設定檔缺漏、介面操作失敗等 |

---

## 3. 指令規格

### 3.1 `status` — 查詢目前狀態

不進行耗時的無線基地台掃描，快速回傳 `wlan0` 目前狀態與 IPv4 位址。

**請求**：
```json
{"v":1,"cmd":"status"}
```

**成功回應**：
```json
{
  "v": 1,
  "ok": true,
  "state": "connected",
  "ssid": "HomeWiFi",
  "ip": "192.0.2.15",
  "signal": 78,
  "hotspot": false,
  "saved_count": 2,
  "reason": "",
  "updated_at": 1700000000.123,
  "observe": false
}
```

**欄位定義**：
- `state`（字串）：
  - `"connected"`：`wlan0` 已連線至一般 Wi‑Fi AP
  - `"hotspot"`：`wlan0` 處於救援熱點（AP）模式（連線名稱為 `pi-hotspot` 或 `802-11-wireless.mode=ap`）
  - `"connecting"`：`wlan0` 正在連線／取得 IP 中
  - `"disconnected"`：`wlan0` 未連線
  - `"unavailable"`：`wlan0` 停用、硬體未就緒或 `NetworkManager` 無法存取
- `ssid`（字串或 `null`）：目前連線的 SSID（熱點模式時為熱點名稱或 `"pi-hotspot"`，未連線時為 `null`）。
- `ip`（字串或 `null`）：`wlan0` 的 IPv4 位址（不含 CIDR 前綴長度，例如 `"192.0.2.15"` 或熱點 `"10.42.0.1"`；未取得 IP 時為 `null`）。
- `signal`（整數 `0..=100` 或 `null`）：目前連線 AP 的訊號強度百分比（熱點或未連線時為 `null`）。
- `hotspot`（布林）：是否處於救援熱點模式（等價於 `state == "hotspot"`）。
- `saved_count`（非負整數）：目前已儲存的 Wi‑Fi 設定檔數量（不含 `pi-hotspot`）。
- `reason`（字串）：狀態補充原因（正常時為 `""`，若 `nmcli` 異常則包含簡短原因）。
- `updated_at`（浮點數）：Unix epoch 秒數。
- `observe`（布林）：守護程式是否以 `--observe` 模式執行。

---

### 3.2 `scan` — 掃描附近 Wi‑Fi 網路

列出附近可見的 Wi‑Fi 網路（依同名 SSID 合併並取最強訊號，已連線者排第一，其餘依訊號強度由高到低排序）。
- 一般模式下會帶 `--rescan auto`。
- `--observe` 模式下強制使用 `--rescan no`（只讀取 `NetworkManager` 快取，不觸發主動掃描）。

**請求**：
```json
{"v":1,"cmd":"scan"}
```

**成功回應**：
```json
{
  "v": 1,
  "ok": true,
  "networks": [
    {
      "ssid": "HomeWiFi",
      "signal": 88,
      "security": "WPA2",
      "in_use": true,
      "saved": true
    },
    {
      "ssid": "GuestNet",
      "signal": 54,
      "security": "",
      "in_use": false,
      "saved": false
    }
  ]
}
```

---

### 3.3 `saved` — 列出已儲存的 Wi‑Fi 網路

列出綁定 `wlan0`（或未綁定特定介面）的一般 Wi‑Fi 設定檔（自動排除 `pi-hotspot` 與 `mode=ap` 設定檔），依 `autoconnect-priority` 由高到低、再依名稱排序。**絕不包含任何密碼或金鑰欄位。**

**請求**：
```json
{"v":1,"cmd":"saved"}
```

**成功回應**：
```json
{
  "v": 1,
  "ok": true,
  "saved": [
    {
      "uuid": "11111111-2222-3333-4444-555555555555",
      "name": "HomeWiFi",
      "ssid": "HomeWiFi",
      "autoconnect": true,
      "priority": 20,
      "active": true
    }
  ]
}
```

---

### 3.4 `connect` — 連線至 Wi‑Fi 網路

連線至已儲存的設定檔（以 `uuid` 或 `ssid` 指定）或新網路（可附帶 `password` 與 `hidden`）。
- 若連線失敗且原本處於救援熱點（`pi-hotspot`）模式，守護程式會自動嘗試拉回 `pi-hotspot`，避免失去救援連線。
- 若建立新設定檔後啟用（`connection up`）失敗，會自動清理剛建立的設定檔，**絕不刪除既有已儲存的設定檔**。
- 所有操作僅針對 `wlan0`。

**請求**：
```json
{
  "v": 1,
  "cmd": "connect",
  "ssid": "OfficeWiFi",
  "password": "secret-password",
  "hidden": false,
  "uuid": null
}
```
> 註：`password`、`hidden`、`uuid` 皆為選填；以已儲存 `uuid` 連線時可省略 `ssid`（或同時提供）。密碼若非空，長度須為 `8..=63` 字元（WPA-PSK 規範）。

**成功回應**：
```json
{
  "v": 1,
  "ok": true,
  "state": "connected",
  "ssid": "OfficeWiFi",
  "ip": "192.0.2.42",
  "signal": 75,
  "hotspot": false,
  "saved_count": 3,
  "reason": "",
  "updated_at": 1700000005.0,
  "observe": false
}
```

---

### 3.5 `forget` — 忘記已儲存的 Wi‑Fi 網路

刪除指定 `uuid` 的已儲存 Wi‑Fi 設定檔（拒絕刪除 `pi-hotspot` 或非 `wlan0` 的設定檔）。

**請求**：
```json
{
  "v": 1,
  "cmd": "forget",
  "uuid": "11111111-2222-3333-4444-555555555555"
}
```

**成功回應**：
```json
{
  "v": 1,
  "ok": true,
  "uuid": "11111111-2222-3333-4444-555555555555",
  "name": "HomeWiFi",
  "saved": []
}
```

---

### 3.6 `priority` — 調整已儲存網路的自動連線優先順序

支援三種調整方式（擇一提供）：
1. **指定數值（`uuid` + `value`）**：將 `uuid` 的 `connection.autoconnect-priority` 設為 `value`（夾限於 `-999..=999`）。
2. **上下移動（`uuid` + `direction`）**：`direction` 為 `"up"` 或 `"down"`，與相鄰設定檔交換優先順序。
3. **整批排序（`order`）**：傳入 UUID 陣列，由高到低重新賦予遞減的優先順序（步進 10）。

**請求範例（方向調整）**：
```json
{
  "v": 1,
  "cmd": "priority",
  "uuid": "11111111-2222-3333-4444-555555555555",
  "direction": "up"
}
```

**請求範例（直接設值）**：
```json
{
  "v": 1,
  "cmd": "priority",
  "uuid": "11111111-2222-3333-4444-555555555555",
  "value": 50
}
```

**成功回應**：
```json
{
  "v": 1,
  "ok": true,
  "saved": [
    {
      "uuid": "11111111-2222-3333-4444-555555555555",
      "name": "HomeWiFi",
      "ssid": "HomeWiFi",
      "autoconnect": true,
      "priority": 50,
      "active": true
    }
  ]
}
```

---

### 3.7 `hotspot` — 手動開啟或關閉救援熱點

開啟或關閉 `wlan0` 上的 `pi-hotspot` 救援熱點。
- 開啟時：若 `pi-hotspot` 設定檔尚不存在，會讀取 `/etc/piwifi/hotspot.conf` 的 `SSID` 與 `PASSPHRASE` 建立設定檔（預設 IP `10.42.0.1/24`），再執行 `connection up pi-hotspot ifname wlan0`。
- 關閉時：停用 `pi-hotspot`，並依優先順序嘗試連線已儲存的 Wi‑Fi 設定檔。

**請求**：
```json
{
  "v": 1,
  "cmd": "hotspot",
  "on": true
}
```

**成功回應**：回傳與 `status` 相同結構的狀態物件。

---

### 3.8 `subscribe` — 訂閱即時狀態變更事件

建立長連線訂閱，當 `state`、`ssid`、`ip` 或 `hotspot` 發生變化時，伺服器會主動推送一行事件 JSON。

**請求**：
```json
{"v":1,"cmd":"subscribe"}
```

**初始與後續推送事件格式**：
```json
{
  "v": 1,
  "event": "state_changed",
  "state": "connected",
  "ssid": "HomeWiFi",
  "ip": "192.0.2.15",
  "signal": 80,
  "hotspot": false,
  "saved_count": 2,
  "reason": "",
  "updated_at": 1700000010.0,
  "observe": false
}
```

---

## 4. `/run/piwifi/status.json` 格式

守護程式在啟動及每次狀態更新時，會以寫入暫存檔再 `rename` 的原子方式更新 `/run/piwifi/status.json`（權限 `0644`），供其他程序免連線快速讀取：

```json
{
  "v": 1,
  "ok": true,
  "state": "connected",
  "ssid": "HomeWiFi",
  "ip": "192.0.2.15",
  "signal": 80,
  "hotspot": false,
  "saved_count": 2,
  "reason": "",
  "updated_at": 1700000010.0,
  "observe": false
}
```

---

## 5. `--observe` 唯讀觀察模式行為規範

當 `piwifi` 以 `--observe`（或 `piwifi daemon --observe`）啟動時：
1. **允許的指令**：`status`、`scan`（強制 `--rescan no`）、`saved`、`subscribe`。
2. **拒絕的指令**：`connect`、`forget`、`priority`、`hotspot`。
   - 收到上述指令時，**絕不呼叫任何會改動系統狀態的 `nmcli` 子指令**（`connection add/modify/up/down/delete`、`device wifi connect` 等）。
   - 於 `journald` / `stderr` 記錄原本在一般模式下會執行的操作（密碼遮罩為 `<redacted>`）。
   - 回傳：
     ```json
     {"v":1,"ok":false,"error":"observe 模式","error_kind":"observe_mode"}
     ```
3. **自動救援熱點檢查**：在背景輪詢發現長時間未連線且應切換熱點時，僅記錄 `[observe] would start rescue hotspot pi-hotspot on wlan0` 日誌，不實際切換。
