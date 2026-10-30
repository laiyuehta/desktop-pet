# Changelog — piwifi

所有 `piwifi` 守護程式的重要變更皆記錄於此檔案。
格式參考 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，版本號遵循語意化版本（SemVer）。

## [0.1.0] - 2026-10-29

### 新增

- **守護程式與 Protocol v1（`/run/piwifi/piwifi.sock`）**：
  - 實作 `status`、`scan`、`saved`、`connect`、`forget`、`priority`、`hotspot`、`subscribe` 八項指令與 `"v": 1` 協定檢查。
  - 實作 `/run/piwifi/status.json` 原子寫入狀態快照（暫存檔 + `rename`）。
  - 實作背景狀態監控與失聯超時自動開啟 `pi-hotspot` 救援熱點（預設 `10.42.0.1/24`）。
- **`--observe` 唯讀觀察模式**：
  - 平行驗證階段可於不動到實際網路連線的前提下比對 `status` / `scan` / `saved` 輸出，並拒絕所有寫入操作（回傳 `observe_mode` 錯誤並記錄預計動作至日誌）。
- **CLI 除錯客戶端**：
  - 同一執行檔支援 `piwifi status`、`piwifi scan`、`piwifi saved`、`piwifi events`。
- **安全與隔離**：
  - 僅操作 `wlan0` 介面，失敗時絕不刪除既有已儲存設定檔。
  - 日誌與錯誤輸出自動遮罩密碼欄位為 `<redacted>`。
  - 附帶 `piwifi.service`、`50-piwifi.rules`（僅開放 4 項 NetworkManager polkit action 予 `piwifi` 系統帳號）、`hotspot.conf.example`、`install.sh` 與 `package.sh`。
