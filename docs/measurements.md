# 實測紀錄

## 2026-10-30：#25 修正後重新量測（#45、#31）

- 日期：2026-10-30 09:5x（Asia/Taipei）
- 量測 commit：`2b801a7`（main，PR #42 squash）
- 機器：Linux 開發機（box），8 核 Intel Xeon、15 GB RAM、Python 3.13.5

### 烘焙 `packs/seed`（`python3 tools/bake.py`）

同一次烘焙同時作為 #31（Pi 動畫包重烘）與 #45 的數據。

| 項目 | 實測 |
|---|---:|
| 總片段數 | 1,263 段 |
| 過渡片段數 | 1,125 段 |
| 狀態／表情片段數 | 138 段 |
| 不重複幀數（引用） | 15,143 格（21,638 格） |
| `frames.bin` | 246,913,525 bytes（工具顯示 246.9 MB） |
| `manifest.json` | 884,308 bytes |
| `packs/seed` 整個目錄（含 preview） | 255,436,767 bytes |
| 烘焙耗時 | 130.6 秒（工具輸出） |

驗證輸出：`全部幀都是完整 JPEG`。數字與 PR #42 所列（#18 時的 1,263 段／246.9 MB）一致；目錄仍遠大於 Pi 上舊包的 77.2 MB（約 3.2 倍）。

### 測試

- `python3 -m pytest -q tests/test_backend.py tests/test_pack.py tests/test_emotion_levels.py tests/test_backend_client.py` 連跑 4 次：每次 `83 passed, 89 subtests passed`（約 24 秒），無偶發失敗。`test_hanging_backend_does_not_block_loop` 包含在內。
- 全套 `python3 -m pytest -q`：`223 passed, 105 subtests passed in 59.76s`。

### Pi 3B

尚未量測：本次量測機器不在 Pi 所在的區網，連不到 Pi 的 ssh port，也沒有可登入 Pi 的金鑰。需要由能 ssh 進 Pi 的機器（桌寵專案機器人1）補跑上述測試，並把新烘的包換上（先備份舊包）。
