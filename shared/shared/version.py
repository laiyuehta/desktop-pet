"""版本與自動更新狀態讀取（PROPOSAL.md 16.3、16.10）。

由前端主程式、後端服務與控制頁面讀取目前部署之版本（commit、describe、部署時間）與自動更新狀態。
未部署（開發模式）時回傳 dev。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("shared.version")

_CACHED_RELEASE: Optional[Dict[str, Any]] = None
_CACHED_RELEASE_MTIME: float = -1.0


def _find_install_root(start_path: Optional[Path] = None) -> Path:
    """尋找 repo 或部署根目錄。"""
    env_inst = os.environ.get("PET_INSTALL_DIR")
    if env_inst:
        p = Path(os.path.expanduser(os.path.expandvars(env_inst)))
        if p.exists():
            return p

    base = start_path or Path(__file__).resolve()
    # 往上尋找帶有 RELEASE.json 或 .git 或 shared 的根目錄
    for p in [base] + list(base.parents):
        if (p / "RELEASE.json").is_file() or (p / ".git").is_dir():
            return p
        if (p / "shared").is_dir() and ((p / "frontend").is_dir() or (p / "backend").is_dir()):
            return p
    return base.parent if base.is_file() else base


def get_release_info(root_dir: Optional[Path] = None) -> Dict[str, Any]:
    """讀取 RELEASE.json，檔案不存在時回傳 dev 資訊。"""
    global _CACHED_RELEASE, _CACHED_RELEASE_MTIME
    base = root_dir or _find_install_root()
    rel_path = base / "RELEASE.json"

    try:
        mtime = rel_path.stat().st_mtime
    except OSError:
        mtime = -1.0

    if _CACHED_RELEASE is not None and _CACHED_RELEASE_MTIME == mtime and mtime != -1.0:
        return dict(_CACHED_RELEASE)

    if mtime != -1.0:
        try:
            with rel_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    _CACHED_RELEASE = data
                    _CACHED_RELEASE_MTIME = mtime
                    return dict(data)
        except Exception as exc:
            log.warning("讀取 RELEASE.json 失敗：%s", exc)

    # 嘗試從 git 取得（本機開發測試環境）
    git_dir = base / ".git"
    commit = "dev"
    short = "dev"
    describe = "dev"
    if git_dir.exists():
        head = git_dir / "HEAD"
        if head.exists():
            try:
                ref = head.read_text(encoding="utf-8").strip()
                if ref.startswith("ref: "):
                    ref_path = git_dir / ref[5:]
                    if ref_path.exists():
                        commit = ref_path.read_text(encoding="utf-8").strip()
                        short = commit[:12]
                else:
                    commit = ref
                    short = commit[:12]
            except Exception:
                pass

    info = {
        "commit": commit,
        "short": short,
        "describe": describe,
        "source": "dev",
    }
    _CACHED_RELEASE = info
    _CACHED_RELEASE_MTIME = -1.0
    return dict(info)


def get_default_state_dir(target: str = "pi") -> Path:
    """取得預設狀態儲存目錄。"""
    env_state = os.environ.get("PET_STATE_DIR")
    if env_state:
        return Path(os.path.expanduser(os.path.expandvars(env_state)))
    if target == "r7" or os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "desktop-pet" / "state" / "update"
        return Path.home() / ".local" / "state" / "desktop-pet" / "update"
    return Path.home() / ".local" / "state" / "desktop-pet" / "update"


def get_autoupdate_status(state_dir: Optional[Path] = None, target: str = "pi") -> Dict[str, Any]:
    """讀取 status.json，提供自動更新運作狀態。"""
    sdir = state_dir or get_default_state_dir(target)
    status_file = sdir / "status.json"
    pause_file = sdir / "PAUSE"

    res: Dict[str, Any] = {
        "status": "on",
        "paused": False,
        "pause_reason": None,
        "last_checked_at": None,
        "last_check_result": None,
    }

    if pause_file.exists():
        res["status"] = "paused"
        res["paused"] = True
        res["pause_reason"] = "PAUSE 暫停檔存在"

    if os.environ.get("PET_AUTOUPDATE") == "0":
        res["status"] = "paused"
        res["paused"] = True
        res["pause_reason"] = "PET_AUTOUPDATE=0"

    if status_file.is_file():
        try:
            with status_file.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    res.update({k: v for k, v in data.items() if v is not None})
        except Exception as exc:
            log.debug("讀取 status.json 失敗：%s", exc)

    return res
