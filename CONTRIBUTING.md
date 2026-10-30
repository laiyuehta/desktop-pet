# 參與開發

完整規則在 [PROPOSAL.md「15. 開發流程（git 為主）」](PROPOSAL.md#15-開發流程git-為主)。重點：

1. **先有 Issue**：一個任務或 bug 一個 Issue（用範本）。
2. **一個任務一個分支**，從最新的 main 開：
   `git fetch origin && git switch -c feat/<簡短名稱> origin/main`
   前綴：`feat/` 功能、`fix/` 修正、`spec/` 規格、`anim/` 動畫與美術、`chore/` 整理與工具。
3. **commit 訊息**：`<type>(<scope>): <摘要>`，例如 `fix(wifi): 熱點關閉後沒有重連`。
4. **開 PR**（用範本）：寫對應規格、`Closes #n`、測試、部署影響、回退方式。
   CI 要綠燈；由使用者核准並用 **squash merge** 合併。不要直接 push 到 main。
   **main 就是正式版**：合併後部署的是 main 的最新 commit（tag 只是里程碑，不是部署條件）；自動部署上線後，合併約 10 分鐘內 Pi 與 R7 就會自動更新（項目書「16. 自動部署」）。需要手動步驟（unit、apt、資料遷移）的 PR 要照 16.7 標記。
5. **規格**先走 `spec/` PR 給使用者核准，實作 PR 再引用那個規格版本或 PR。
6. **不要 force push、不要改寫歷史**。分支推上去以後，要跟上 main 就 merge main 進來，不要 rebase。
7. **負責範圍**看 `.github/CODEOWNERS`；改到別人負責的路徑要在 PR 標出來，請負責的人確認。
8. **不要放進 repo**：密碼、token、正式網址、區網 IP、Wi‑Fi 名稱、Windows 使用者路徑、照片、`deploy.env`、`/etc/desktop-pet/env`、`backend/config.json`。CI 會掃。新增的設定值一律做成環境變數並補進 `deploy.env.example`（見 PROPOSAL.md 16.2）。

本機測試（#38 起分兩端，requirements 第一行 `-e ../shared` 要在該端目錄下安裝）：`(cd frontend && python -m pip install -r requirements.txt pytest && python -m pytest -q)`、`(cd backend && python -m pip install -r requirements.txt) && python -m pytest -q backend/tests shared/tests`（遙控頁的 script 測試需要 node）。CI 依路徑分流：`frontend/**` 跑前端、`backend/**` 跑後端、`shared/**` 兩端都跑、`piwifi/**` 跑 Rust、`.github/workflows/**` 全跑。

## Antigravity（AI 開發 agent）

Antigravity 是使用者在自己的 Windows 電腦上跑的 AI 開發 agent，跟三個 bot 一樣是這個 repo 的開發者，**只透過 PR 貢獻**。分工見 PROPOSAL.md「15. 開發流程」的「負責範圍」。

**負責**
- Rust 守護程式：現在是 `piwifi/`，之後還有 `pitouch`、`piwheels`、`pigimbal`、`pimic`。
- 網頁介面：`desktop-pet-admin` 的頁面、R7 後端控制網頁。
- 主程式的功能與重構，例如拆出前端後台：`frontend/main.py`、`frontend/pet/`、`backend/` 的程式。
- 測試（`frontend/tests/`、`backend/tests/`、`shared/tests/`）與 CI 修正（`.github/workflows/`、`tools/ci/`）。

**不碰**
- 美術與動畫：`frontend/packs/`、`frontend/assets/`、`frontend/preview/`、動畫規格（桌寵專案機器人2 負責）。
- 機器上的設定：`deploy.env`、`/etc/desktop-pet/env`、R7 的環境檔、`backend/config.json`。新增的設定值要做成環境變數（見項目書 16.2），在 PR 寫變數名稱，不寫值。
- Pi 與 R7 上的任何東西，包括 piscreen。不改 piscreen 的程式，也不改 `piscreen.service`。
- **不用 ssh 連 Pi 或 R7，不部署，不需要任何密碼、token 或金鑰。**
- 部署跟著 main：自動部署上線前由桌寵專案機器人1 在合併後手動部署，上線後由機器自己更新；實機驗收一律由桌寵專案機器人1 負責。要在機器上量的東西寫進 PR 的「部署影響」，請它測。

**流程**
1. 動手前先讀 PROPOSAL.md 相關章節：一定要讀「整體架構」（含「主程式 ↔ 前端後台的接口」「守護程式的共同規範」「Rust 怎麼編給 Pi 3B」「piwifi」「遷移步驟」）和「15. 開發流程」，再讀任務 Issue 指定的章節。
2. 一個 Issue 一個分支：`feat/<名稱>` 或 `fix/<名稱>`，從最新的 `origin/main` 開。
3. 用自己的 GitHub 帳號 `@laiyueda12-star` 推分支、開 PR（認證只設在這個 repo，不用使用者的 SSH 金鑰或 token）；commit 作者名稱用 `Antigravity`，例如 `git -c user.name="Antigravity" commit …`。commit 要小，訊息照 `<type>(<scope>): <摘要>`。
4. 開 PR 要用範本，寫 `Closes #n` 和對應的規格章節。CI 要綠燈，紅了就修到綠。
5. **不 push 到 main，不 force push**，也不合併。PR 開好、CI 綠燈就停，等使用者審查與合併。
6. 規格不清楚或要改規格時，在 Issue 提出，由桌寵專案機器人1 開 `spec/` PR，不要自己改規格。

**在 Windows 上跑測試**
- 有些測試用 Unix socket（`socket.AF_UNIX`），原生 Windows 的 Python 沒有這個功能，實際在 Windows 原生 Python 3.13 跑過：test_io、test_pack、test_picam、test_mirror、test_camera 等約 16 個失敗，test_remote_page 卡住。
- **請用 WSL2（Ubuntu）**，跟 CI 和 Pi 一樣是 Linux：
  ```bash
  sudo apt install python3 python3-venv python3-pip nodejs   # 要 Python 3.11 以上，CI 用 3.13
  python3 -m venv .venv && . .venv/bin/activate
  (cd frontend && pip install -r requirements.txt pytest && python -m pytest -q)
  (cd backend && pip install -r requirements.txt) && python -m pytest -q backend/tests shared/tests
  ```
- CI（Ubuntu、Python 3.13、node 22）是最後標準：本機沒辦法跑的測試，以 PR 上的 CI 結果為準。

**Rust**
- 編譯目標與做法寫在 PROPOSAL.md「整體架構」→「Rust 怎麼編給 Pi 3B」：
  - 目標 `aarch64-unknown-linux-musl`，靜態連結。
  - 每個守護程式用自己的 `rust-toolchain.toml` 與 `.cargo/config.toml`。
  - 用 release 設定。
  - 執行檔不進 git。
- 在 WSL2 裡：`rustup target add aarch64-unknown-linux-musl`，連結器用 `rust-lld` 或 `gcc-aarch64-linux-gnu`。
- 單元測試在本機用 host 目標跑（`cargo test`）。
