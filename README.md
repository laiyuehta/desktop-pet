# desktop-pet 桌寵專案

放在桌上的實體電子寵物：圓球種子形態的小機器人，會隨成長長出腳、耳朵。第一期只做電子寵物，第二期才加入幫忙做事的能力（目前只預留接口）。

- 項目書：[PROPOSAL.md](PROPOSAL.md)
- 參考圖（只給美術參考，不是實際素材）：`assets/reference/`
- 原型平台：樹莓派 3B + SmartCool 5 吋 USB 螢幕（透過 piscreen 服務顯示）

![示範動畫](preview/demo.gif)

## 目前進度（第一版程式原型 v0.1）

| 項目 | 狀態 |
|---|---|
| 自訂 2D 骨架格式 + runtime（關鍵幀、linear/ease/step、切換淡入） | ✅ |
| 種子形態零件（程式產生）＋ idle／happy／bored／sleep 動畫 | ✅ |
| 燈眼／嘴巴／額頭菱形程式繪製（發光、眨眼、瞇眼笑、半睜、閉眼睡、看左右） | ✅ |
| 事件驅動狀態機（JSON 對應表、閒置計時、晚上／早上時鐘事件） | ✅ |
| 成長值／階段資料結構與 `set_stage()` 換骨架接口 | ✅（推進規則未做；只有種子骨架） |
| 輸入轉接器：鍵盤模擬、網頁遙控／HTTP 模擬；觸控／按鈕／視覺 stub | ✅ |
| 外部控制器接口（輪子、雲台）：LoggingActuator / NullActuator stub | ✅（沒有真的驅動） |
| 擴充模組接口（事件匯流排、模組註冊、動作請求）＋空範例 | ✅（第二期才做功能） |
| 輸出：piscreen（Unix socket + JPEG）、預覽 PNG / GIF | ✅（piscreen 尚未在 Pi 實測） |

## 安裝

```bash
# 電腦或 Pi 都只需要 Pillow
pip install -r requirements.txt        # 或 Pi 上：sudo apt install python3-pil
```

## 在電腦上預覽

```bash
# 1) 離線算出示範動畫（不需要螢幕）：preview/demo.gif + idle/happy/sleep.png
python3 tools/render_demo.py

# 2) 即時跑：畫面寫到 preview/live/latest.png（用會自動重新載入的圖片檢視器開著）
#    同時開鍵盤模擬器與網頁遙控
python3 main.py --sink preview --input keyboard,web --web 127.0.0.1:8080 --idle-timeout 10
```

鍵盤模擬器（在終端機輸入後按 Enter）：`p` 摸頭、`o` 戳、`b` 按鈕、`v` 有人靠近、`w` 叫醒、`s` 睡覺、`1`/`2`/`3` 成長階段、`state sleep` 強制切狀態、`n` 點頭、`h` 搖頭、`q` 結束。

## 在樹莓派上跑（接 piscreen）

前提：Pi 上的 `piscreen.service` 已在跑（`/run/piscreen/piscreen.sock` 存在）。桌寵程式**不直接碰 USB**，也不會重啟 piscreen。

```bash
cd desktop-pet
python3 tools/bench.py            # 先量這台 Pi 的繪圖 / JPEG fps
python3 main.py --sink piscreen --input web --web 0.0.0.0:8080 --fps 20
```

- 手機或電腦開 `http://192.0.2.43:8080/` 就是網頁遙控。
- 程式每 5 秒在 log 印出實際 fps；piscreen 連不上時會自動重試並丟格，不會當掉。
- 常用參數：`--fps`（預設 20）、`--jpeg-quality`（預設 85）、`--socket`、`--idle-timeout`、`--duration`、`-v`。

## 網頁遙控（內部控制器）

`--input web` 時由 `main.py` 一起開（Python 標準函式庫 `http.server`，不用另外裝東西）。手機友善的單頁，繁體中文介面：

- **目前狀態**：狀態、成長階段、成長值、表情、fps、最後事件（每秒輪詢 `GET /status`）
- **互動事件**：摸頭、戳一下、按按鈕、有人靠近、叫醒、去睡覺、模擬閒置、模擬晚上
- **強制切換狀態**：待機／開心／無聊／睡覺
- **成長階段**：1 種子／2 長腳／3 長耳朵（目前只有種子骨架，其他階段沿用種子外型）
- **外部控制器測試**：雲台點頭、搖頭、回正；輪子前進、後退、左轉、右轉、停止（目前接 LoggingActuator，只顯示紀錄）

HTTP API（也可以當事件模擬器用）：

```bash
curl -X POST localhost:8080/event    -d '{"type":"touch","name":"pat"}'
curl -X POST localhost:8080/control  -d '{"cmd":"set_state","state":"sleep"}'
curl -X POST localhost:8080/control  -d '{"cmd":"set_stage","stage":2}'
curl -X POST localhost:8080/actuator -d '{"motion":"nod","times":2}'
curl -X POST localhost:8080/actuator -d '{"motion":"wheels","action":"forward","speed":0.5,"duration":1}'
curl localhost:8080/status
```

> ⚠️ 網頁遙控沒有密碼，只適合在家裡內網用；不要把埠號開到外網。

## 專案結構

```
main.py                    主程式（主迴圈、選輸出端與輸入）
config/states.json         狀態機設定：狀態 → 動畫/表情/計時，(狀態, 事件) → 下一狀態，成長階段 → 骨架
assets/skeletons/seed.json 種子骨架 + 動畫
assets/parts/seed/*.png    種子零件圖（由 tools/gen_seed_parts.py 產生）
pet/skeleton.py            骨架格式、插值、動畫播放器、零件合成（含快取）
pet/eyes.py                EyeController（表情、眨眼、眼神）+ FaceRenderer（燈眼/嘴/菱形）
pet/renderer.py            背景、陰影、零件、臉、Z 特效 → 800×480 RGB
pet/state_machine.py       StateMachine、Growth、ClockEvents
pet/events.py              標準事件格式
pet/app.py                 PetApp：事件路由、動作請求（ActionRouter）、換骨架、狀態輸出
pet/inputs/                InputAdapter、KeyboardInput、WebRemoteInput、Touch/Button/Vision stub
pet/web/remote.html        網頁遙控頁面
pet/outputs/               OutputSink、PiscreenSink、FileSink、GifSink、NullSink
pet/actuators/             ActuatorController、Wheels、PanTilt、ActuatorHub、LoggingActuator、NullActuator
pet/extensions/            EventBus、Extension、ExtensionManager、ExampleExtension
tools/gen_seed_parts.py    產生零件圖
tools/render_demo.py       產生預覽 GIF / PNG
tools/bench.py             量 fps
tests/                     unittest
```

## 骨架格式

```json
{
  "name": "seed",
  "canvas": [800, 480],
  "bones": [
    {"name": "root", "parent": null, "pivot": [400, 440]},
    {"name": "body", "parent": "root", "pivot": [0, -158], "z": 10,
     "part": {"image": "../parts/seed/body.png", "anchor": [156, 156]}},
    {"name": "face", "parent": "body", "pivot": [0, 0]},
    {"name": "antenna_l", "parent": "body", "pivot": [-34, -136], "rest": {"rot": -14}, "z": 5,
     "part": {"image": "../parts/seed/antenna_l.png", "anchor": [22, 112]}}
  ],
  "face": {"bone": "face", "eye_radius": 44, "eyes": [[-66, 10], [66, 10]], "diamond": [0, -60], "mouth": [0, 72]},
  "animations": {
    "idle": {"duration": 3.2, "loop": true, "tracks": {
      "body":      {"y":   [[0, 0, "ease"], [1.6, -7, "ease"], [3.2, 0]]},
      "antenna_l": {"rot": [[0, 0, "ease"], [0.8, 6, "ease"], [2.4, -5, "ease"], [3.2, 0]]}
    }}
  }
}
```

- 骨頭依序定義，父骨頭要先出現；`pivot` 是骨頭原點在父骨頭座標中的位置，根骨頭就是螢幕座標。
- `rest`：靜止旋轉（度）與縮放；`z`：繪製順序（小的先畫）。
- `part.anchor`：零件圖中對齊骨頭原點的像素。
- 動畫通道：`x`、`y`、`rot`（加在靜止值上）、`sx`、`sy`（乘在靜止值上）。
- 關鍵幀 `[時間, 數值, 緩動]`，緩動是這一幀到下一幀的插值：`linear`（預設）、`ease`、`step`。
- 切換動畫時自動 0.25 秒交叉淡入。
- 眼睛、嘴巴不是貼圖：`face` 只給位置，由 `pet/eyes.py` 依表情畫出來。
- 新的成長階段只要做新的骨架 JSON + 零件圖，動畫名稱照用 idle/happy/bored/sleep，狀態機不用改。

## 狀態機設定

`config/states.json`：

```json
"idle":  {"animation": "idle", "eyes": "normal",
          "timers": [{"after": 600, "event": "idle_timeout", "reset_on_input": true}]},
"happy": {"animation": "happy", "eyes": "happy",
          "timers": [{"after": 3.0, "event": "happy_done"}],
          "on_enter": [{"name": "motion", "params": {"motion": "nod", "times": 1}}]},
...
"transitions": [
  {"from": "idle",  "event": "pat",          "to": "happy"},
  {"from": "idle",  "event": "idle_timeout", "to": "bored"},
  {"from": "bored", "event": "idle_timeout", "to": "sleep"},
  {"from": "sleep", "event": "pat",          "to": "idle"},
  {"from": "*",     "event": "wake",         "to": "idle"}
]
```

- 互動事件（touch/button/vision/sim）會重置 `reset_on_input` 的計時。
- `on_enter` 是進入狀態時發出的動作請求，例如開心時請外部控制器點一下頭。
- 23:00～07:00 跨越時發 `night` / `morning` 事件（`clock` 設定）。

## 測試

```bash
python3 -m unittest discover -s tests -v     # 不需要額外套件（pytest 也能跑）
```

## 效能

`python3 tools/bench.py` 量純繪圖與「繪圖 + JPEG 編碼」的 fps。開發機（Intel Xeon 雲端主機，Python 3.13、Pillow 13）上約 600 fps（純繪圖）／ 410 fps（含 JPEG，每格約 18 KB）。**Pi 3B 還沒量**，要到 Pi 上跑同一支程式才知道。
