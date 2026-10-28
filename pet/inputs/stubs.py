"""實體輸入的預留類別（硬體還沒決定，全部是 TODO）。"""

from __future__ import annotations

from .base import InputAdapter


class TouchInput(InputAdapter):
    """觸控輸入（例如電容觸控板、TTP223 觸控模組）。

    TODO：讀 GPIO / I2C，分辨摸頭(pat)、戳(poke)、長按(hold)，
    呼叫 self.emit_named("pat", source="touch-head")。
    """
    name = "touch"
    event_type = "touch"

    def start(self) -> None:
        raise NotImplementedError("TODO：觸控硬體尚未決定")


class ButtonInput(InputAdapter):
    """實體按鈕。TODO：GPIO 中斷 + 去彈跳，發 press / long_press。"""
    name = "button"
    event_type = "button"

    def start(self) -> None:
        raise NotImplementedError("TODO：按鈕硬體尚未決定")


class VisionInput(InputAdapter):
    """視覺辨識（例如 Pi 相機）。TODO：偵測有人靠近(person_near)、揮手(wave)、看著它(look_at)。"""
    name = "vision"
    event_type = "vision"

    def start(self) -> None:
        raise NotImplementedError("TODO：視覺辨識尚未實作")
