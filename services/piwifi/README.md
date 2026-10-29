# piwifi — Wi‑Fi 與救援熱點守護程式（Rust）

`piwifi` 是專為 Raspberry Pi 3B+（1 GB RAM）設計的輕量級 Wi‑Fi 與救援熱點（`pi-hotspot`）系統守護程式，負責收斂所有 `NetworkManager` / `nmcli` 權限與網路狀態管理，並透過 `/run/piwifi/piwifi.sock`（Protocol v1）對桌寵主程式（`desktop-pet`）與管理面板（`desktop-pet-admin`）提供查詢與控制介面。

---

## 1. 為什麼獨立為 Rust 守護程式？

1. **權限隔離與安全最小化**：
   - 僅系統帳號 `piwifi` 透過 `/etc/polkit-1/rules.d/50-piwifi.rules` 取得 4 項 `NetworkManager` 權限（`wifi.scan`、`network-control`、`settings.modify.system`、`wifi.share.protected`）。
   - 主程式與管理面板不再直接執行 `nmcli`，僅讀寫 `/run/piwifi/piwifi.sock`（`piwifi:@USER@ 0660`）或讀取 `/run/piwifi/status.json`。
   - 熱點密碼集中於 `/etc/piwifi/hotspot.conf`（`root:piwifi 0640`），不進 Git、亦不外洩至 socket 回應或日誌。
2. **救援生命線獨立於主程式**：
   - 即使 Python 主程式因重構、更新或崩潰而停機，`piwifi.service` 仍獨立維持 Wi‑Fi 連線與失聯超時自動開啟 `pi-hotspot` 救援熱點，確保 SSH 與網頁管理面板不會失聯。
3. **極低資源佔用（Pi 3B+ 友善）**：
   - 純 `std` 執行緒模型（不引入 `tokio`），僅依賴 `serde` 與 `serde_json`。
   - 交叉編譯產出 `aarch64-unknown-linux-musl` 單一靜態執行檔，部署免安裝 Rust 工具鏈。

---

## 2. 編譯產物大小與記憶體佔用

| 項目 | 實測／估算數值 | 說明 |
|---|---|---|
| **`aarch64-unknown-linux-musl` Release Binary 大小** | **741,248 bytes（約 724 KiB / 0.71 MiB）** | 啟用 `opt-level = "s"`、`lto = true`、`codegen-units = 1`、`panic = "abort"`、`strip = true` 與 `+crt-static` |
| **`--observe` 模式預估常駐記憶體（RSS）** | **約 1.8 ～ 3.2 MiB**（目標 `< 8 MiB`） | 靜態 musl 連結、無執行期 GC、僅 2～3 個 `std::thread`（8 MiB stack 虛擬位址空間按需配置實體頁） |

---

## 3. 編譯與打包

### 開發機交叉編譯（`aarch64-unknown-linux-musl`）

```bash
cd services/piwifi

# 1. 安裝 musl target（首次執行）
rustup target add aarch64-unknown-linux-musl

# 2. 格式檢查、靜態分析與單元／整合測試
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test

# 3. 交叉編譯靜態執行檔
cargo build --release --target aarch64-unknown-linux-musl
ls -lh target/aarch64-unknown-linux-musl/release/piwifi
```

> `.cargo/config.toml` 已設定使用內建 `rust-lld` 與 `link-self-contained=yes` 連結靜態 musl 執行檔，無須額外安裝外部 C 交叉工具鏈。

### 產生發行壓縮包（`dist/`）

```bash
./services/piwifi/package.sh
# 產出：
#   dist/piwifi-0.1.0-aarch64-linux-musl.tar.gz
#   dist/piwifi-0.1.0-aarch64-linux-musl.tar.gz.sha256
```

---

## 4. 安裝與設定

### 目錄與檔案結構

| 檔案 | 安裝目標路徑 | 權限 | 用途 |
|---|---|---|---|
| `piwifi` | `/usr/local/bin/piwifi` | `root:root 0755` | 守護程式與 CLI 除錯客戶端（同一執行檔） |
| `piwifi.service` | `/etc/systemd/system/piwifi.service` | `root:root 0644` | systemd 服務單元（`@USER@` 由 `install.sh` 替換） |
| `50-piwifi.rules` | `/etc/polkit-1/rules.d/50-piwifi.rules` | `root:root 0644` | 授權 `piwifi` 帳號操作 NetworkManager |
| `hotspot.conf.example` | `/etc/piwifi/hotspot.conf` | `root:piwifi 0640` | 救援熱點 `SSID` 與 `PASSPHRASE`（既有檔案不覆蓋） |

### 執行安裝腳本

於 Raspberry Pi 上解壓縮打包檔後執行：

```bash
# 第一階段：以 --observe 唯讀觀察模式安裝並啟動（不影響現有網路）
sudo ./install.sh --user <pi-username> --observe

# 或直接以正式模式安裝
sudo ./install.sh --user <pi-username>
```

---

## 5. CLI 除錯與 `--observe` 平行驗證

`piwifi` 執行檔本身同時提供守護程式與 CLI 客戶端子指令：

```bash
# 啟動守護程式（正式模式）
piwifi daemon

# 啟動守護程式（唯讀觀察模式：只讀狀態，不對 NetworkManager 做任何寫入）
piwifi daemon --observe

# CLI 查詢目前狀態（對應 {"v":1,"cmd":"status"}）
piwifi status

# CLI 掃描附近 Wi-Fi（對應 {"v":1,"cmd":"scan"}）
piwifi scan

# CLI 列出已儲存網路（對應 {"v":1,"cmd":"saved"}）
piwifi saved

# CLI 訂閱即時狀態變更事件串流（對應 {"v":1,"cmd":"subscribe"}）
piwifi events
```

---

## 6. 桌寵專案機器人1：切換步驟與退回步驟

為確保 **桌寵專案機器人1** 不失聯、不停機，切換採用三階段漸進流程：

### 階段一：平行驗證（`--observe` 模式）

1. 將編譯好的套件上傳至桌寵專案機器人1並以 `--observe` 模式安裝：
   ```bash
   sudo ./install.sh --user <pi-username> --observe
   ```
2. 確認服務正常運作且記憶體佔用符合預期：
   ```bash
   systemctl status piwifi.service
   ps -o pid,user,rss,comm -C piwifi
   ```
3. 執行 `piwifi status`、`piwifi scan`、`piwifi saved` 與 `/run/piwifi/status.json`，比對輸出是否與既有 `pet/wifi.py` 完全一致。
4. 此階段主程式仍維持舊路徑（直接呼叫 `pet/wifi.py`），`piwifi` 絕不執行任何 `nmcli connection up/down/add/modify/delete`。

### 階段二：正式切換（Switchover）

1. 將 `piwifi.service` 切換為正式模式（移除 `--observe`）：
   ```bash
   sudo ./install.sh --user <pi-username>
   sudo systemctl restart piwifi.service
   ```
2. 確認 `/run/piwifi/piwifi.sock` 已建立且權限為 `piwifi:<pi-username> 0660`，並測試 `piwifi status` 回傳 `"observe": false`。
3. 在桌寵主程式環境設定啟用 `piwifi` socket 後重啟主服務：
   ```bash
   # 於環境變數或部署設定啟用 PET_WIFI_BACKEND=piwifi
   sudo systemctl restart desktop-pet.service
   ```
4. 移除舊的 `/etc/polkit-1/rules.d/50-desktop-pet-wifi.rules`（收斂權限至 `50-piwifi.rules`）。

### 退回步驟（Rollback）

若切換後發現任何異常，可在 30 秒內無縫退回原本的 `pet/wifi.py` 直呼模式，**已儲存的 Wi‑Fi 設定檔與目前連線完全不受影響**：

```bash
# 1. 停止 piwifi 守護程式（或退回 --observe 模式）
sudo systemctl stop piwifi.service
sudo systemctl disable piwifi.service

# 2. 確保舊 polkit 規則存在（供主程式使用者直接呼叫 nmcli）
sudo ./tools/wifi/install-wifi-setup.sh

# 3. 將主程式切回內建 pet/wifi.py（移除 PET_WIFI_BACKEND=piwifi）並重啟
sudo systemctl restart desktop-pet.service
```

---

## 7. 協定規格

完整的 Socket JSON 協定、指令欄位、錯誤代碼與 `/run/piwifi/status.json` 格式請見 [`PROTOCOL.md`](./PROTOCOL.md)。
