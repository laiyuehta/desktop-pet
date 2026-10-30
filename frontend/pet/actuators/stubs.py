"""不碰硬體的控制器：NullActuator（什麼都不做）、LoggingActuator（只記錄）。"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from .base import PanTilt, Wheels

log = logging.getLogger("pet.actuators")


class NullActuator(Wheels, PanTilt):
    """接受所有動作但什麼都不做。"""
    name = "null"

    def perform(self, cmd: dict) -> bool:
        return cmd.get("motion") in ("wheels", "nod", "shake", "center", "set_angle")

    def drive(self, speed, duration=None):
        pass

    def turn(self, direction, speed=0.5, duration=None):
        pass

    def halt(self):
        pass

    def set_angle(self, pan=None, tilt=None, speed=1.0):
        pass


class LoggingActuator(Wheels, PanTilt):
    """把每個動作寫進 log 與 history（網頁遙控的狀態區會顯示），用來測流程。

    同時模擬雲台目前角度，方便之後把畫面表現與實體動作對齊。
    """
    name = "logging"

    def __init__(self, history_size: int = 30):
        self.history: List[str] = []
        self.history_size = history_size
        self.pan = 0.0
        self.tilt = 0.0
        self.wheel_speed = 0.0

    def _rec(self, msg: str) -> None:
        line = time.strftime("%H:%M:%S ") + msg
        log.info("[外部控制器] %s", msg)
        self.history.append(line)
        del self.history[:-self.history_size]

    def perform(self, cmd: dict) -> bool:
        if cmd.get("motion") == "wheels":
            return Wheels.perform(self, cmd)
        if PanTilt.perform(self, cmd):
            extra = f" ×{cmd.get('times', 1)}" if cmd.get("motion") in ("nod", "shake") else ""
            self._rec(f"雲台 {cmd.get('motion')}{extra} → 回到 pan={self.pan:.0f}° tilt={self.tilt:.0f}°")
            return True
        return False

    # Wheels
    def drive(self, speed, duration=None):
        self.wheel_speed = speed
        self._rec(f"輪子 {'前進' if speed >= 0 else '後退'} speed={abs(speed):.2f} duration={duration}")

    def turn(self, direction, speed=0.5, duration=None):
        self._rec(f"輪子 {'左轉' if direction == 'left' else '右轉'} speed={speed:.2f} duration={duration}")

    def halt(self):
        self.wheel_speed = 0.0
        self._rec("輪子 停止")

    # PanTilt
    def set_angle(self, pan: Optional[float] = None, tilt: Optional[float] = None, speed: float = 1.0):
        if pan is not None:
            self.pan = max(self.PAN_RANGE[0], min(self.PAN_RANGE[1], float(pan)))
        if tilt is not None:
            self.tilt = max(self.TILT_RANGE[0], min(self.TILT_RANGE[1], float(tilt)))

    def stop(self):
        self.halt()
        self.center()
