"""把各層接起來的 PetApp：事件匯流排 → 狀態機 → 表現層；動作請求 → 外部控制器。

主迴圈（main.py）每格呼叫：
    app.step(dt)        # 派送事件、更新狀態機計時、更新動畫與表情
    frame = app.render()
    sink.send(frame)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional

from .actuators import ActuatorHub, LoggingActuator
from .events import make_event
from .extensions import EventBus, ExampleExtension, ExtensionManager
from .eyes import EyeController
from .renderer import Renderer
from .skeleton import Skeleton
from .state_machine import ClockEvents, StateMachine

log = logging.getLogger("pet.app")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PetApp:
    def __init__(self, config_path: Optional[str] = None, timer_overrides: Optional[Dict[str, float]] = None,
                 use_clock: bool = True, eye_seed: Optional[int] = None, actuators: Optional[ActuatorHub] = None):
        config_path = config_path or os.path.join(ROOT, "config", "states.json")
        with open(config_path, "r", encoding="utf-8") as fh:
            self.config = json.load(fh)
        self.bus = EventBus()
        self.fsm = StateMachine(self.config, timer_overrides=timer_overrides, emit=self.bus.publish)
        self._skeleton_cache: Dict[int, Skeleton] = {}
        self.renderer = Renderer(self._load_stage_skeleton(self.fsm.growth.stage))
        self.eyes = EyeController(seed=eye_seed)
        self.logger_actuator = LoggingActuator()
        self.actuators = actuators or ActuatorHub([self.logger_actuator])
        self.extensions = ExtensionManager(self.bus)
        self.extensions.register(ExampleExtension())
        clock_cfg = self.config.get("clock", {})
        self.clock = (ClockEvents(self.bus.publish, clock_cfg.get("night_start", 23), clock_cfg.get("night_end", 7))
                      if use_clock and clock_cfg.get("enabled", True) else None)
        self._clock_acc = 0.0
        self.fps = 0.0
        self.started = time.time()
        self._status: dict = {}
        self._status_lock = threading.Lock()

        self.fsm.state_listeners.append(self._on_state_change)
        self.fsm.stage_listeners.append(self._on_stage_change)
        self.bus.subscribe("*", self._route, owner="core")
        self._apply_state(None)

    # -- 生命週期 ---------------------------------------------------------------
    def start(self) -> None:
        self.actuators.start()
        self.extensions.start_all()

    def stop(self) -> None:
        self.extensions.stop_all()
        self.actuators.stop()

    def post(self, ev: dict) -> None:
        """執行緒安全：送事件進來（輸入轉接器用）。"""
        self.bus.publish(ev)

    # -- 每格 -------------------------------------------------------------------
    def step(self, dt: float) -> None:
        self.bus.dispatch_pending()
        self.fsm.update(dt)
        if self.clock:
            self._clock_acc += dt
            if self._clock_acc >= 5.0:
                self._clock_acc = 0.0
                self.clock.poll()
        self.renderer.update(dt)
        self._eye_state = self.eyes.update(dt)
        self._update_status()

    def render(self):
        if not hasattr(self, "_eye_state"):
            self._eye_state = self.eyes.update(0.0)
        return self.renderer.render(self._eye_state)

    # -- 事件路由 ---------------------------------------------------------------
    def _route(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "pet":
            return                                  # 自己發的通知，不再處理
        if t == "control":
            try:
                if ev.get("name") == "set_state":
                    self.fsm.force(ev["state"], reason=ev.get("source", "control"))
                elif ev.get("name") == "set_stage":
                    self.fsm.set_stage(int(ev["stage"]))
            except (KeyError, ValueError) as exc:
                log.warning("控制指令失敗：%s", exc)
            return
        if t == "action":
            self._handle_action(ev)
            return
        self.fsm.handle_event(ev)

    def _handle_action(self, ev: dict) -> None:
        """動作請求（ActionRouter）：寵物本體決定怎麼表現。"""
        name, params = ev.get("name"), dict(ev.get("params") or {})
        if name == "motion":
            self.actuators.perform(params)
        elif name == "expression":
            try:
                self.eyes.override(params.get("expression", "surprised"), float(params.get("seconds", 2)))
            except KeyError as exc:
                log.warning("%s", exc)
        elif name == "set_state":
            try:
                self.fsm.force(params["state"], reason=ev.get("source", "action"))
            except KeyError as exc:
                log.warning("%s", exc)
        elif name in ("say", "sound"):
            log.info("（第一期尚未實作）%s：%s", name, params)
        else:
            log.warning("未知的動作請求：%s", name)

    def _on_state_change(self, old: str, new: str, ev: Optional[dict]) -> None:
        self._apply_state(ev, replay=(old == new))
        for act in self.fsm.info.get("on_enter", []):
            self.bus.publish(make_event("action", act["name"], source=f"state:{new}", params=act.get("params", {})))
        self.bus.publish(make_event("pet", "state_changed", source="core", old=old, new=new,
                                    cause=(ev or {}).get("name")))

    def _apply_state(self, ev: Optional[dict], replay: bool = False) -> None:
        self.renderer.player.play(self.fsm.animation(), restart=False)
        self.eyes.set_expression(self.fsm.eyes())
        self.renderer.effects = set(self.fsm.effects())

    # -- 成長／換骨架 -------------------------------------------------------------
    def _load_stage_skeleton(self, stage: int) -> Skeleton:
        if stage in self._skeleton_cache:
            return self._skeleton_cache[stage]
        info = self.fsm.stages.get(stage, {})
        path = os.path.join(ROOT, info.get("skeleton", "assets/skeletons/seed.json"))
        if not os.path.exists(path):
            log.warning("階段 %s 的骨架 %s 還沒做，先沿用種子外型", stage, info.get("skeleton"))
            path = os.path.join(ROOT, "assets", "skeletons", "seed.json")
        sk = Skeleton.load(path)
        self._skeleton_cache[stage] = sk
        return sk

    def _on_stage_change(self, old: int, new: int) -> None:
        """set_stage() 的掛勾：換骨架，動畫名稱共用，狀態機不變。"""
        sk = self._load_stage_skeleton(new)
        if sk is not self.renderer.skeleton:
            self.renderer.set_skeleton(sk)
        self.renderer.player.play(self.fsm.animation(), restart=True)
        self.bus.publish(make_event("pet", "stage_changed", source="core", old=old, new=new))

    # -- 狀態（給網頁遙控）----------------------------------------------------------
    def _update_status(self) -> None:
        last = self.fsm.last_event
        st = {
            "state": self.fsm.state,
            "state_label": self.fsm.info.get("label", self.fsm.state),
            "states": list(self.fsm.states),
            "time_in_state": round(self.fsm.time_in_state, 1),
            "stage": self.fsm.growth.stage,
            "stage_name": self.fsm.stage_name(),
            "growth": self.fsm.growth.to_dict(),
            "skeleton": self.renderer.skeleton.name,
            "animation": self.renderer.player.current,
            "eyes": self.eyes.expression,
            "fps": round(self.fps, 1),
            "uptime": round(time.time() - self.started, 1),
            "last_event": {k: last.get(k) for k in ("type", "name", "source")} if last else None,
            "actuator_log": list(self.logger_actuator.history[-10:]),
            "extensions": self.extensions.list(),
        }
        with self._status_lock:
            self._status = st

    def status(self) -> dict:
        with self._status_lock:
            return dict(self._status)
