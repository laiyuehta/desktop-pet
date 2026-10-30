"""前端（Pi）↔ 後端（R7）HTTP API 的協定常數（PROPOSAL.md「前後端連線安全：API 金鑰」）。"""

#: 每個 /v1/* 請求都要帶的金鑰標頭
TOKEN_HEADER = "X-Pet-Token"

DEFAULT_BACKEND_PORT = 8090

#: 公開健康檢查（不帶金鑰只回 {"ok": true}）
PATH_HEALTH = "/health"
#: 詳細健康檢查（需金鑰）
PATH_HEALTH_V1 = "/v1/health"
PATH_EVENT = "/v1/pet/event"
PATH_TALK = "/v1/pet/talk"
PATH_PHOTO = "/v1/pet/photo"

#: 後端回覆的情緒（裝置端再對應到自己的表情）
EMOTIONS = ["happy", "normal", "surprised", "sleepy", "bored", "sad", "shy"]
#: 後端回覆的意圖
INTENTS = ["none", "sleep", "wake", "nod", "shake", "play"]
