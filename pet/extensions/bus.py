"""事件匯流排。

- publish() 可以從任何執行緒呼叫（輸入轉接器、HTTP 執行緒都在別的執行緒），
  事件先放進佇列；
- 主迴圈每格呼叫 dispatch_pending()，在主執行緒把事件依序送給訂閱者，
  所以狀態機和繪圖不需要加鎖。
- subscribe(pattern, handler)：pattern 可以是 "*"、"touch"（只看 type）、
  "touch:pat"（type:name）或 "*:pat"。
"""

from __future__ import annotations

import logging
import queue
from typing import Callable, Dict, List, Tuple

log = logging.getLogger("pet.bus")

Handler = Callable[[dict], None]


def _match(pattern: str, ev: dict) -> bool:
    if pattern == "*":
        return True
    if ":" in pattern:
        t, n = pattern.split(":", 1)
        return (t == "*" or t == ev.get("type")) and (n == "*" or n == ev.get("name"))
    return pattern == ev.get("type")


class EventBus:
    def __init__(self, max_per_dispatch: int = 256):
        self._q: "queue.Queue[dict]" = queue.Queue()
        self._subs: List[Tuple[str, Handler, str]] = []
        self.max_per_dispatch = max_per_dispatch
        self.history: List[dict] = []      # 最近的事件（給狀態頁、除錯用）
        self.history_size = 20

    def subscribe(self, pattern: str, handler: Handler, owner: str = "") -> None:
        self._subs.append((pattern, handler, owner))

    def unsubscribe_owner(self, owner: str) -> None:
        self._subs = [s for s in self._subs if s[2] != owner]

    def publish(self, ev: dict) -> None:
        """執行緒安全：只放進佇列。"""
        self._q.put(ev)

    def dispatch_pending(self) -> int:
        n = 0
        while n < self.max_per_dispatch:
            try:
                ev = self._q.get_nowait()
            except queue.Empty:
                break
            self.dispatch(ev)
            n += 1
        return n

    def dispatch(self, ev: dict) -> None:
        """立即在目前執行緒派送（主執行緒用）。"""
        self.history.append(ev)
        del self.history[:-self.history_size]
        for pattern, handler, owner in list(self._subs):
            if _match(pattern, ev):
                try:
                    handler(ev)
                except Exception:  # 一個模組出錯不能讓寵物當掉
                    log.exception("事件處理失敗（%s）：%s", owner or pattern, ev)
