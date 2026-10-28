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
3. 先開 llama-server（你已經會開，監聽 `http://127.0.0.1:8080`，模型別名 `qwen3-14b`）。
4. 啟動後端：
   ```powershell
   backend\run_backend.bat
   ```
   或直接 `py -3 -m backend`。看到「桌寵後端 v0.1.0 啟動：http://0.0.0.0:8090/health」就好了，Ctrl+C 結束。
5. 在 R7 本機測試：
   ```powershell
   curl.exe http://127.0.0.1:8090/health
   $body = [Text.Encoding]::UTF8.GetBytes('{"pet_id":"seed-001","text":"你好"}')
   Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8090/v1/pet/talk -ContentType 'application/json; charset=utf-8' -Body $body
   ```
   `/health` 裡 `llm.local.reachable` 是 `true` 代表連得到 llama-server；`/talk` 回覆的 `source` 是 `local` 代表是模型回的，`fallback` 代表用了備用台詞。

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
python3 main.py --sink piscreen --input web --backend http://192.0.2.100:8090
```

網頁遙控（`http://192.0.2.43:8080/`）多了「跟寵物說話」輸入框；寵物說的話會以對話框顯示在螢幕上。
對話框需要中文字型：`sudo apt install fonts-wqy-microhei`（小）或 `fonts-noto-cjk`，也可以用 `--font 路徑` 指定。

## 設定（backend/config.json）

| 欄位 | 說明 |
|---|---|
| `server.host` / `server.port` | 監聽位址，預設 `0.0.0.0:8090` |
| `server.api_token` | 留空不檢查；設定後裝置要加 `--backend-token` |
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
| GET | `/v1/pet/<id>/state` | 存檔內容（心情、成長、短期記憶） |
| GET | `/health` | 服務與 LLM 狀態 |

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

## 測試

```bash
python3 -m unittest discover -s tests -v     # 用假 LLM 伺服器，不需要真的模型
```
