"""測試共用設定：不讀開發者本機的 backend/config.json 或環境檔（裡面可能有真的 token），只用預設範本。"""
import os

os.environ["PET_BACKEND_NO_LOCAL_CONFIG"] = "1"
for _k in list(os.environ):
    if _k.startswith("PET_") and _k != "PET_BACKEND_NO_LOCAL_CONFIG" and not _k.startswith("PET_TEST_"):
        os.environ.pop(_k, None)
