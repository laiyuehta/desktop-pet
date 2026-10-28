"""輸出端（Output Sink）接口：收 800×480 RGB 的 PIL Image。"""

from __future__ import annotations

import abc

from PIL import Image


class OutputSink(abc.ABC):
    name = "sink"

    def open(self) -> None:
        pass

    @abc.abstractmethod
    def send(self, frame: Image.Image) -> None:
        """送出一格畫面。不應該丟例外讓主迴圈當掉（連線斷了就丟格）。"""

    def close(self) -> None:
        pass


class NullSink(OutputSink):
    """什麼都不做（量 fps、測試用）。"""
    name = "null"

    def send(self, frame: Image.Image) -> None:
        pass
