"""空的擴充模組範例：只示範怎麼訂閱事件、怎麼請求動作，不做任何真正的功能。"""

from __future__ import annotations

import logging

from .base import Extension

log = logging.getLogger("pet.ext.example")


class ExampleExtension(Extension):
    name = "example"

    def start(self) -> None:
        self.subscribe("pet:state_changed", self.on_state_changed)

    def on_state_changed(self, ev: dict) -> None:
        log.debug("example 收到狀態改變：%s → %s", ev.get("old"), ev.get("new"))
        # 第二期的模組可以在這裡做事，例如：
        # if ev["new"] == "idle" and 有新提醒:
        #     self.request_action("expression", expression="surprised", seconds=2)
        #     self.request_action("say", text="有新提醒")
