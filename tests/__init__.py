"""測試套件（#38：Pi 端程式在 frontend/，backend/ 在 repo 根目錄；unittest discover 也要找得到兩邊）。"""
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_REPO, os.path.join(_REPO, "frontend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
