"""desktop-pet 前端（Pi）與後端（R7）共用的模組（#38）。

只放兩邊都會用到、而且必須一致的東西，只用 Python 標準函式庫：
- protocol：HTTP API 路徑、金鑰標頭、情緒／意圖清單
- apikey：API 金鑰格式（產生、雜湊、讀金鑰檔）
- envfile：KEY=VALUE 環境檔格式（PROPOSAL.md 16.2）
"""
__all__ = ["protocol", "apikey", "envfile"]
