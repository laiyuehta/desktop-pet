"""輸出端（Output Sink）接口：收 800×480 RGB 的 PIL Image。"""

from __future__ import annotations

import abc

from PIL import Image


class OutputSink(abc.ABC):
    name = "sink"
    mirror = None        # ScreenMirror（螢幕畫面同步）；PiscreenSink／NullSink 會把送出的 JPEG 同步一份
    frame_meta = None    # 這一格的來源資訊（PackDriver 每格設定）

    def open(self) -> None:
        pass

    @abc.abstractmethod
    def send(self, frame: Image.Image) -> None:
        """送出一格畫面。不應該丟例外讓主迴圈當掉（連線斷了就丟格）。"""

    def send_jpeg(self, data: bytes) -> None:
        """送一格已經編好的 800×480 baseline JPEG（動畫包用）。預設解碼後走 send()；
        PiscreenSink 會直接轉送、不重新編碼。"""
        import io
        img = Image.open(io.BytesIO(data))
        img.load()
        self.send(img.convert("RGB"))

    def close(self) -> None:
        pass


class NullSink(OutputSink):
    """什麼都不做（量 fps、測試用）。"""
    name = "null"

    def send(self, frame: Image.Image) -> None:
        pass

    def send_jpeg(self, data: bytes) -> None:
        if self.mirror is not None:
            self.mirror.publish(data, self.frame_meta)
