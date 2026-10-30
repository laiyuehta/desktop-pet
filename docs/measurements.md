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

驗證輸出：`全部幀都是完整 JPEG`。數字與 PR #42 所列（#18 時的 1,263 段／246.9 MB）一致；仍遠大於 Pi 上舊包的 77.2 MB：`frames.bin` 246.9 MB 約 3.2 倍，整個目錄 255.4 MB 約 3.3 倍。

### 測試

- `python3 -m pytest -q tests/test_backend.py tests/test_pack.py tests/test_emotion_levels.py tests/test_backend_client.py` 連跑 4 次：每次 `83 passed, 89 subtests passed`（約 24 秒），無偶發失敗。`test_hanging_backend_does_not_block_loop` 包含在內。
- 全套 `python3 -m pytest -q`：`223 passed, 105 subtests passed in 59.76s`。

### Pi 3B

2026-10-30 約 10:25–10:43（台北時間；兩輪指定測試約 10:25–10:36，全套約 10:37–10:43），Raspberry Pi 3B（aarch64，Python 3.13.5），在暫存資料夾 clone main `fa540d4` 執行，沒有動到正在跑的服務。

| 項目 | 結果 |
|---|---|
| `test_backend`、`test_pack`、`test_emotion_levels`、`test_backend_client`（含 `test_hanging_backend_does_not_block_loop`），連跑 2 輪 | 兩輪都是 83 個測試 OK（skipped=1），各約 309.6 秒 / 309.4 秒，沒有偶發失敗 |
| 全套 `python3 -m unittest discover -s tests` | 141 個測試，315.0 秒；134 個通過、skipped=1，另有 7 個測試模組（`test_admin`、`test_camera`、`test_control_api`、`test_frames`、`test_mirror`、`test_picam`、`test_remote_page`）因為 Pi 沒裝 `pytest` 無法匯入（`ModuleNotFoundError: No module named 'pytest'`），不是測試本身失敗 |

### Pi 換包（#31）

同日在 R7（Windows，Python 3.12.10、Pillow 13.0.0）用 main `fa540d4` 重新 bake，耗時 93 秒，`frames.bin` 246,913,525 bytes，sha256 `4f02321f63ea12c8a3e010f62e856ff0b313d1135306898df2cfe1a0d6c05805`，與上方本機烘焙完全相同。scp 到 Pi 後比對雜湊一致。
舊的 `~/desktop-pet/packs` 先備份成 `~/packs-backup-20261030.tar`（77 MB）再替換，10:18 重啟 desktop-pet 後日誌：`1263 段片段、21638 格、246.9 MB、20 fps`，fps 穩定 20.0，沒有缺少狀態的警告。
