# 情緒強度動畫（emotion levels）

> **狀態：只有動畫與設定，主程式還沒接上。**
> 這一批只新增了骨架動畫（`frontend/assets/skeletons/seed.json`）、狀態（`frontend/config/states.json`）與變化版設定（`frontend/config/bake.json`）。
> 目前**沒有任何事件會自動進入這些狀態**，既有狀態（idle / happy / bored / sleep）的轉移完全沒改；
> 只能用 `set_state` 遙控指令預覽，播完自動回 `idle`。

## 預覽方式

- 遙控頁 API：`POST /control {"cmd":"set_state","state":"happy_3"}`（遙控頁目前的按鈕只有既有四個狀態，新狀態要直接打 API）
- 鍵盤輸入：`state happy_3`
- 後端指令：`{"type":"set_state","state":"sad_2"}`
- 動畫包預覽：`python3 frontend/tools/bake.py` 之後看 `~/.local/share/desktop-pet/packs/seed/preview/<狀態>.gif`

## 狀態一覽

共同規則：

- 都是循環動畫（20 fps，關鍵幀對齊 0.05 秒），頭尾姿勢相同，循環不跳。
- 每個狀態都有計時器 `<狀態>_done`（`reset_on_input: false`），對應轉移 `<狀態> --<狀態>_done--> idle`。
  計時 = 動畫長度 × 輪數 − 0.1 秒，讓計時器在播放器回到第 0 格之前發出，動作做完整輪才回 idle。
- `cut_every` 設成一輪格數（只在第 0 格可以切出去），所以動作不會做到一半被切走；`wake` / `sleep`（`*` 規則）也會等這一輪播完（最多 3.2 秒）。
- 眼睛只用 `frontend/pet/eyes.py` 既有的表情（`shy` 沒有專屬眼睛，用 `squint` 加身體動作表達）。
- 變化版是眨眼／眼神（同一姿勢、不同眼睛），播放器依 weight 隨機挑。`happy` 系列眼睛是 ^ ^，眨眼看不出來，所以只用眼神變化版。

| 狀態 | 名稱 | 等級意義 | 身體動作 | 眼睛 | 一輪 | 回 idle | 變化版（weight） |
|---|---|---|---|---|---|---|---|
| `happy_1` | 微笑 | 開心 Lv1：輕微好心情 | 以地面為支點左右輕搖（±3.5°）、慢呼吸，觸角柔軟交錯擺動 | happy | 2.4 s | 4.7 s（2 輪） | base(2)、look_l(1)、look_r(1) |
| `happy_2` | 開心 | 開心 Lv2：明顯開心 | 左右交替小跳（每跳 0.6 秒、高 30px），空中往跳的方向傾、落地壓扁，觸角上下拍 | happy | 1.2 s | 3.5 s（3 輪） | base(2)、look_l(1)、look_r(1) |
| `happy_3` | 超開心 | 開心 Lv3：興奮到爆 | 蹲低蓄力 → 高跳 50px 並在空中轉一圈（spin 360°）→ 落地壓扁 → 再小彈一下；觸角起跳時豎直、落地後彈晃 | happy | 2.0 s | 3.9 s（2 輪） | base(2)、look_up(1) |
| `surprised_1` | 愣一下 | 驚訝 Lv1：小小嚇一跳 | 抽動一下（微彈 9px、觸角抖兩下）後整隻定住約 1 秒（呼吸停止），再慢慢放鬆 | surprised | 2.4 s | 2.3 s（1 輪） | base(1)、blink(2：回神眨兩下)、look_l(1) |
| `surprised_2` | 往後縮 | 驚訝 Lv2：被嚇到退縮 | 整隻縮小壓低（sy 0.84）、往旁邊退半步並微傾，觸角往後貼平，縮著微微發抖，再回來 | surprised | 2.4 s | 2.3 s（1 輪） | base(1)、blink(2)、look_r(1) |
| `surprised_3` | 嚇到彈起 | 驚訝 Lv3：大驚嚇 | 無預備動作直接彈起（拉長）、觸角豎直；落地壓扁再小彈，全身發抖約 0.6 秒後回復 | surprised | 2.4 s | 2.3 s（1 輪） | base(1)、blink(2) |
| `sad_1` | 垂眼 | 難過 Lv1：有點失落 | 身體微微下沉、呼吸變慢變淺、極小幅左右晃，觸角稍微垂下 | sad | 3.2 s | 6.3 s（2 輪） | base(1)、blink(2)、look_l(1) |
| `sad_2` | 低頭縮小 | 難過 Lv2：沮喪 | 整隻縮小壓扁（sx 0.93 / sy 0.86）、臉往下歪（spin 7°），觸角大幅垂下；中間嘆一口氣（再沉一下） | sad | 3.2 s | 6.3 s（2 輪） | base(1)、blink(2)、look_r(1) |
| `sad_3` | 哭哭 | 難過 Lv3：哭出來 | 垂頭壓扁，身體一抽一抽（每 0.4 秒抽泣一下、共 4 下再停），全身細細發抖，觸角垂下跟著抖 | sad | 2.4 s | 4.7 s（2 輪） | base(1)、blink(2) |
| `shy` | 害羞 | 害羞（單一等級） | 整隻往左側身躲開並傾斜、臉歪向一邊，扭扭捏捏小幅晃，觸角往內捲 | squint | 3.2 s | 6.3 s（2 輪） | base(1)、away(2：眼神往下飄開)、peek(1：偷看一下又躲開) |
| `annoyed_1` | 皺眉 | 不爽 Lv1 | 身體微微鼓起，觸角繃直往內收（像皺眉），中間「哼」一聲把臉撇開、停一下再轉回來 | squint | 2.4 s | 4.7 s（2 輪） | base(1)、blink(1)、side_eye(2：斜眼) |
| `annoyed_2` | 生氣 | 不爽 Lv2：生氣 | 氣到鼓起來（一脹一脹，sx 1.1）、左右猛抖、原地跺兩下，觸角豎直抖動 | squint | 1.6 s | 4.7 s（3 輪） | base(2)、glare(1：往上瞪) |

既有的 `happy`（0.7 秒原地彈跳）完全沒改，跟 `happy_2`（左右交替跳）是不同動作。

## 做不到／沒做的

- **特效**：渲染器目前只有 `zzz` 一種特效，沒有閃光／星星／眼淚。`happy_3` 的閃光、`sad_3` 的眼淚都沒有，只靠身體動作表達。要加的話得改 `frontend/pet/renderer.py`（主程式），不在這一批範圍。
- **害羞專屬眼睛／腮紅**：`frontend/pet/eyes.py` 沒有 `shy` 表情，用 `squint`。
- **動畫包大小**：烘焙工具會為「每一對狀態」產生過渡片段（不管狀態機有沒有這條轉移），新增 12 個狀態後 `packs/seed` 從 77.2 MB 變成 246.9 MB（過渡片段 189 → 1125 段）。新狀態已經把 `cut_every` 設成一輪，只留一個切換點，但既有狀態切進新狀態的過渡還是很多。
  之後可考慮改 `frontend/pet/baker.py`：只為狀態機實際會走的轉移（含 set_state 常用的目標）產生過渡片段，其餘直接從第 0 格開始播。

## 之後主程式要做的事（尚未實作）

以下都**還沒做**，列出來給之後接主程式時參考。

1. **觸發事件**（`frontend/config/states.json` 加轉移，或由主程式直接 `force`）：
   - 建議事件名稱：`emotion_happy`、`emotion_surprised`、`emotion_sad`、`emotion_shy`、`emotion_annoyed`，事件帶 `level`（1–3）。
   - 狀態機目前的轉移表只看 (狀態, 事件名稱)，不看參數；有兩種做法：
     - a. 事件名稱直接帶等級：`emotion_happy_1` … `emotion_annoyed_2`，在 `transitions` 加 `{"from": "idle", "event": "emotion_happy_2", "to": "happy_2"}` 之類（最簡單，不改程式）。
     - b. 主程式（`frontend/pet/app.py`）收到 `emotion` 事件時依 `level` 算出狀態名再 `fsm.force()`。
   - 從哪些狀態可以進入要決定：例如 `sleep` 時只接受 `surprised_*`（被吵醒），`bored` 可以接 `happy_*`。
2. **等級升級（escalation）**：
   - 在同一情緒狀態裡又收到同類事件 → 升一級（`happy_1 --emotion_happy--> happy_2 --emotion_happy--> happy_3`），最高到 3（annoyed 到 2）。
   - 例如：摸頭一次 `happy_1`、短時間內連續摸 `happy_2`、摸很多下或聽到喜歡的話 `happy_3`；戳一下 `surprised_1`、用力戳／突然大聲 `surprised_3`；一直戳 → `annoyed_1` → `annoyed_2`。
   - 升級時重設計時器（狀態機 `_enter` 會自動重設）；沒有新事件就照計時器回 idle（之後也可以改成「降一級」而不是直接回 idle，例如 `happy_3_done → happy_1`）。
   - 可以搭配後端 `backend/mood.py` 的心情值決定起始等級（心情好時同樣摸頭直接 `happy_2`）。
3. **後端情緒對應**：`backend/llm.py` 的 `EMOTIONS` 有 `shy`，但 `frontend/pet/eyes.py` 沒有 `shy` 表情；之後可以讓後端的 `emotion` 指令帶強度（例如 `{"type":"emotion","emotion":"sad","level":2}`），主程式改成切到對應的 `sad_2` 狀態，而不是只換眼睛。
4. **遙控頁**：`frontend/pet/web/remote.html` 的狀態按鈕是寫死的，要加新狀態的按鈕（或改成從 `/status` 讀狀態清單）。
5. **on_enter 動作**：既有 `happy` 進入時會 `motion nod`；新狀態都還沒有 `on_enter`（例如 `happy_3` 可配 nod×2、`annoyed_2` 配 shake），之後依實體驅動器決定。
