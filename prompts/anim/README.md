# 動畫提示詞組合包（本機 LLM 抽卡用）

讓本機 LLM（OpenAI 相容 API，例如 LM Studio / Ollama / llama.cpp server）來寫桌寵的表情動畫，一次抽很多個變體再挑喜歡的。授權：MIT（程式碼端）。

動畫完全是 **JSON 資料**（不用寫 Python）：
- `frontend/assets/skeletons/seed.json` 的 `animations`：骨架關鍵幀
- `frontend/config/states.json`：狀態（眼睛表情、計時器、轉移）
- `frontend/config/bake.json`：烘焙變化版（眨眼／眼神、切換點）

LLM 只輸出一個「patch JSON」（`animations` / `states` / `bake_states` / `transitions` / `notes`），工具會把它合併到設定的**暫存副本**驗證、烘焙，不會改 repo 裡的檔案。

## 檔案

| 檔案 | 用途 |
|---|---|
| `system.md` | 系統提示詞：角色＋硬性規則（格式、骨頭、通道、範圍、0.05 秒對齊、不能動的東西） |
| `context.md` | 資料模型與繪圖 API 精簡規格＋真實範例（約 2k tokens） |
| `tasks/new_emotion_level.md` | 新增一個情緒／等級狀態 |
| `tasks/tweak_expression.md` | 修改既有表情動畫 |
| `tasks/new_transition.md` | 新增狀態轉移（必要時調 fade_in/out） |
| `tasks/variants.md` | 抽卡：同一提示詞叫 N 次，每次一個不同的候選 |
| `frontend/tools/prompt_pack.py` | 組提示詞、呼叫 LLM、驗證、烘焙預覽 |

## 流程

在 `frontend/` 底下執行（Windows 用 `py` 或 `python`，路徑用 `\` 也可以）。

1. **挑任務範本、填變數**（`{{KEY}}` 用 `--var KEY=值` 填；`KEY=@檔案` 讀檔；`KEY=@anim:sad_2` 自動帶入現有動畫 JSON）。
2. **組成提示詞**（手動貼到任何聊天介面用）：
   ```bash
   python tools/prompt_pack.py build --task tweak_expression --var STATE=sad_2 --var DURATION=3.2 \
     --var "CHANGE=嘆氣更明顯，觸角垂更低" --var CURRENT_JSON=@anim:sad_2 -o prompt.md
   ```
   `prompt.md` 裡 `# SYSTEM` 段放 system prompt，`# USER` 段（context + 任務）放使用者訊息。
3. **或直接叫本機 LLM 抽 N 個**（每個變體存成 `vNN.json`、原始回覆 `vNN.raw.txt`，並自動驗證）：
   ```bash
   set OPENAI_BASE_URL=http://127.0.0.1:1234/v1   # PowerShell: $env:OPENAI_BASE_URL="..."
   set OPENAI_MODEL=qwen3-14b
   python tools/prompt_pack.py gen --task variants -n 8 --bake -o ../out/happy_4 \
     --var STATE=happy_4 --var "BRIEF=開心 Lv4：原地轉圈圈後撒嬌" --var EYES=happy \
     --var DURATION=2.4 --var ROUNDS=2 --var EXTRA=
   ```
   也可用 `--base-url` / `--model` / `--api-key` / `--temperature`。`variants` 範本的 `{{SEED_HINT}}` 會自動填 #1、#2…
4. **驗證**（自己手動存的輸出也可以）：
   ```bash
   python tools/prompt_pack.py validate ../out/happy_4/v03.json --bake --out-dir ../out/happy_4/v03
   python tools/prompt_pack.py validate-all ../out/happy_4 --bake
   ```
   檢查：JSON 格式、骨頭／通道、關鍵幀 0.05 秒對齊、首尾、循環頭尾相同、數值範圍、眼睛名稱、計時器公式、cut_every、variants、轉移；然後載入骨架＋狀態機，`--bake` 會用 10 fps、無過渡烘焙小動畫包到 `<變體>/pack/`。
5. **比較**：打開每個變體的 `pack/preview/<狀態>.gif`（和 `states.png`），挑一個。
6. **採用**：把選中的 patch 內容手動合併進三個設定檔（或請 Grok Bot 合併），然後跑完整測試：
   ```bash
   python -m pytest -q tests/test_emotion_levels.py tests/test_pack.py tests/test_skeleton.py
   python -m pytest -q          # 全部
   python tools/bake.py --out ../out/full-pack   # 完整烘焙（含過渡）
   ```
   注意：`tests/test_emotion_levels.py` 會檢查狀態總數（目前 16 個／240 組過渡），新增狀態後要一起更新測試與 `docs/emotion-levels.md`。

## 小技巧

- 弱模型：`--temperature 0.6~0.9`，抽 6~10 個，通常有幾個能過驗證。不過的看錯誤訊息，可以把錯誤貼回去叫它修。
- 有 `<think>` 的模型沒關係，工具會去掉思考段、取出 JSON。
- 想要的動作越具體（秒數、幅度、哪根骨頭先動）越好抽。
