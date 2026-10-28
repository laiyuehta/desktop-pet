"""設定檔載入：config.json 與內建預設值深度合併（設定檔缺的欄位用預設）。"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "config.json")


def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: Optional[str] = None, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """讀內建預設（backend/config.json），再疊上指定檔案與 overrides。"""
    with open(DEFAULT_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if path and os.path.abspath(path) != DEFAULT_PATH:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = deep_merge(cfg, json.load(fh))
    if overrides:
        cfg = deep_merge(cfg, overrides)
    # data_dir 相對路徑以 backend/ 為基準，不受目前工作目錄影響
    if not os.path.isabs(cfg["data_dir"]):
        cfg["data_dir"] = os.path.join(HERE, cfg["data_dir"])
    return cfg
