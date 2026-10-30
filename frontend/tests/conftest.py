"""前端測試共用設定：不讀開發者本機的 backend/config.json 或環境檔（裡面可能有真的 token），只用預設範本。"""
import os
import sys

os.environ["PET_BACKEND_NO_LOCAL_CONFIG"] = "1"
for _k in list(os.environ):
    if _k.startswith("PET_") and _k != "PET_BACKEND_NO_LOCAL_CONFIG" and not _k.startswith("PET_TEST_"):
        os.environ.pop(_k, None)

_FRONTEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(_FRONTEND)
# frontend/（pet、main、services）；shared/（沒 pip install -e 時也找得到）；repo 根目錄與 backend/tests（整合測試用真的 backend 與假 LLM）
for _p in (os.path.join(_REPO, "backend", "tests"), _REPO, os.path.join(_REPO, "shared"), _FRONTEND):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
