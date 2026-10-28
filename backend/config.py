"""設定檔載入：config.example.json（內建預設，進 repo）＋ config.json（本機設定，含 api_token，不進 repo）深度合併。

設定檔缺的欄位用預設。本機 config.json 可以只寫要改的欄位，也可以是完整的舊版 config.json。"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "config.example.json")
LOCAL_PATH = os.path.join(HERE, "config.json")  # 本機設定（.gitignore），有就疊上去


def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """讀內建預設（backend/config.example.json），疊上本機 backend/config.json（有的話），
    再疊上指定檔案與 overrides。只讀不寫，不會建立、覆蓋或刪除任何設定檔。"""
    with open(DEFAULT_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if os.path.isfile(LOCAL_PATH) and os.environ.get("PET_BACKEND_NO_LOCAL_CONFIG") != "1":
        with open(LOCAL_PATH, "r", encoding="utf-8") as fh:
            cfg = deep_merge(cfg, json.load(fh))
    if path and os.path.abspath(path) not in (DEFAULT_PATH, LOCAL_PATH):
        with open(path, "r", encoding="utf-8") as fh:
            cfg = deep_merge(cfg, json.load(fh))
    if overrides:
        cfg = deep_merge(cfg, overrides)
    # data_dir 相對路徑以 backend/ 為基準，不受目前工作目錄影響
    if not os.path.isabs(cfg["data_dir"]):
        cfg["data_dir"] = os.path.join(HERE, cfg["data_dir"])
    return cfg
