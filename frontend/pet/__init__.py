"""桌寵（desktop-pet）第一期原型套件。

模組分層（對應 PROPOSAL.md 第 5 章）：
- skeleton / eyes / renderer：表現層（骨架動畫 + 燈眼繪製）
- state_machine：事件驅動狀態機 + 成長資料結構
- inputs：輸入轉接器（鍵盤、網頁遙控、觸控／按鈕／視覺 stub）
- outputs：輸出端（piscreen、預覽檔案）
- extensions：能力擴充接口（第二期用，只有空範例）
- actuators：外部控制器接口（輪子、雲台，只有 stub）
"""

__version__ = "0.1.0"
