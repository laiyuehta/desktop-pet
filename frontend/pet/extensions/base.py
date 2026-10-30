"""能力擴充接口（第二期用，第一期只定義接口 + 空範例）。

每個擴充模組：
- 有 name、enabled 開關、start()/stop()
- 可以 subscribe() 寵物發出的事件（被摸、醒來、狀態改變……）
- 可以 publish() 事件給寵物（例如 {"type":"ext","name":"reminder"}）
- 可以 request_action() 請寵物做表情、點頭、顯示文字、播聲音；
  寵物本體決定怎麼表現（ActionRouter，見 pet/app.py）。
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from ..events import make_event
from .bus import EventBus

log = logging.getLogger("pet.ext")

# 擴充模組可以請求的動作（寵物本體負責實作）
ACTIONS = {
    "expression": "暫時換表情，params: {expression, seconds}",
    "motion": "外部控制器動作，params: {motion: nod|shake|center|set_angle|wheels, ...}",
    "say": "顯示一段文字（第一期只記錄 log）params: {text}",
    "sound": "播聲音（第一期只記錄 log）params: {name}",
    "set_state": "請求切換狀態 params: {state}",
}


class Extension:
    name = "extension"

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.bus: Optional[EventBus] = None

    # -- 由 ExtensionManager 呼叫 ---------------------------------------------
    def attach(self, bus: EventBus) -> None:
        self.bus = bus

    def start(self) -> None:
        """初始化（開連線、開執行緒等）。"""

    def stop(self) -> None:
        """停止並釋放資源。"""

    # -- 給模組用的工具 ---------------------------------------------------------
    def subscribe(self, pattern: str, handler: Callable[[dict], None]) -> None:
        self.bus.subscribe(pattern, handler, owner=f"ext:{self.name}")

    def publish(self, name: str, type_: str = "ext", **extra) -> None:
        self.bus.publish(make_event(type_, name, source=f"ext:{self.name}", **extra))

    def request_action(self, action: str, **params) -> None:
        if action not in ACTIONS:
            raise ValueError(f"不支援的動作 {action}")
        self.bus.publish(make_event("action", action, source=f"ext:{self.name}", params=params))


class ExtensionManager:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self.modules: Dict[str, Extension] = {}

    def register(self, ext: Extension) -> None:
        if ext.name in self.modules:
            raise ValueError(f"擴充模組名稱重複：{ext.name}")
        self.modules[ext.name] = ext

    def start_all(self) -> None:
        for ext in self.modules.values():
            if not ext.enabled:
                continue
            ext.attach(self.bus)
            try:
                ext.start()
                log.info("擴充模組已啟動：%s", ext.name)
            except Exception:
                log.exception("擴充模組啟動失敗：%s", ext.name)

    def stop_all(self) -> None:
        for ext in self.modules.values():
            if ext.enabled and ext.bus is not None:
                try:
                    ext.stop()
                finally:
                    self.bus.unsubscribe_owner(f"ext:{ext.name}")

    def list(self) -> List[dict]:
        return [{"name": e.name, "enabled": e.enabled} for e in self.modules.values()]
