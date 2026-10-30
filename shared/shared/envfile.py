"""KEY=VALUE 環境檔格式（PROPOSAL.md 16.2），Pi 的 /etc/desktop-pet/env 與 R7 的 %LOCALAPPDATA%\\desktop-pet\\env 共用。

- 一行一個 KEY=VALUE，# 開頭是註解；不加引號、不寫 export。
- 值留空（例如 PET_HOTSPOT_URL=）視為未設，不放進回傳字典。
- 變數展開由呼叫端決定（傳 expand 函式），格式本身不展開。
"""
import logging
import os
from typing import Callable, Dict, Iterable, Optional

log = logging.getLogger("shared.envfile")


def parse_env_lines(lines: Iterable[str], expand: Optional[Callable[[str], str]] = None) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k or not v:
            continue
        out[k] = expand(v) if expand else v
    return out


def parse_env_file(path: str, expand: Optional[Callable[[str], str]] = None) -> Dict[str, str]:
    """讀取單一環境檔；檔案不存在或讀不到時回傳空 dict。"""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        log.warning("無法讀取環境檔 %s：%s", path, exc)
        return {}
    return parse_env_lines(lines, expand)
