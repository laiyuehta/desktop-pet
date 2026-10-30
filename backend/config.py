"""設定檔載入（PROPOSAL.md 16.2 四層優先順序）：

優先順序（高 → 低）：
1. 命令列參數（`--config` 指定檔與 `overrides` 字典，以及 `backend/__main__.py` 的 `--host`、`--port`）
2. 環境變數（程序環境變數 `os.environ` > R7 環境檔 `PET_ENV_FILE` 或 `%LOCALAPPDATA%\\desktop-pet\\env`）
3. 本機設定檔（`PET_BACKEND_CONFIG` 指定的 `config.json`，沒設則讀 `backend/config.json`）
4. `backend/config.example.json`（repo 裡的預設範本）

設定檔缺的欄位用預設。只讀不寫，不會建立、覆蓋或刪除任何設定檔。"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from typing import Any, Dict, Mapping, Optional

from shared.apikey import read_key_file
from shared.envfile import parse_env_file as _shared_parse_env_file

log = logging.getLogger("backend.config")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "config.example.json")
LOCAL_PATH = os.path.join(HERE, "config.json")  # 本機設定（.gitignore），有就疊上去


def _expand_r7_vars(val: str, environ: Optional[Mapping[str, str]] = None) -> str:
    """展開 %VAR%（跨平台支援，確保在 Linux CI 與 Windows R7 皆能展開 %LOCALAPPDATA% 等變數）。"""
    env_map = os.environ if environ is None else environ

    def _repl(m: re.Match) -> str:
        name = m.group(1)
        return env_map.get(name) or os.environ.get(name) or m.group(0)

    out = re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", _repl, val)
    return os.path.expandvars(out)


def default_r7_env_path() -> str:
    local_app_data = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    return os.path.join(local_app_data, "desktop-pet", "env")


def parse_env_file(
    path: str,
    expand_vars: bool = True,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """讀取 KEY=VALUE 環境檔；值留空視為未設。R7 環境檔支援展開 %LOCALAPPDATA% 等變數。"""
    expand = (lambda v: _expand_r7_vars(v, environ=environ)) if expand_vars else None
    return _shared_parse_env_file(path, expand)


def load_r7_env(
    env_file: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """載入 R7 環境變數（程序環境變數 > 環境檔）。"""
    proc_env = dict(os.environ if environ is None else environ)
    clean_proc = {k: _expand_r7_vars(v, environ=proc_env) for k, v in proc_env.items() if v != ""}
    no_local = clean_proc.get("PET_BACKEND_NO_LOCAL_CONFIG") == "1"

    chosen_file = env_file or clean_proc.get("PET_ENV_FILE")
    if not chosen_file and not no_local:
        default_env = default_r7_env_path()
        if os.path.isfile(default_env):
            chosen_file = default_env

    merged: Dict[str, str] = {}
    if chosen_file and os.path.isfile(chosen_file):
        merged.update(parse_env_file(chosen_file, expand_vars=True, environ=proc_env))
    merged.update(clean_proc)
    return merged


def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _apply_env_overrides(cfg: Dict[str, Any], env: Mapping[str, str]) -> Dict[str, Any]:
    out = copy.deepcopy(cfg)
    srv = out.setdefault("server", {})
    if env.get("PET_BACKEND_HOST"):
        srv["host"] = env["PET_BACKEND_HOST"]
    if env.get("PET_BACKEND_PORT"):
        try:
            srv["port"] = int(env["PET_BACKEND_PORT"])
        except ValueError:
            log.warning("PET_BACKEND_PORT 不是整數：%s", env["PET_BACKEND_PORT"])
    if env.get("PET_BACKEND_DATA_DIR"):
        out["data_dir"] = os.path.expanduser(env["PET_BACKEND_DATA_DIR"])
    if env.get("PET_BACKEND_API_TOKEN"):
        srv["api_token"] = env["PET_BACKEND_API_TOKEN"]
    if env.get("PET_BACKEND_API_TOKEN_FILE"):
        tok_path = os.path.expanduser(env["PET_BACKEND_API_TOKEN_FILE"])
        try:
            srv["api_token"] = read_key_file(tok_path)
        except OSError as exc:
            log.warning("無法讀取 PET_BACKEND_API_TOKEN_FILE (%s)：%s", tok_path, exc)
    if env.get("PET_BACKEND_KEYS_FILE"):
        keys_path = os.path.expanduser(env["PET_BACKEND_KEYS_FILE"])
        try:
            with open(keys_path, "r", encoding="utf-8") as fh:
                loaded_keys = json.load(fh)
                if isinstance(loaded_keys, list):
                    srv["keys"] = loaded_keys
                elif isinstance(loaded_keys, dict) and isinstance(loaded_keys.get("keys"), list):
                    srv["keys"] = loaded_keys["keys"]
        except Exception as exc:
            log.warning("無法讀取 PET_BACKEND_KEYS_FILE (%s)：%s", keys_path, exc)
    if env.get("PET_BACKEND_TRUSTED_PROXIES"):
        srv["trusted_proxies"] = [p.strip() for p in env["PET_BACKEND_TRUSTED_PROXIES"].split(",") if p.strip()]
    if env.get("PET_BACKEND_INSECURE_DEV") is not None:
        srv["insecure_dev"] = env["PET_BACKEND_INSECURE_DEV"].strip() in ("1", "true", "True")
    llm_local = out.setdefault("llm", {}).setdefault("local", {})
    if env.get("PET_LLM_LOCAL_URL"):
        llm_local["base_url"] = env["PET_LLM_LOCAL_URL"]
    if env.get("PET_LLM_LOCAL_MODEL"):
        llm_local["model"] = env["PET_LLM_LOCAL_MODEL"]
    if env.get("PET_CONSOLE_LISTEN"):
        out.setdefault("console", {})["listen"] = env["PET_CONSOLE_LISTEN"]
    return out


def load_config(
    path: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    env_file: Optional[str] = None,
) -> Dict[str, Any]:
    """依四層優先順序載入設定：
    4. backend/config.example.json
    3. PET_BACKEND_CONFIG 或本機 backend/config.json
    2. 環境變數（程序環境變數 > R7 環境檔）
    1. path（命令列 --config）與 overrides
    只讀不寫，不會建立、覆蓋或刪除任何設定檔。"""
    # 4. 預設範本
    with open(DEFAULT_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    merged_env = load_r7_env(env_file=env_file, environ=env)
    no_local = merged_env.get("PET_BACKEND_NO_LOCAL_CONFIG") == "1"

    # 3. 本機設定檔（PET_BACKEND_CONFIG 優先，否則 LOCAL_PATH）
    env_cfg_path = merged_env.get("PET_BACKEND_CONFIG")
    if env_cfg_path:
        env_cfg_path = os.path.expanduser(env_cfg_path)
        if os.path.isfile(env_cfg_path):
            with open(env_cfg_path, "r", encoding="utf-8") as fh:
                cfg = deep_merge(cfg, json.load(fh))
        else:
            log.warning("PET_BACKEND_CONFIG 指定的設定檔不存在：%s", env_cfg_path)
    elif not no_local and os.path.isfile(LOCAL_PATH):
        with open(LOCAL_PATH, "r", encoding="utf-8") as fh:
            cfg = deep_merge(cfg, json.load(fh))

    # 2. 環境變數覆寫
    cfg = _apply_env_overrides(cfg, merged_env)

    # 1. 命令列指定檔案與 overrides
    if path and os.path.abspath(path) not in (DEFAULT_PATH, LOCAL_PATH):
        with open(path, "r", encoding="utf-8") as fh:
            cfg = deep_merge(cfg, json.load(fh))
    if overrides:
        cfg = deep_merge(cfg, overrides)

    # data_dir 相對路徑以 backend/ 為基準，不受目前工作目錄影響
    if not os.path.isabs(cfg["data_dir"]):
        cfg["data_dir"] = os.path.join(HERE, cfg["data_dir"])
    return cfg
