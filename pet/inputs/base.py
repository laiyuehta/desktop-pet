"""輸入轉接器（Input Adapter）接口。

每種輸入是一個獨立模組，把原始訊號轉成標準事件（見 pet/events.py），
呼叫 self.emit(event) 丟給事件匯流排。emit 是執行緒安全的。
"""

from __future__ import annotations

import abc
from typing import Callable, Optional

from ..events import make_event


class InputAdapter(abc.ABC):
    name = "input"
    event_type = "sim"

    def __init__(self, emit: Callable[[dict], None], source: Optional[str] = None):
        self._emit = emit
        self.source = source or self.name
        self.running = False

    def emit(self, ev: dict) -> None:
        self._emit(ev)

    def emit_named(self, name: str, type_: Optional[str] = None, **extra) -> None:
        self.emit(make_event(type_ or self.event_type, name, source=self.source, **extra))

    @abc.abstractmethod
    def start(self) -> None:
        """開始讀取輸入（通常開背景執行緒）。"""

    def stop(self) -> None:
        self.running = False
