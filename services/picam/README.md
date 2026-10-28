# picam — Pi 相機常駐服務

照 PROPOSAL.md「週邊常駐服務原則」：**只有 picam 打開相機、一直占著**，其他程式（桌寵、camcast、工具）都當 client，透過 Unix socket 要照片或預覽，不直接碰相機，也不會互搶。

- `picamd.py`：常駐 daemon（systemd `picam.service`，開機啟動、`Restart=always`，以 pi-user 身分、video 群組執行，不需要 root）。
- `picam_client.py`：client 函式庫（只用標準函式庫）。桌寵的 `camera.snapshot` 模組與 camcast 都用它。
- `install.sh`：在 Pi 上安裝／更新（複製到 `/home/pi-user/picam/`、裝 systemd unit、enable、restart）。
- 需要 `python3-picamera2`：`sudo apt install --no-install-recommends python3-picamera2`。

## 為什麼用 Picamera2
Picamera2 讓一個程式長期持有相機，並在同一個程式裡切換「閒置串流／預覽／拍照」三種設定；預覽用 Pi 的硬體 MJPEG 編碼器（沒有就自動改軟體 JPEG）。
用 `rpicam-*` 指令的話，拍照和預覽要不同的程式，每次切換都要放開再重開相機（等於又回到搶相機），也拿不到「同一個程式獨占」的效果。

## 行為
- **閒置**：相機用 640×480、5 fps 低速串流（`--idle-fps`，0 = 停住但仍占著）。曝光與白平衡一直保持收斂，拍照不用等 1 秒暖機。
- **拍照合併**：第一個請求到了以後等 0.5 秒（`--window`），這段時間內到的請求合併成**同一次拍照、同一個 `photo_id`**。
  拍照中又到的請求，如果尺寸不超過這次拍的尺寸，就掛在這一張上。太大的排到下一張。
- **解析度**：用這批請求裡最大的寬和高去拍；較小的請求用同一張照片縮圖給（長寬比不同時置中裁切）。
- **頻率上限**：每秒最多 2 次實際拍照（`--max-rate`），超過的請求合併到下一張。每個請求都有回應。
- **預覽中拍照**：停掉預覽編碼 → 切到 still 設定拍一張 → 切回預覽（停頓約 1 秒），訂閱者不用處理。
- **多個預覽訂閱者**：用所有訂閱者裡最大的尺寸和 fps 跑一個預覽，每個訂閱者照自己的 fps 拿（多的格跳過）。
  慢的 client 只拿最新一張。
- **錯誤**：相機打不開或拍照失敗 → 回 `camera_error`，關掉相機重開（退避 2～60 秒）。預覽超過 6 秒沒畫面也會重開。
  picam 本身掛掉由 systemd 3 秒後重啟。client 斷線會收到錯誤，`Subscriber` 會自動重連。
- 照片內容不寫日誌，只記 photo_id、尺寸、請求數、client 名稱。

## Socket 協定
`/run/picam/picam.sock`（pi-user:pi-user 0660）。每個請求是一行 JSON，以 `\n` 結尾。回應先是一行 JSON，有圖片時後面接 `[4 byte big-endian 長度][JPEG]`。同一條連線可以送很多個 snapshot／status；subscribe 之後這條連線只用來送預覽。

| 請求 | 回應 |
|---|---|
| `{"cmd":"snapshot","width":1280,"height":960,"quality":85,"client":"desktop-pet","reason":"manual"}` | `{"ok":true,"photo_id":"20261028-213000-123","ts":1793193000.123,"size":110000,"width":1280,"height":960,"quality":85,"capture":[1280,960],"shared":3}` + 圖片 |
| `{"cmd":"subscribe","fps":15,"width":800,"height":480,"client":"camcast"}` | `{"ok":true,"width":800,"height":480,"fps":15}`，然後一直送 `[長度][JPEG]` 直到斷線 |
| `{"cmd":"status"}` | 相機型號、模式、sensor 模式、client 數、拍照次數、合併數、錯誤數、最後一張（同 status.json） |

錯誤：`{"ok":false,"kind":"bad_request|camera_error|timeout","error":"中文說明"}`（沒有圖片）。socket 不存在或連不上時，client 會丟 `PicamError("service_down", "相機服務未啟動")`。

參數範圍：snapshot 寬 64～2592、高 48～1944、品質 1～100；subscribe fps 1～30、最大 1296×972。
`photo_id` = 拍攝時間 `年月日-時分秒-毫秒`（毫秒四捨五入，跟桌寵、後端同一套算法）。`shared` = 這張分給了幾個請求。

## 用法
```sh
cat /run/picam/status.json                         # 狀態
journalctl -u picam -f                             # 日誌
python3 -c "import sys; sys.path.insert(0,'/home/pi-user/picam'); import picam_client as p; m,d=p.snapshot(); open('/tmp/a.jpg','wb').write(d); print(m)"
```
Python：
```python
from picam_client import snapshot, status, Subscriber, PicamError
meta, jpeg = snapshot(1280, 960, 85, client="me", reason="manual")
for jpeg in Subscriber(fps=15, width=800, height=480, client="me").frames():   # 自動重連
    ...
```
