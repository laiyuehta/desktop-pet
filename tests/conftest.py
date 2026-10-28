"""測試共用設定：不讀開發者本機的 backend/config.json（裡面可能有真的 token），只用 config.example.json。"""
import os

os.environ["PET_BACKEND_NO_LOCAL_CONFIG"] = "1"
os.environ.pop("PET_BACKEND", None)
os.environ.pop("PET_BACKEND_TOKEN_FILE", None)
