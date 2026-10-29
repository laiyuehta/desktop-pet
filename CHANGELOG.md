# 更新紀錄

格式參考 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，版號用 [語意化版本](https://semver.org/lang/zh-TW/)（tag `vX.Y.Z`）。
規則見 PROPOSAL.md「15. 開發流程（git 為主）」的「發佈與版號」。每個 PR 在「未發佈」加一行；打 tag 時把「未發佈」改成版號與日期。

## [未發佈]

### 新增
- 情緒強度狀態接上主程式（#19）：`config/states.json` 新增 `emotions` 設定（上限等級、`sleep` 僅接受 `surprised`、觸控時間窗與次數）；`pet/app.py` 支援 `emotion_*` 觸發、同系列升級到上限並重設計時器、`pat`（`happy_1..3`）與 `poke`（`surprised_1` → 連戳進 `annoyed_1..2`）輸入對應；`pet/backend_client.py` 支援 `emotion` 指令帶 `level` 切換狀態（未帶 `level` 維持只換眼睛）；`backend/mood.py` 依心情值決定摸頭起始等級；`pet/web/remote.html` 改由 `/status` 動態產生全部狀態按鈕。
- `services/piwifi/`：Rust 實作的 Wi‑Fi 與救援熱點守護程式（Protocol v1 `/run/piwifi/piwifi.sock`、`/run/piwifi/status.json`、`--observe` 唯讀觀察模式、CLI 除錯子指令、假 `nmcli` 測試、`aarch64-unknown-linux-musl` 交叉編譯與 CI `rust (piwifi)` 檢查）。
- 拆出前端後台 desktop-pet-admin（#6）：獨立 Python 程式提供管理網頁（:8080）與 Wi‑Fi 管理；桌寵主程式新增 control.sock 與 frames.sock 接口，主程式重啟時網頁與救援熱點不中斷。
- Antigravity 改用自己的 GitHub 帳號 `@laiyueda12-star`：CODEOWNERS 把它負責的路徑換成該帳號，CONTRIBUTING 註明認證只設在這個 repo。
- git 為主的開發流程（項目書 v2.8）：分支命名、PR＋CI 才能進 main、規格 PR、commit 訊息格式、squash merge、Issue、CODEOWNERS、不再改寫歷史、semver tag 與這份更新紀錄、只部署 tag（v2.9 改成跟著 main）。
- CI（`.github/workflows/ci.yml`）：Python 3.13 跑全部測試（含 node 檢查遙控頁 script）；gitleaks 掃 PR 差異與全部歷史；個人資訊檢查（`.gitleaks.toml` 的 personal 規則＋`tools/ci/check_personal.py` 雜湊比對）。
- Antigravity 加入開發：`CONTRIBUTING.md` 新增它的負責範圍、禁止事項、流程、Windows（WSL2）測試方式與 Rust 編譯說明；項目書負責範圍表與 CODEOWNERS 同步。
- CI 檢查 PNG 有沒有夾帶生成資料（`tools/ci/check_png_metadata.py`）：tEXt／zTXt／iTXt 的 prompt、workflow、parameters 等關鍵字，或任何超過 1 KB 的文字 chunk 都會失敗。
- `.github/CODEOWNERS`、PR 範本、Issue 範本（Bug、任務）、`CONTRIBUTING.md`。

### 變更
- 狀態機與後端情緒等級重構（#23）：`pet/backend_client.py` 的情緒最高等級改由 `config/states.json`（`emotions.levels`）單一來源載入；`pet/state_machine.py` 新增 `reset_input_timers()` 公開方法，`pet/app.py` 不再直接存取私有 `_timers` 欄位。
- `pet/baker.py`、`pet/pack.py`：動畫包替所有狀態配對烘焙過渡片段（16 個狀態 × 15 = 240 組），未烘到的轉移在切換點或非循環片段結尾直接退回目標狀態第 0 格播放（#18）。
- `services/piwifi/`：`piwifi.service` 狀態檔環境變數對齊為 `PIWIFI_STATUS`（程式同時相容 `PIWIFI_STATUS_FILE`），`README.md` 硬體目標修正為 Pi 3B 並補上實測 RSS（約 0.8 MB）。
- `desktop-pet-admin` 無觀看者時取消 `frames.sock` 訂閱以降低主程式與後台閒置 CPU；有人觀看時再以 20 fps 訂閱，且 `/screen.jpg` 與 `/screen.info` 在無訂閱時直接以 `latest` 取得最新畫面（#11）。
- 部署規則改成**跟著 main**（項目書 v2.9）：main 只收 CI 綠燈的 PR，所以正式機器部署 main 的最新 commit；tag 只當里程碑，不是部署條件。PR 範本的「回退方式」同步更新。

### 規劃（尚未實作）
- 自動部署規格（項目書 v2.9「16. 自動部署」）：端點、帳號、路徑、token 檔位置改用環境變數（Pi `/etc/desktop-pet/env`、R7 環境檔；`deploy.env.example` 列出新變數）；Pi（systemd timer）與 R7（工作排程器）定時用 HTTPS 拉 main，版本目錄＋連結切換、健康檢查、失敗自動回退並記下失敗 commit、`PET_AUTOUPDATE=0` 暫停、管理頁顯示目前 commit。
- `tools/deploy/deploy.sh` 手動部署腳本：把 main 上的指定 commit 部署到 Pi 或 R7（含需要手動安裝的項目），失敗時回到上一版。

## 起點：2026-10-29 的 main（`4680c4e`，還沒有版號）

這是改用 git 流程之前的狀態，當作第一份紀錄。之後第一個 tag 預計是 `v2.8.0`（這個流程合併後）。
舊的 tag `proposal-v1.0` 只標記項目書 v1.0，不是發佈版本。

**裝置端（Pi 3B）**
- 預錄動畫包播放（`tools/bake.py` 產生），所有狀態 20 fps；對話框即時疊加。
- 事件驅動狀態機（idle、happy、bored、sleep）、成長資料、擴充模組與外部控制器接口（只有假控制器）。
- 輸出到 piscreen（只當 client）；網頁遙控（內網）含螢幕畫面同步、Wi‑Fi 設定（掃描、連線、記錄、自動重試、救援熱點 `PetRescue-AP`）。
- 相機常駐服務 picam；能力模組 camera.snapshot（共用同一張照片、背景上傳、待傳補傳）。
- 開機自動啟動 `desktop-pet.service`；部署值用 `deploy.env`（不進 git）代換 unit 裡的佔位。

**後端（R7）**
- 事件／對話 API、心情與成長、存檔；本機 LLM 客戶端與路由，LLM 沒開時用備用台詞。
- `api_token`：`/v1/*` 要帶 token；照片 API 一定要 token；`/health` 對外只回基本資訊。
- 登入自動啟動（工作排程器＋`supervise.py`）；本機設定 `config.json` 不進 git（範本 `config.example.json`）。

**規格（項目書，尚未實作）**
- v2.5 多模態模型與記憶系統（擱置）、v2.6 整體架構（三層、前端拆分、Rust 守護程式）、v2.7 後端控制網頁與 API 金鑰、公開前檢查。

**專案**
- MIT 授權；公開前整理與歷史改寫（桌寵專案機器人3，2026-10-29）。
