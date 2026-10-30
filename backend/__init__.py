"""桌寵後端（「大腦」）：成長、心情、存檔、對話（本機 LLM），只傳狀態與指令給裝置。

只用 Python 標準函式庫，CPython 3.10 以上（Windows / Linux 都可以）。
啟動：python -m backend（設定：backend/config.example.json ＋ 本機 backend/config.json）
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
    shared_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
    if os.path.isfile(os.path.join(shared_dir, "shared", "__init__.py")) and shared_dir not in sys.path:
        sys.path.insert(0, shared_dir)


_ensure_shared_importable()
