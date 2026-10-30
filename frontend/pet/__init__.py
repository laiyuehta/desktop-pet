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


def _ensure_shared_importable() -> None:
    """沒裝 shared 套件（venv 還沒建好、或直接用系統 python）時，退回 repo 內的 shared/（#38）。

    shared/ 跟著 repo 一起部署；這樣舊 unit 或 install.sh 還沒跑完時也不會因 import 失敗而 crash loop。
    """
    import importlib.util
    import os
    import sys

    spec = importlib.util.find_spec("shared")
    if spec is not None and spec.origin:  # 已裝好的正式套件（repo 根目錄的 shared/ 資料夾只會被當成 namespace，不算）
        return
    shared_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "shared")
    if os.path.isfile(os.path.join(shared_dir, "shared", "__init__.py")) and shared_dir not in sys.path:
        sys.path.insert(0, shared_dir)


_ensure_shared_importable()
