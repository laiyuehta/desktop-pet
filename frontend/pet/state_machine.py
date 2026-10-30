"""事件驅動狀態機（設定檔 config/states.json）。

- 對應表：(目前狀態, 事件名稱) → 下一個狀態，"*" 代表任何狀態。
- 計時事件：每個狀態可以設「進入後 / 最後一次互動後 N 秒」發出某事件，
  例如 idle 600 秒 → idle_timeout、happy 3 秒 → happy_done。
- 表現：每個狀態對應 animation（骨架動畫名稱）+ eyes（表情名稱）+ effects。
- 成長：Growth 資料結構與 set_stage() 接口；推進規則還沒決定，所以沒實作。

狀態機本身不依賴繪圖、硬體，可以單獨測試、移植。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .events import INTERACTION_TYPES, make_event

log = logging.getLogger("pet.fsm")


@dataclass
class Growth:
    """成長資料（存檔、伺服器同步的單位）。推進規則 TODO：待項目書決定。"""
    value: float = 0.0          # 成長值
    stage: int = 1              # 階段：1 種子、2 長腳、3 長耳朵……
    branch: Optional[str] = None  # 路線（例如兔耳 A/B/C），尚未設定
    history: List[dict] = field(default_factory=list)

    def add(self, amount: float, reason: str = "") -> None:
        """累積成長值。TODO：什麼條件會升階段還沒決定，這裡不自動升級。"""
        self.value += amount
        self.history.append({"add": amount, "reason": reason})
        del self.history[:-50]

    def to_dict(self) -> dict:
        return {"value": self.value, "stage": self.stage, "branch": self.branch}


StateListener = Callable[[str, str, Optional[dict]], None]   # (舊狀態, 新狀態, 觸發事件)
StageListener = Callable[[int, int], None]                    # (舊階段, 新階段)


class StateMachine:
    def __init__(self, config: dict, timer_overrides: Optional[Dict[str, float]] = None,
                 emit: Optional[Callable[[dict], None]] = None):
        self.config = config
        self.states: Dict[str, dict] = config["states"]
        self.table: Dict[tuple, str] = {}
        for tr in config.get("transitions", []):
            src, evn, dst = tr["from"], tr["event"], tr["to"]
            if dst not in self.states:
                raise ValueError(f"轉移目標 {dst} 不是已定義的狀態")
            if src != "*" and src not in self.states:
                raise ValueError(f"轉移來源 {src} 不是已定義的狀態")
            self.table[(src, evn)] = dst
        self.timer_overrides = dict(timer_overrides or {})
        self.emit = emit                  # 有事件匯流排時，計時事件經匯流排送回來
        self.state_listeners: List[StateListener] = []
        self.stage_listeners: List[StageListener] = []
        self.growth = Growth()
        self.stages: Dict[int, dict] = {int(k): v for k, v in
                                        config.get("growth", {}).get("stages", {}).items()}
        self.state = config.get("initial", next(iter(self.states)))
        self.time_in_state = 0.0
        self.last_event: Optional[dict] = None
        self._timers: List[dict] = []
        self._reset_timers()

    @classmethod
    def from_file(cls, path: str, **kw) -> "StateMachine":
        with open(path, "r", encoding="utf-8") as fh:
            return cls(json.load(fh), **kw)

    # -- 查詢 ------------------------------------------------------------------
    @property
    def info(self) -> dict:
        return self.states[self.state]

    def animation(self) -> str:
        return self.info.get("animation", self.state)

    def eyes(self) -> str:
        return self.info.get("eyes", "normal")

    def effects(self) -> List[str]:
        return list(self.info.get("effects", []))

    def lookup(self, state: str, event_name: str) -> Optional[str]:
        return self.table.get((state, event_name)) or self.table.get(("*", event_name))

    # -- 計時 ------------------------------------------------------------------
    def reset_input_timers(self) -> None:
        """重設所有標記為 reset_on_input 的計時器（互動事件時呼叫）。"""
        for t in self._timers:
            if t["reset_on_input"]:
                t["elapsed"], t["fired"] = 0.0, False

    def _reset_timers(self) -> None:
        self._timers = []
        for t in self.states[self.state].get("timers", []):
            # 覆寫順序：「狀態.事件」 > 「事件」 > 設定檔
            key = f"{self.state}.{t['event']}"
            after = float(self.timer_overrides.get(key, self.timer_overrides.get(t["event"], t["after"])))
            self._timers.append({"after": after, "event": t["event"], "elapsed": 0.0,
                                 "fired": False, "reset_on_input": t.get("reset_on_input", True)})

    def update(self, dt: float) -> None:
        self.time_in_state += dt
        for t in list(self._timers):
            if t["fired"]:
                continue
            t["elapsed"] += dt
            if t["elapsed"] >= t["after"]:
                t["fired"] = True
                ev = make_event("time", t["event"], source=f"timer:{self.state}", for_state=self.state)
                if self.emit:
                    self.emit(ev)
                else:
                    self.handle_event(ev)

    # -- 事件 ------------------------------------------------------------------
    def handle_event(self, ev: dict) -> Optional[str]:
        """處理一個事件；有轉移時回傳新狀態，否則 None。"""
        name = ev.get("name")
        if ev.get("type") == "time" and ev.get("for_state") not in (None, self.state):
            return None   # 過期的計時事件（狀態已經換了）
        self.last_event = ev
        if ev.get("type") in INTERACTION_TYPES:
            self.reset_input_timers()
        nxt = self.lookup(self.state, name)
        if nxt is None:
            return None
        self._enter(nxt, ev)
        return nxt

    def force(self, state: str, reason: str = "force") -> None:
        """強制切換狀態（遙控頁、伺服器指令用）。"""
        if state not in self.states:
            raise KeyError(f"未知的狀態 {state}")
        self._enter(state, make_event("control", "set_state", source=reason, state=state))

    def _enter(self, new: str, ev: Optional[dict]) -> None:
        old = self.state
        self.state = new
        self.time_in_state = 0.0
        self._reset_timers()
        log.info("狀態 %s → %s（事件 %s）", old, new, ev.get("name") if ev else None)
        for fn in self.state_listeners:
            fn(old, new, ev)

    # -- 成長 ------------------------------------------------------------------
    def set_stage(self, stage: int) -> None:
        """切換成長階段（換骨架由監聽者負責）。推進規則 TODO。"""
        stage = int(stage)
        if self.stages and stage not in self.stages:
            raise KeyError(f"未定義的成長階段 {stage}")
        old = self.growth.stage
        self.growth.stage = stage
        for fn in self.stage_listeners:
            fn(old, stage)

    def stage_name(self) -> str:
        return self.stages.get(self.growth.stage, {}).get("name", str(self.growth.stage))


class ClockEvents:
    """依牆上時鐘發出 night / morning 事件（只在跨越時發一次，開機時不發）。"""

    def __init__(self, emit: Callable[[dict], None], night_start: int = 23, night_end: int = 7,
                 now: Callable[[], _dt.datetime] = _dt.datetime.now):
        self.emit = emit
        self.night_start, self.night_end = night_start, night_end
        self.now = now
        self._was_night: Optional[bool] = None

    def is_night(self, hour: int) -> bool:
        if self.night_start > self.night_end:
            return hour >= self.night_start or hour < self.night_end
        return self.night_start <= hour < self.night_end

    def poll(self) -> None:
        night = self.is_night(self.now().hour)
        if self._was_night is not None and night != self._was_night:
            self.emit(make_event("time", "night" if night else "morning", source="clock"))
        self._was_night = night
