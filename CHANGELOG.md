# 更新紀錄

格式參考 [Keep a Changelog](https://keepachangelog.com/zh-TW/1.1.0/)，版號用 [語意化版本](https://semver.org/lang/zh-TW/)（tag `vX.Y.Z`）。
規則見 PROPOSAL.md「15. 開發流程（git 為主）」的「發佈與版號」。每個 PR 在「未發佈」加一行；打 tag 時把「未發佈」改成版號與日期。

## [未發佈]

### 新增
- 前後端連線安全與 API 金鑰（#32）：依 PROPOSAL v2.7 實作「每台裝置一把金鑰、綁定 pet_ids」安全架構，後端設定僅存 SHA-256 雜湊；新增 `python -m backend.keys` CLI 工具（支援 new、list、enable、disable、revoke，金鑰寫入 0600 檔案且不印出至螢幕）；後端 `/v1/*` 端點強制要求 `X-Pet-Token`，以 `hmac.compare_digest` 進行常數時間比對避免時序攻擊；落實「沒設定就不開放」（無金鑰回 503，`insecure_dev` 僅限綁定 127.0.0.1）；公開 `/health` 精簡為只回 `{"ok": true}`，詳細指標移至 `GET /v1/health`；實作記憶體安全守護（每金鑰 120/分、照片 10/分、同 IP 5 分鐘錯 10 次鎖 15 分鐘、全域錯誤上限、信任反向代理 `X-Forwarded-For`、錯誤嘗試聚合記錄）；連線逾時 15 秒、最多 32 條連線、標頭單行上限 8 KB、非法 Content-Length 回 400；Pi 端客戶端非 HTTPS 且非私有 IP/localhost 時拒送金鑰（可由 `--allow-insecure-lan` 放行），收到 401 自動重新讀取金鑰檔重試。
- 情緒強度狀態接上主程式（#19）：`config/states.json` 新增 `emotions` 設定（上限等級、`sleep` 僅接受 `surprised`、觸控時間窗與次數）；`pet/app.py` 支援 `emotion_*` 觸發、同系列升級到上限並重設計時器、`pat`（`happy_1..3`）與 `poke`（`surprised_1` → 連戳進 `annoyed_1..2`）輸入對應；`pet/backend_client.py` 支援 `emotion` 指令帶 `level` 切換狀態（未帶 `level` 維持只換眼睛）；`backend/mood.py` 依心情值決定摸頭起始等級；`pet/web/remote.html` 改由 `/status` 動態產生全部狀態按鈕。
- `services/piwifi/`：Rust 實作的 Wi‑Fi 與救援熱點守護程式（Protocol v1 `/run/piwifi/piwifi.sock`、`/run/piwifi/status.json`、`--observe` 唯讀觀察模式、CLI 除錯子指令、假 `nmcli` 測試、`aarch64-unknown-linux-musl` 交叉編譯與 CI `rust (piwifi)` 檢查）。
- 拆出前端後台 desktop-pet-admin（#6）：獨立 Python 程式提供管理網頁（:8080）與 Wi‑Fi 管理；桌寵主程式新增 control.sock 與 frames.sock 接口，主程式重啟時網頁與救援熱點不中斷。
- Antigravity 改用自己的 GitHub 帳號 `@laiyueda12-star`：CODEOWNERS 把它負責的路徑換成該帳號，CONTRIBUTING 註明認證只設在這個 repo。
- git 為主的開發流程（項目書 v2.8）：分支命名、PR＋CI 才能進 main、規格 PR、commit 訊息格式、squash merge、Issue、CODEOWNERS、不再改寫歷史、semver tag 與這份更新紀錄、只部署 tag（v2.9 改成跟著 main）。
- CI（`.github/workflows/ci.yml`）：Python 3.13 跑全部測試（含 node 檢查遙控頁 script）；gitleaks 掃 PR 差異與全部歷史；個人資訊檢查（`.gitleaks.toml` 的 personal 規則＋`tools/ci/check_personal.py` 雜湊比對）。
- Antigravity 加入開發：`CONTRIBUTING.md` 新增它的負責範圍、禁止事項、流程、Windows（WSL2）測試方式與 Rust 編譯說明；項目書負責範圍表與 CODEOWNERS 同步。
- CI 檢查 PNG 有沒有夾帶生成資料（`tools/ci/check_png_metadata.py`）：tEXt／zTXt／iTXt 的 prompt、workflow、parameters 等關鍵字，或任何超過 1 KB 的文字 chunk 都會失敗。
- **自動部署（Pi 與 R7 自動更新器，#13、#14、PR #37）**：
  - 共用更新核心模組 `shared/shared/autoupdate.py` 與 `shared/shared/version.py`：以 Git smart HTTP 查詢遠端 main 分支最新 commit、雙重驗證 GitHub Actions CI check-runs（`tests (Python 3.13)` 與 `secrets & personal-info scan` 必過）、安全解壓 codeload tarball。
  - 原地「備份＋覆蓋」更新設計（依使用者決定全面取代多版本 symlink 與 junction 切換）：
    - 取得 `autoupdate.lock` 行程鎖（Unix: `fcntl.flock`、Windows: `msvcrt.locking` 具備原子排他鎖定）並通過 `PAUSE`、`PET_AUTOUPDATE=0`、dry-run 檢查後才動檔案。
    - 原子建立暫存備份（中途中斷舊備份完整無損）。
    - 依平台 sparse 篩選：Pi 僅更新 `frontend/` 與 `shared/`（排除 `backend/` 與 `piwifi/`）；R7 僅更新 `backend/` 與 `shared/`（排除 `frontend/` 與 `piwifi/`）。
    - 依賴變更時在現有 venv 重新執行 pip，絕不刪除或覆蓋 `frontend/.venv` 與 `backend/.venv`。
    - `MANIFEST.sha256` 僅記錄新版覆蓋之檔案，回退時嚴格僅刪除新發布引入之檔案 `(newly_overwritten ∪ failed_manifest) - backup_manifest`，徹底保護非 repo 本機檔案（筆記、存檔、自訂包、`deploy.env`）在更新或回退過程中不被誤刪。
    - 比對新舊版本（以舊版 `MANIFEST.sha256` 與新版發布掃描聯集比對）精確偵測刪檔與 requirements 變更，僅在實際影響時重啟對應服務，排除本機未追蹤檔案干擾。
    - `affected` 受影響服務清單持久化記錄於 `update_state.json`，於手動或自動回退時依此紀錄決定是否重啟服務（未影響者維持運行不盲目重啟）。
    - 外部行程呼叫全面設定 timeout 逾時防護，防止更新程式異常掛起佔用行程鎖；POSIX 下執行子行程（如 `ensure_frontend_venv` 與 pip）採用 `start_new_session=True` 獨立行程群組，逾時以 `os.killpg(SIGKILL)` 徹底清理孫行程，防止背景行程殘留持續修改虛擬環境；pip 逾時預設調升至 600 秒（支援 `PET_PIP_TIMEOUT` 自訂）。
    - 切換後健康檢查：Pi 驗證 unit active、fps ≥ 18、畫面幀數遞增、piscreen 唯讀檢查，後端離線視為警告不阻擋；R7 公開 `/health` 回應 Header `X-Commit`、`/v1/health` 帶 `commit`，直接向伺服器連線比對真實執行 commit；失敗或覆蓋異常（如磁碟空間耗盡）自動自備份還原並記錄至 `failed.json`，後續週期不再自動嘗試同一 commit。
    - 開機自檢（boot_check）與週期中斷自動復原：`run_cycle` 於起首主動偵測前次更新是否中斷或留在 `restarting` / `pending_boot_check`，先對目標版本驗證健康檢查，失敗立即自備份還原並寫入 `failed.json`，成功則標記為 `success` 並清理暫停標記；`boot_check` 遇 `in_progress` 且 `pending_boot_check` 時正確執行健康檢查自檢而非誤觸斷電還原。
    - `load_manifest` 與 `overwrite_release` 嚴格過濾包含 `..` 或絕對路徑之不合規條目，防範目錄穿越刪檔。
    - R7 虛擬環境與 pip 缺失時一致硬性報錯，移除 `sys.executable` 隱性退回。
    - 每輪週期嘗試皆記錄於 `updates.jsonl`；更新器支援 `--status` 狀態查詢與 `--retry` 重新嘗試失敗版本。
  - Pi 服務與排程（`frontend/services/desktop-pet-update/`）：
    - `desktop-pet-update.service`（oneshot）與 `desktop-pet-update.timer`（5 分鐘週期；預設檢查間隔 `PET_AUTOUPDATE_INTERVAL=300`，`--once` 受檢查間隔保護，`--force` 強制執行）。
    - `desktop-pet-boot-check.service` 開機自檢服務。
    - `51-desktop-pet-update.rules` polkit 規則：僅允許重啟 desktop-pet、desktop-pet-admin、picam、piwifi，嚴禁 touch piscreen、嚴禁重開機與 daemon-reload。
  - R7 服務與排程（`backend/autostart/`）：
    - `backend/autostart/install_update_task.ps1` 與 `uninstall_update_task.ps1`：建立/移除 Windows 工作排程器 `DesktopPet Update` 工作，背景隱藏每 5 分鐘執行一次 `autoupdate.py --once`，傳入 `--install-dir` 錨定 repo 根目錄。
  - 單元測試完整覆蓋：
    - `shared/tests/test_autoupdate.py`：測試 smart HTTP pkt-lines、CI check-runs 判定、安全解壓防穿越、保護路徑、連續兩次更新非 repo 檔案留存驗證、備份還原、跨平台檔案鎖與異常中斷回退。
    - `frontend/tests/test_autoupdate.py`：測試 Pi systemd 重啟偵測、切換前檢查、held 條件、Pi 健康檢查、開機自檢與 polkit/systemd 模板。
    - `backend/tests/test_autoupdate_r7.py`：測試 R7 後端變更偵測、R7 健康檢查與真實 commit 比對、R7 備份覆蓋週期、回退後重啟、兩週期更新與回退本機檔案留存驗證、Windows 排程腳本。
- `.github/CODEOWNERS`、PR 範本、Issue 範本（Bug、任務）、`CONTRIBUTING.md`。

### 變更
- #38 第二階段（模組拆分）：新增 `shared/`（`protocol` 協定常數、`apikey` 金鑰格式、`envfile` 環境檔格式，含極簡 `pyproject.toml`），`frontend/requirements.txt`、`backend/requirements.txt` 第一行 `-e ../shared`；Pi 改用 `frontend/.venv`（install／switch／rollback 腳本建立，unit 的 ExecStart 用 venv 的 python）；R7 改用 `backend\.venv`（新增 `backend\setup_venv.ps1`，`install_task.ps1`、`start_hidden.cmd`、`run_backend.bat` 預設用它，不再用 ComfyUI 內附 python）；環境檔新增硬體開關 `PET_ENABLE_PICAM`、`PET_ENABLE_CAMCAST`（picam 安裝與 camcast 啟動照開關）；測試拆成 `frontend/tests`、`backend/tests`、`shared/tests`、`tools/ci/tests`；CI 依路徑分流成 `frontend.yml`、`backend.yml`、`piwifi.yml`（由 `ci.yml` 用 paths-filter 呼叫，必過 check 名稱不變）。動畫包改放 repo 外的 `~/.local/share/desktop-pet/packs/`（`--pack auto` 與 `tools/bake.py` 預設都用這裡，不再找 repo 內的 packs）、`deploy.env` 只放 repo 根目錄（升級時手動搬，見 `frontend/services/desktop-pet/README.md`）；Pi 的 venv 可離線建立（`.pth` 指向 `shared/`），沒有 venv 時前後端都會退回 repo 的 `shared/`；rollback.sh 先備妥 venv／unit 才停 admin；piwifi 共用日誌擷取的測試改成序列執行；R7 的 `install_task.ps1` 不再從 PATH 找 pythonw（優先順序：`-PythonW` 參數 > `PET_PYTHONW` > `backend\.venv`；`start_hidden.cmd` 為 `PET_PY` > venv）；客戶端金鑰規則測試移到 `frontend/tests`；修正 `piwifi/package.sh` 的 repo 根目錄、admin 讀 `deploy.env` 的位置。另外：`--pack auto` 找不到動畫包時印 WARNING（列出找過的路徑；非 systemd 下也印到 stderr）；動畫包預設位置遵循絕對路徑的 `XDG_DATA_HOME`（`$XDG_DATA_HOME/desktop-pet/packs`，main 與 bake 共用）；`ensure_frontend_venv` 把 requirements.txt 雜湊存在 `.venv/.requirements.sha256`，變了就重跑 pip，pip 失敗會讓 install／switch／rollback 中止；行為變更：`backend\setup_venv.ps1 -Python` 只接受單一執行檔路徑（例如 `C:\Python313\python.exe`），不再接受 `"py -3"` 這種含參數的字串。
- 前後端分資料夾（#38 第一階段，只搬移不拆模組）：Pi 端程式 `main.py`、`pet/`、`config/`、`assets/`、`preview/`、`requirements.txt`、Pi 用工具（`tools/bake.py` 等、`tools/deploy/`、`tools/wifi/`）與 `services/`（desktop-pet、desktop-pet-admin、picam、camcast）搬到 `frontend/`；Rust 守護程式 `services/piwifi/` 搬到頂層 `piwifi/`；`backend/`、`tools/ci/` 留在原處（`tests/` 在第二階段拆成 `frontend/tests`、`backend/tests`、`shared/tests`、`tools/ci/tests`；unittest 需要 `-t`）。systemd unit 的 `WorkingDirectory`／`ExecStart` 改成 `@HOME@/desktop-pet/frontend`，Pi 上要重跑 install.sh 重新安裝 unit。
- 部署環境檔檢查與測試相容性整理（#35）：`tools/deploy/common.sh` 的 `require_sys_env` 支援 `--warn` 模式，回退時輸出警告而非錯誤；`services/desktop-pet-admin/rollback.sh` 環境檔檢查改用 `require_sys_env --warn || true`，相容自訂 `PET_DEPLOY_ENV` 且無環境檔時僅警示不阻擋回退；`tests/test_env_config.py` 與 `tests/test_autostart.py` 改以 `unittest.TestCase` 實作，以真正腳本與 stub 完整覆蓋 rollback 執行，使無 pytest 之環境（如 Pi 3B 實機）亦能直接以標準庫 `unittest` 執行驗證；更正 CHANGELOG #26 條目誤植之 `deploy.env.example` 描述。
- 測試穩定性改善與烘焙測試健全（#25）：修復 `tests/test_backend.py` 離線對話回退門檻（< 2.5s）並以請求次數（`len(llm.calls) == 1`）嚴格斷言逾時不再重試；`tests/test_pack.py` 切換延遲邊界門檻微調為 `>= 199` 消除浮點精度抖動；`tests/test_emotion_levels.py` 將動畫包情緒檢查抽為模組函式 `verify_manifest_emotions`，CI 新增輕量真實 `bake()` 覆蓋全 16 狀態 240 組轉移片段；`pet/baker.py` 保留 `transition_pairs()` 作為轉移擴充點。
- 後端客戶端迴圈測試門檻與清理（#30、#40、#44）：放寬 `tests/test_backend_client.py` 中 `test_hanging_backend_does_not_block_loop` 總耗時門檻為 6.0s（相容 Pi 3B 實測 40 格約 4.41–4.44s），耗時評估改以單一 `elapsed` 變數比較與輸出訊息；單格延遲門檻（預設 8 幀 = 0.4s）上限 clamp 在後端逾時（0.5s）以下（`min(env, timeout*0.9)`），並直接讀取 `app.backend.timeout`；簡化 `FakeBackend.do_POST` 異常捕捉為 `except OSError`；放寬 `test_offline_backend_does_not_block_loop` 平均單格門檻（預設 2 幀 = 0.1s）；支援透過環境變數 `PET_TEST_LOOP_MAX_FRAME_S`、`PET_TEST_LOOP_MAX_TOTAL_S` 與 `PET_TEST_LOOP_PER_FRAME_S` 自訂門檻；`tests/conftest.py` 保留 `PET_TEST_*` 環境變數。
- 部署防呆與日誌警告整理（#26）：將建立 `/etc/desktop-pet/env` 列為各服務安裝與切換腳本（`install.sh`、`switch.sh`）之檢查步驟，相容舊版 `deploy.env` 並輸出警告，緊急回退（`rollback.sh`）改為只警示不阻擋；移除 `main.py` 於 `parse_args` 階段在 `logging.basicConfig` 前輸出的未設定後端重複警告，統整於 `main` 中格式化輸出一次。
- 設定改用環境變數與環境檔（自動部署第 1 步，#12）：Pi 主程式、前端後台、安裝腳本與 systemd unit 支援從 `/etc/desktop-pet/env`（過渡期相容 `deploy.env`）讀取設定並移除 `@BACKEND_URL@` 佔位；R7 後端、`supervise.py`、`start_hidden.cmd` 支援 `%LOCALAPPDATA%\desktop-pet\env` 與四層設定優先順序，設定、token 檔、存檔、照片、動畫包與日誌皆可透過環境變數放至版本目錄外；取消 `deploy.env.example` 範本變數註解。
- 狀態機與後端情緒等級重構（#23、#27）：後端情緒等級以狀態設定檔為單一來源延遲解析（支援 `PET_CONFIG` 與 `PetApp` 的 `--config` 覆寫）；`PetApp` 將目前設定檔的 `emotions.levels` 傳入 `BackendClient` 與 `directive_to_event`，缺少設定或載入失敗時輸出警告並退回預設；`pet/state_machine.py` 新增 `reset_input_timers()` 公開方法，`pet/app.py` 不再直接存取私有 `_timers` 欄位。
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
