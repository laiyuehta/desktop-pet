"""Wi‑Fi 設定（過渡期橋接）：實作已移至 services/desktop-pet-admin/wifi.py。

此處提供相容性匯入，確保既有測試與程式碼運作正常。
"""
from __future__ import annotations

import importlib.util
import os

_ADMIN_WIFI_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "services", "desktop-pet-admin", "wifi.py"
)

if not os.path.exists(_ADMIN_WIFI_PATH):
    raise ImportError(f"無法載入前端後台 Wi-Fi 模組：找不到 {_ADMIN_WIFI_PATH}")

_spec = importlib.util.spec_from_file_location("desktop_pet_admin_wifi", _ADMIN_WIFI_PATH)
if not _spec or not _spec.loader:
    raise ImportError(f"無法載入前端後台 Wi-Fi 模組規格：{_ADMIN_WIFI_PATH}")

_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
for _k, _v in _mod.__dict__.items():
    if not _k.startswith("__"):
        globals()[_k] = _v
