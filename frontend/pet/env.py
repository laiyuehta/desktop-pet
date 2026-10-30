"""環境變數與環境檔（KEY=VALUE）讀取工具（PROPOSAL.md 16.2）。

格式規則：
- 一行一個 KEY=VALUE，# 開頭是註解；不加引號、不寫 export、不用 $VAR 展開、= 兩邊不留空白。
- 值留空（例如 PET_HOTSPOT_URL=）視為未設，不放進回傳字典。
- 支援替換路徑中的 @HOME@（以 PET_HOME 或目前使用者家目錄展開）。
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Mapping, Optional

from shared.envfile import parse_env_file as _shared_parse_env_file

log = logging.getLogger("pet.env")

DEFAULT_PI_ENV_FILE = "/etc/desktop-pet/env"
# repo 根目錄（frontend/ 的上一層）：舊版 deploy.env 放在這裡
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEGACY_DEPLOY_ENV = os.path.join(REPO_ROOT, "deploy.env")


def parse_env_file(path: str, expand_vars: bool = False) -> Dict[str, str]:
    """讀取單一 KEY=VALUE 環境檔；檔案不存在時回傳空 dict。"""
    # 格式（值留空視為沒設）與後端共用 shared.envfile
    return _shared_parse_env_file(path, os.path.expandvars if expand_vars else None)


def expand_pet_path(val: Optional[str], env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """展開路徑中的 @HOME@ 與 ~。"""
    if not val:
        return None
    home = (env or {}).get("PET_HOME") or os.environ.get("PET_HOME") or os.path.expanduser("~")
    expanded = val.replace("@HOME@", home)
    if expanded.startswith("~/") or expanded.startswith("~\\"):
        expanded = home + expanded[1:]
    elif expanded == "~":
        expanded = home
    return os.path.expanduser(expanded)


def load_pi_env(
    env_file: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    repo_root: Optional[str] = None,
    warn_legacy: bool = True,
) -> Dict[str, str]:
    """依 PROPOSAL.md 16.2 載入 Pi 環境變數（程序環境變數 > 環境檔）。

    環境檔尋找順序：
    1. env_file 參數或環境變數 PET_ENV_FILE
    2. /etc/desktop-pet/env（DEFAULT_PI_ENV_FILE）
    3. 舊版 <repo>/deploy.env（過渡期相容，讀到時印 WARNING）
    """
    proc_env = dict(os.environ if environ is None else environ)
    # 清除空字串，視為未設
    clean_proc = {k: v for k, v in proc_env.items() if v != ""}

    chosen_file: Optional[str] = env_file or clean_proc.get("PET_ENV_FILE")
    is_legacy = False
    if not chosen_file:
        if os.path.isfile(DEFAULT_PI_ENV_FILE):
            chosen_file = DEFAULT_PI_ENV_FILE
        else:
            root = repo_root or REPO_ROOT
            legacy = os.path.join(root, "deploy.env")
            if os.path.isfile(legacy):
                chosen_file = legacy
                is_legacy = True

    file_env: Dict[str, str] = {}
    if chosen_file and os.path.isfile(chosen_file):
        if is_legacy and warn_legacy:
            log.warning(
                "從舊版 %s 讀取設定；請改放到 /etc/desktop-pet/env（見 PROPOSAL.md 16.2）",
                chosen_file,
            )
        file_env = parse_env_file(chosen_file)

    merged: Dict[str, str] = {}
    merged.update(file_env)
    merged.update(clean_proc)
    return merged

