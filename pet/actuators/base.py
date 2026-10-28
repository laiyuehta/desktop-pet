"""外部控制器（實體動作硬體）接口：輪子、雲台。第一期只預留，沒有真的驅動。

設計：
- ActuatorController：所有控制器的基底（name、start/stop、perform(cmd)）。
- Wheels：輪子（前進／後退／左右轉／停止，speed 0~1，duration 秒）。
- PanTilt：雲台（點頭＝tilt、搖頭＝pan、設定角度、速度、回正）。
- 狀態或擴充模組不直接呼叫硬體，而是發「動作請求」事件：
    {"type":"action","name":"motion","params":{"motion":"nod","times":1}}
  由 ActuatorHub 轉給對應的控制器。
- 真正的硬體驅動（馬達驅動板、伺服機 PWM）之後再寫子類別，見各 TODO。
"""

from __future__ import annotations

import abc
import logging
from typing import Dict, List, Optional

log = logging.getLogger("pet.actuators")


class ActuatorController(abc.ABC):
    name = "actuator"

    def start(self) -> None:
        """初始化硬體。"""

    def stop(self) -> None:
        """停止所有動作、釋放硬體（程式結束一定會呼叫，安全第一）。"""

    @abc.abstractmethod
    def perform(self, cmd: dict) -> bool:
        """執行一個動作指令；不支援時回傳 False。"""


class Wheels(ActuatorController):
    """輪子（差速兩輪）。TODO：實作馬達驅動（例如 L298N / DRV8833 + GPIO PWM）。"""
    name = "wheels"

    @abc.abstractmethod
    def drive(self, speed: float, duration: Optional[float] = None) -> None:
        """speed > 0 前進、< 0 後退（-1~1）；duration=None 表示持續到 stop()。"""

    @abc.abstractmethod
    def turn(self, direction: str, speed: float = 0.5, duration: Optional[float] = None) -> None:
        """direction: "left" / "right"（原地轉）。"""

    @abc.abstractmethod
    def halt(self) -> None:
        """立即停止輪子。"""

    def perform(self, cmd: dict) -> bool:
        if cmd.get("motion") != "wheels":
            return False
        act = cmd.get("action", "stop")
        speed = max(0.0, min(1.0, float(cmd.get("speed", 0.5))))
        dur = cmd.get("duration")
        dur = float(dur) if dur is not None else None
        if act == "forward":
            self.drive(speed, dur)
        elif act == "backward":
            self.drive(-speed, dur)
        elif act in ("left", "right"):
            self.turn(act, speed, dur)
        elif act == "stop":
            self.halt()
        else:
            return False
        return True


class PanTilt(ActuatorController):
    """雲台（兩軸伺服機）：pan 左右、tilt 上下。TODO：實作伺服機驅動（例如 PCA9685）。"""
    name = "pantilt"
    PAN_RANGE = (-60.0, 60.0)
    TILT_RANGE = (-30.0, 30.0)

    @abc.abstractmethod
    def set_angle(self, pan: Optional[float] = None, tilt: Optional[float] = None, speed: float = 1.0) -> None:
        """設定角度（度），speed 0~1 表示轉動速度。"""

    def nod(self, times: int = 1, amplitude: float = 12.0, speed: float = 0.8) -> None:
        """點頭 = tilt 上下。預設實作用 set_angle 串起來，真驅動可以覆寫成非同步。"""
        for _ in range(max(1, times)):
            self.set_angle(tilt=amplitude, speed=speed)
            self.set_angle(tilt=-amplitude * 0.3, speed=speed)
        self.set_angle(tilt=0.0, speed=speed)

    def shake(self, times: int = 1, amplitude: float = 20.0, speed: float = 0.8) -> None:
        """搖頭 = pan 左右。"""
        for _ in range(max(1, times)):
            self.set_angle(pan=-amplitude, speed=speed)
            self.set_angle(pan=amplitude, speed=speed)
        self.set_angle(pan=0.0, speed=speed)

    def center(self) -> None:
        self.set_angle(pan=0.0, tilt=0.0)

    def perform(self, cmd: dict) -> bool:
        m = cmd.get("motion")
        speed = float(cmd.get("speed", 0.8))
        if m == "nod":
            self.nod(int(cmd.get("times", 1)), float(cmd.get("amplitude", 12.0)), speed)
        elif m == "shake":
            self.shake(int(cmd.get("times", 1)), float(cmd.get("amplitude", 20.0)), speed)
        elif m == "center":
            self.center()
        elif m == "set_angle":
            self.set_angle(cmd.get("pan"), cmd.get("tilt"), speed)
        else:
            return False
        return True


class ActuatorHub:
    """把動作請求分派給已註冊的控制器。沒有控制器能處理時只記錄。"""

    def __init__(self, controllers: Optional[List[ActuatorController]] = None):
        self.controllers: List[ActuatorController] = list(controllers or [])
        self.log: List[str] = []

    def add(self, c: ActuatorController) -> None:
        self.controllers.append(c)

    def start(self) -> None:
        for c in self.controllers:
            c.start()

    def stop(self) -> None:
        for c in self.controllers:
            try:
                c.stop()
            except Exception:
                log.exception("停止控制器失敗：%s", c.name)

    def perform(self, cmd: Dict) -> bool:
        for c in self.controllers:
            if c.perform(cmd):
                return True
        log.info("沒有控制器處理動作：%s", cmd)
        self.log.append(f"（無人處理）{cmd}")
        return False
