# 桌寵後端（大腦）

跑在家裡內網的電腦（目前是 Windows 桌機 R7，`192.0.2.100`），負責：

- **成長、心情、存檔**：每隻寵物一個 JSON 檔（`backend/data/<pet_id>.json`，原子寫入）
- **對話**：呼叫本機 LLM（R7 上的 llama.cpp `llama-server`，OpenAI 相容），以「種子」人設回一句話
- **只傳狀態與指令**給裝置（Pi），不傳畫面；後端沒開時裝置照常運作

只用 Python 標準函式庫（CPython 3.10 以上），**不需要 pip install 任何東西**。

## 在 R7（Windows）上執行

1. 確認有 Python 3.10+（PowerShell）：
   ```powershell
   py -3 --version
   ```
   沒有的話可以從 python.org 安裝，或 `winget install Python.Python.3.12`。
2. 取得程式（私有 repo，需要 GitHub 登入）：
   ```powershell
   cd $HOME
   git clone https://github.com/laiyuehta/desktop-pet.git
   cd desktop-pet
   ```
   之後更新：`git pull`
3. 建立後端的 venv（#38 起不再借用 ComfyUI 內附 python；後端只用標準函式庫，venv 只裝共用模組 `shared/`）：
   ```powershell
   powershell -ExecutionPolicy Bypass -File backend\setup_venv.ps1
   ```
   之後 `run_backend.bat`、`start_hidden.cmd`、工作排程器（`autostart\install_task.ps1`）都會自動用 `backend\.venv`。
   注意：`install_task.ps1` **不再從 PATH 找 pythonw.exe**；優先順序是 `-PythonW` 參數 > `PET_PYTHONW` > `backend\.venv\Scripts\pythonw.exe`，都沒有就報錯。`PET_PY`（`start_hidden.cmd`）／`PET_PYTHONW` 有設時會蓋過 venv（例如暫時退回 ComfyUI 的 python），升級後請把環境檔裡指向 ComfyUI 的值清掉。
   沒有 venv 時（例如直接 `py -3 -m backend`），程式會自動把 repo 的 `shared/` 加進 sys.path。
4. 先開 llama-server（你已經會開，監聽 `http://127.0.0.1:8080`，模型別名 `qwen3-14b`）。
5. 啟動後端：
   ```powershell
   backend\run_backend.bat
   ```
   或直接 `py -3 -m backend`；背景執行用 `backend\start_hidden.cmd`（輸出寫進 `backend_out.log`／`backend_err.log`）。看到「桌寵後端 v0.1.0 啟動：http://0.0.0.0:8090/health」就好了，Ctrl+C 結束。
### 5. API 金鑰管理（PROPOSAL v2.7）

後端採用「每台裝置一把金鑰、綁定 pet_ids」架構，設定檔中**僅儲存金鑰之 SHA-256 雜湊**，金鑰本體永不存入後端設定或日誌中。

產生金鑰：
```powershell
py -3 -m backend.keys new --id pi-seed-001 --pet seed-001 --note "Pi 客廳桌寵" --output ~\.config\desktop-pet\backend-token
```
- 金鑰隨機生成並以權限 0600 寫入指定檔案（不印出至螢幕）。
- 雜湊自動寫入 `backend/config.json`（或由 `PET_BACKEND_CONFIG` 指定之路徑）。
- 查詢、啟用、停用與撤銷：
  ```powershell
  py -3 -m backend.keys list
  py -3 -m backend.keys disable --id pi-seed-001
  py -3 -m backend.keys enable --id pi-seed-001
  py -3 -m backend.keys revoke --id pi-seed-001
  ```

### 6. 在 R7 本機測試：
   ```powershell
   # 公開健康檢查（不帶金鑰只回 {"ok": true}）
   curl.exe http://127.0.0.1:8090/health

   # 帶金鑰存取 API 或查詢詳細健康指標
   $body = [Text.Encoding]::UTF8.GetBytes('{"pet_id":"seed-001","text":"你好"}')
   Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8090/v1/pet/talk -ContentType 'application/json; charset=utf-8' -Headers @{'X-Pet-Token'='<金鑰>'} -Body $body
   Invoke-RestMethod -Method Get -Uri http://127.0.0.1:8090/v1/health -Headers @{'X-Pet-Token'='<金鑰>'}
   ```
   未設定金鑰時，`/v1/*` 預設回傳 503「後端未設定金鑰」（沒設定就不開放）；本機單元測試可設 `server.insecure_dev: true`（僅在綁定 127.0.0.1 時生效）。
   帶金鑰的 `/v1/health` 裡 `llm.local.reachable` 是 `true` 代表連得到 llama-server；`/talk` 回覆的 `source` 是 `local` 代表是模型回的，`fallback` 代表用了備用台詞。

### Windows 防火牆（讓 Pi 連得到 8090）

用**系統管理員** PowerShell 執行（只允許同網段 LocalSubnet 連入 TCP 8090）：

```powershell
New-NetFirewallRule -DisplayName "DesktopPet Backend (TCP 8090, LAN only)" -Direction Inbound -Protocol TCP -LocalPort 8090 -RemoteAddress LocalSubnet -Action Allow -Profile Private
```

- 這條規則只在「私人」網路設定檔生效。用 `Get-NetConnectionProfile` 看目前網路是不是 `Private`；如果是 `Public`，可以把網路改成私人（`Set-NetConnectionProfile -InterfaceAlias "乙太網路" -NetworkCategory Private`，介面名稱以實際為準），或把上面指令的 `-Profile Private` 改成 `-Profile Any`（仍然只限 LocalSubnet）。
- 移除規則：`Remove-NetFirewallRule -DisplayName "DesktopPet Backend (TCP 8090, LAN only)"`
- llama-server 只聽 `127.0.0.1`，**不用**也不要對外開 8080。
- 從 Pi 測試：`curl http://192.0.2.100:8090/health`

### Pi 端接上後端

```bash
python3 frontend/main.py --sink piscreen --input web --backend http://192.0.2.100:8090
```

網頁遙控（`http://192.0.2.43:8080/`）多了「跟寵物說話」輸入框；寵物說的話會以對話框顯示在螢幕上。
對話框需要中文字型：`sudo apt install fonts-wqy-microhei`（小）或 `fonts-noto-cjk`，也可以用 `--font 路徑` 指定。

## 設定（環境變數與環境檔 ＋ config.json ＋ config.example.json）

設定讀取依四層優先順序（高 → 低，詳見 [PROPOSAL.md 16.2](../PROPOSAL.md#162-環境變數與設定的優先順序)）：

1. **命令列參數**：`--config`、`--host`、`--port`
2. **環境變數與環境檔**：程序環境變數優先於環境檔 `%LOCALAPPDATA%\desktop-pet\env`（可用 `PET_ENV_FILE` 指定路徑；格式為 `KEY=VALUE`，支援 `%LOCALAPPDATA%` 展開，範本見 `deploy.env.example`）：
   - `PET_BACKEND_HOST` / `PET_BACKEND_PORT`：覆寫 `server.host` / `server.port`（`start_hidden.cmd` 與 `supervise.py` 也會讀 `PET_BACKEND_PORT` 與 `PET_SUPERVISOR_LOCK_PORT`）
   - `PET_BACKEND_CONFIG`：指定本機 `config.json` 路徑（例如 `%LOCALAPPDATA%\desktop-pet\config\config.json`）
   - `PET_BACKEND_DATA_DIR`：覆寫 `data_dir`（例如 `%LOCALAPPDATA%\desktop-pet\data`，讓存檔與照片放在版本目錄外）
   - `PET_BACKEND_LOG_DIR`：`supervise.py` 與 `start_hidden.cmd` 的日誌資料夾（預設為 repo 根目錄）
   - `PET_BACKEND_API_TOKEN_FILE`：從檔案讀取 `server.api_token`（機密只放檔案，不直接寫在環境檔）
   - `PET_LLM_LOCAL_URL` / `PET_LLM_LOCAL_MODEL`：覆寫 `llm.local.base_url` / `llm.local.model`
   - `PET_CONSOLE_LISTEN`：控制頁監聽位址（`console.listen`）
3. **本機 `config.json`**：讀 `PET_BACKEND_CONFIG` 指定的路徑，未設則讀 `backend/config.json`（已列入 `.gitignore`，不會進 repo）。可以只寫要改的欄位，例如 `{"server": {"api_token": "..."}}`。
4. **內建預設 `backend/config.example.json`**：repo 裡的預設範本。程式只讀不寫，不會建立、覆蓋或刪除任何設定檔。

| 欄位 | 說明 |
|---|---|
| `server.host` / `server.port` | 監聽位址，預設 `0.0.0.0:8090` |
| `server.keys` | 每台裝置一把金鑰（陣列，只存 SHA-256，用 `python -m backend.keys new` 產生） |
| `server.api_token` | 舊版共用金鑰（過渡期相容）；未設金鑰且未設 api_token 時，`/v1/*` 一律回 503（沒設定就不開放） |
| `server.insecure_dev` | 僅限開發測試；只有綁定 `127.0.0.1` 時有效，允許無金鑰存取 `/v1/*` |
| `server.trusted_proxies` | 信任之反向代理 IP 清單（預設 `["127.0.0.1", "::1"]`），僅在信任代理時採 `X-Forwarded-For` 最右側 IP |
| `photos.retention` / `recent_in_record` | 每隻寵物保留幾張照片（預設 200）／存檔裡記幾筆最近照片（預設 20） |
| `data_dir` | 存檔資料夾（相對路徑以 `backend/` 為準） |
| `llm.local.base_url` / `model` / `timeout_s` / `retries` | 本機模型；之後換 1~3B 小模型只改這裡。llama-server 預設埠之後可能改成 9931，記得同步改 |
| `llm.local.no_think` | Qwen3 專用：加 `/no_think` 與 `enable_thinking=false`；換別的模型可設 `false` |
| `llm.local.json_mode` | 送 `response_format: json_object`（llama-server 會用文法強制輸出 JSON） |
| `llm.cloud` | 雲端（第二期），預設關閉、沒有實作；金鑰之後從環境變數讀 |
| `mood.*` | 心情衰減與互動加成 |
| `growth.auto_advance` / `thresholds` | 升階段規則：**預設關閉**（還沒決定） |
| `chatter` | 被摸時偶爾說固定台詞的機率與冷卻 |

也可以複製一份設定檔，用 `run_backend.bat --config 我的設定.json` 只覆寫要改的欄位。

## API

| 方法 | 路徑 | 說明 |
|---|---|---|
| POST | `/v1/pet/event` | 裝置回報事件，回傳指令 |
| POST | `/v1/pet/talk` | 對寵物說話，回傳 LLM（或備用）回覆 + 指令 |
| GET | `/v1/pet/<id>/state` | 存檔內容（心情、成長、短期記憶）；需金鑰且包含該 pet_id |
| GET | `/health` | 公開健康檢查：未帶金鑰只回 `{"ok": true}` |
| GET | `/v1/health` | 詳細健康檢查：需金鑰，回版本、uptime、LLM 狀態等指標 |
| POST | `/v1/pet/photo?pet_id=&reason=&taken=` | 上傳 JPEG（body 是圖本身），回 `{ok, photo_id}`；需金鑰且限制 10 張/分 |
| GET | `/v1/pet/<id>/photos[?limit=]` | 照片列表；需金鑰且包含該 pet_id |
| GET | `/v1/pet/<id>/photos/<photo_id>` | 取圖（image/jpeg）；需金鑰且包含該 pet_id |

事件範例：

```http
POST /v1/pet/event
{"pet_id":"seed-001","event":{"type":"touch","name":"pat","source":"touch-head","ts":1793175421.5},"state":"happy","stage":1}

200 {"ok":true,
     "directives":[{"type":"say","text":"再摸一下嘛","seconds":3},{"type":"emotion","emotion":"happy","seconds":2}],
     "mood":{"happiness":58.0,"affection":33.0,"energy":70.0},"growth":{"value":1.0,"stage":1}}
```

對話範例：

```http
POST /v1/pet/talk
{"pet_id":"seed-001","text":"你好"}

200 {"ok":true,"say":"你好呀～","emotion":"happy","intent":"none","source":"local","latency_ms":612.4,"attempts":1,
     "directives":[{"type":"say","text":"你好呀～","seconds":5},{"type":"emotion","emotion":"happy","seconds":3}]}
```

指令種類：`set_state`（`state`）、`set_stage`（`stage`）、`say`（`text`、`seconds`）、`emotion`（`happy/normal/surprised/sleepy/bored/sad/shy`）、`actuator`（`motion`：`nod`、`shake`、`wheels`…）。
LLM 的 `intent`（`sleep/wake/nod/shake/play`）會轉成對應指令。

## 對話流程與降級

1. 組 prompt：「種子」人設 + 目前狀態/心情 + 最近 6 輪對話（短期記憶）
2. Router 選 provider（目前固定本機；雲端只有接口）
3. 呼叫模型（逾時 `timeout_s`），去掉 `<think>…</think>`，抽出 JSON，驗證 `say/emotion/intent`
4. 失敗（連不上、HTTP 錯誤、不是合格 JSON）重試一次；如果第一次已經花了超過一半逾時時間就不重試
5. 還是失敗 → 備用台詞（`source: "fallback"`），對話永遠有回應

## 自動更新（PROPOSAL.md 16、Issue #14）

R7 後端支援拉取式自動更新（以「原地備份＋覆蓋」設計取代 junction 切換）：
- **`backend\autoupdate.py`**：
  - 以 Git smart HTTP 查詢遠端 `main` 分支最新 commit。
  - 雙重檢查 CI check-runs（`tests (Python 3.13)` 與 `secrets & personal-info scan` 必過）。
  - sparse 篩選：僅更新 `backend/` 與 `shared/`，不拉取 `frontend/` 或 `piwifi/`。
  - 依賴管理：若 `backend/requirements.txt` 或 `shared/` 變更才重新安裝依賴，絕不刪除或覆蓋 `backend\.venv`。
  - 保護機制：`config.json`、`data/`（寵物存檔）、`logs/`、`secrets/` 與非 repo 本機檔案全程保護。
  - 重啟與自檢：結束 8090 連線由 supervisor 重啟，驗證 `/health` 正常且 commit 一致；失敗自動自備份還原並記錄至 `failed.json`。
- **Windows 工作排程器（Task Scheduler）**：
  - 安裝背景定時排程（每 5 分鐘執行一次 `--once`）：
    ```powershell
    powershell -ExecutionPolicy Bypass -File backend\autostart\install_update_task.ps1
    ```
  - 移除定時排程：
    ```powershell
    powershell -ExecutionPolicy Bypass -File backend\autostart\uninstall_update_task.ps1
    ```
- **手動操作指令**：
  ```powershell
  python backend\autoupdate.py --once       # 檢查更新（受間隔保護，預設 300 秒）
  python backend\autoupdate.py --force      # 忽略間隔強制檢查與更新
  python backend\autoupdate.py --retry      # 清除失敗標記並重新嘗試更新先前失敗之 commit
  python backend\autoupdate.py --status     # 查詢目前版本與自動更新狀態
  python backend\autoupdate.py --dry-run    # 模擬測試，不更動檔案與服務
  python backend\autoupdate.py --rollback   # 手動從備份還原
  python backend\autoupdate.py --boot-check # 開機自檢
  ```

### 一次性遷移與手動回退指引

1. **初次設定與遷移至自動更新**：
   - 確保已建立後端專用虛擬環境：
     ```powershell
     powershell -ExecutionPolicy Bypass -File backend\setup_venv.ps1
     ```
   - 註冊 Windows 工作排程器定時工作（自動傳入 `--install-dir` 指定 repo 根目錄）：
     ```powershell
     powershell -ExecutionPolicy Bypass -File backend\autostart\install_update_task.ps1
     ```
   - 驗證工作排程狀態與更新器功能：
     ```powershell
     python backend\autoupdate.py --status
     python backend\autoupdate.py --self-test
     ```

2. **手動回退與復原程序**：
   - 若更新後發現問題需立即回退：
     ```powershell
     python backend\autoupdate.py --rollback
     ```
   - 回退程式會完整自備份還原舊版本檔案、移除新版引入的新檔案、保留本機資料（`data/`、`logs/`、非 repo 檔案），並重啟後端程序。
   - 檢視更新與回退歷史：
     ```powershell
     Get-Content -Tail 20 $env:LOCALAPPDATA\desktop-pet\state\update\updates.jsonl
     ```

## 測試

```bash
python3 -m unittest discover -s backend/tests -t . -v     # 用假 LLM 伺服器，不需要真的模型
```
