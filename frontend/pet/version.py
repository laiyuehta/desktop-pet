"""版本與自動更新狀態讀取（PROPOSAL.md 16.3、16.10）。

由主程式與遙控頁讀取目前部署之版本（commit、describe、部署時間）與自動更新狀態。
未部署（開發模式）時回傳 dev。
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from pathlib import Path
from shared.version import (
    get_release_info as _shared_get_release_info,
    get_autoupdate_status as _shared_get_autoupdate_status,
)


def get_release_info(root_dir: Optional[Path] = None) -> Dict[str, Any]:
    return _shared_get_release_info(root_dir=root_dir)


def get_autoupdate_status(state_dir: Optional[Path] = None) -> Dict[str, Any]:
    return _shared_get_autoupdate_status(state_dir=state_dir, target="pi")
