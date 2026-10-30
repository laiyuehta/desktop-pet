"""後端測試共用設定：不讀開發者本機的 backend/config.json 或環境檔（裡面可能有真的 token），只用預設範本。"""
import os
import sys

os.environ["PET_BACKEND_NO_LOCAL_CONFIG"] = "1"
for _k in list(os.environ):
    if _k.startswith("PET_") and _k != "PET_BACKEND_NO_LOCAL_CONFIG" and not _k.startswith("PET_TEST_"):
        os.environ.pop(_k, None)

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (os.path.join(_REPO, "backend", "tests"), os.path.join(_REPO, "shared"), _REPO):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
