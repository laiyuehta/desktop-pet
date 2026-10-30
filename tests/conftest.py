"""測試共用設定：不讀開發者本機的 backend/config.json 或環境檔（裡面可能有真的 token），只用預設範本。"""
import os
import sys

# #38：Pi 端程式（main.py、pet/、services/）搬到 frontend/，backend/ 留在 repo 根目錄；兩個都放進 sys.path
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_REPO, os.path.join(_REPO, "frontend"), os.path.join(_REPO, "shared")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ["PET_BACKEND_NO_LOCAL_CONFIG"] = "1"
for _k in list(os.environ):
    if _k.startswith("PET_") and _k != "PET_BACKEND_NO_LOCAL_CONFIG" and not _k.startswith("PET_TEST_"):
        os.environ.pop(_k, None)
