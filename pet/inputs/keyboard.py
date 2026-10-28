"""鍵盤／終端機模擬器：從 stdin 讀一行指令，轉成事件。

指令（輸入後按 Enter）：
  p 摸頭(pat)   o 戳一下(poke)   b 按鈕(press)   v 有人靠近(person_near)
  w 叫醒(wake)  s 去睡覺(sleep)   1/2/3 切換成長階段
  state <名稱>  強制切狀態        n 點頭  h 搖頭（外部控制器測試）
  q 結束程式
也可以直接輸入事件名稱，例如 idle_timeout。
"""

from __future__ import annotations

import sys
import threading

from ..events import make_event
from .base import InputAdapter

KEYS = {
    "p": ("touch", "pat"), "o": ("touch", "poke"), "b": ("button", "press"),
    "v": ("vision", "person_near"), "w": ("sim", "wake"), "s": ("sim", "sleep"),
}


class KeyboardInput(InputAdapter):
    name = "keyboard"

    def __init__(self, emit, stream=None, on_quit=None):
        super().__init__(emit, source="keyboard")
        self.stream = stream or sys.stdin
        self.on_quit = on_quit
        self.thread = None

    def start(self) -> None:
        self.running = True
        self.thread = threading.Thread(target=self._loop, name="keyboard-input", daemon=True)
        self.thread.start()
        print(__doc__.split("\n", 2)[2], flush=True)

    def handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        parts = line.split()
        cmd = parts[0].lower()
        if cmd in ("q", "quit", "exit"):
            if self.on_quit:
                self.on_quit()
            self.running = False
        elif cmd in KEYS:
            t, n = KEYS[cmd]
            self.emit(make_event(t, n, source=self.source))
        elif cmd in ("1", "2", "3", "4"):
            self.emit(make_event("control", "set_stage", source=self.source, stage=int(cmd)))
        elif cmd == "state" and len(parts) > 1:
            self.emit(make_event("control", "set_state", source=self.source, state=parts[1]))
        elif cmd in ("n", "h"):
            self.emit(make_event("action", "motion", source=self.source,
                                 params={"motion": "nod" if cmd == "n" else "shake"}))
        else:
            self.emit(make_event("sim", line, source=self.source))

    def _loop(self) -> None:
        for line in self.stream:
            if not self.running:
                break
            self.handle_line(line)
