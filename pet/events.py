"""標準事件格式與小工具。

所有輸入、時間、伺服器指令、擴充模組都統一成同一種 dict：
    {"type": "touch", "name": "pat", "source": "touch-head", "ts": 1730000000.0, ...}

type 常見值：
- touch / button / vision / sim ：互動輸入（會重置閒置計時）
- talk    ：對人說話（{"type":"talk","name":"talk","text":"…"}，有接後端時轉給 /v1/pet/talk）
- time    ：時間事件（idle_timeout、happy_done、night、morning）
- control ：遙控指令（set_state、set_stage）
- action  ：動作請求（motion、expression、say、sound），由寵物本體決定怎麼表現
- pet     ：寵物本體發出的通知（state_changed、stage_changed）
"""

import time
from typing import Any, Dict, Optional

INTERACTION_TYPES = {"touch", "button", "vision", "sim", "talk"}


def make_event(type_: str, name: str, source: str = "unknown", ts: Optional[float] = None,
               **extra: Any) -> Dict[str, Any]:
    ev = {"type": type_, "name": name, "source": source, "ts": time.time() if ts is None else ts}
    ev.update(extra)
    return ev


def normalize_event(raw: Dict[str, Any], default_source: str = "unknown") -> Dict[str, Any]:
    """把外部送進來的事件補齊欄位並檢查。"""
    if not isinstance(raw, dict):
        raise ValueError("事件必須是 JSON 物件")
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("事件缺少 name")
    ev = dict(raw)
    ev.setdefault("type", "sim")
    ev.setdefault("source", default_source)
    ev.setdefault("ts", time.time())
    return ev
