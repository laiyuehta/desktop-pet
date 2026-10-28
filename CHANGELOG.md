# 更新紀錄

格式參考 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，版號用 [語意化版本](https://semver.org/lang/zh-TW/)（tag `vX.Y.Z`）。
規則見 PROPOSAL.md「15. 開發流程（git 為主）」的「發佈與版號」。每個 PR 在「未發佈」加一行；打 tag 時把「未發佈」改成版號與日期。

## [未發佈]

### 新增
- 拆出前端後台 desktop-pet-admin（#6）：獨立 Python 程式提供管理網頁（:8080）與 Wi‑Fi 管理；桌寵主程式新增 control.sock 與 frames.sock 接口，主程式重啟時網頁與救援熱點不中斷。
- git 為主的開發流程（項目書 v2.8）：分支命名、PR＋CI 才能進 main、規格 PR、commit 訊息格式、squash merge、Issue、CODEOWNERS、不再改寫歷史、semver tag 與這份更新紀錄、只部署 tag。
- CI（`.github/workflows/ci.yml`）：Python 3.13 跑全部測試（含 node 檢查遙控頁 script）；gitleaks 掃 PR 差異與全部歷史；個人資訊檢查（`.gitleaks.toml` 的 personal 規則＋`tools/ci/check_personal.py` 雜湊比對）。
- Antigravity 加入開發：`CONTRIBUTING.md` 新增它的負責範圍、禁止事項、流程、Windows（WSL2）測試方式與 Rust 編譯說明；項目書負責範圍表與 CODEOWNERS 同步。
- CI 檢查 PNG 有沒有夾帶生成資料（`tools/ci/check_png_metadata.py`）：tEXt／zTXt／iTXt 的 prompt、workflow、parameters 等關鍵字，或任何超過 1 KB 的文字 chunk 都會失敗。
- `.github/CODEOWNERS`、PR 範本、Issue 範本（Bug、任務）、`CONTRIBUTING.md`。

### 規劃（尚未實作）
- `tools/deploy` 部署腳本：把指定 tag 部署到 Pi 或 R7，失敗時回到上一個 tag。

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
