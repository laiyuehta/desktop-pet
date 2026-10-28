"""裝置端的後端（大腦）客戶端。

- 背景執行緒送事件到後端，主迴圈完全不等網路（send_event 只是放進佇列）。
- 每個請求逾時很短（預設 2 秒；對話因為要等 LLM，預設 15 秒）。
- 後端連不上時安靜降級：記錄一次 log、暫停送事件一段時間（退避），佇列滿了丟最舊的；
  寵物照常待機、眨眼、睡覺。
- 後端回傳的指令（directives）轉成既有的事件丟回事件匯流排，由 PetApp 照原本的流程處理：
    set_state / set_stage → control 事件
    say / emotion / actuator → action 事件（say 會在畫面上顯示對話框）
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from .events import make_event

log = logging.getLogger("pet.backend")

# 後端情緒 → 裝置表情（pet/eyes.py 的 EXPRESSIONS）
EMOTION_TO_EXPRESSION = {
    "happy": "happy", "normal": "normal", "surprised": "surprised", "sleepy": "sleepy",
    "bored": "bored", "sad": "sad", "shy": "squint",
}

OFFLINE_LINES = "嗯…？（大腦在休息）"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def directive_to_event(d: Dict[str, Any]) -> Optional[dict]:
    """把一個後端指令轉成裝置事件；不認得的回傳 None。"""
    t = d.get("type")
    src = "backend"
    try:
        if t == "set_state":
            return make_event("control", "set_state", source=src, state=str(d["state"]))
        if t == "set_stage":
            return make_event("control", "set_stage", source=src, stage=int(d["stage"]))
        if t == "say":
            text = str(d.get("text", "")).strip()
            if not text:
                return None
            return make_event("action", "say", source=src,
                              params={"text": text[:80], "seconds": float(d.get("seconds", 4))})
        if t == "emotion":
            expr = EMOTION_TO_EXPRESSION.get(str(d.get("emotion")), None)
            if not expr:
                return None
            return make_event("action", "expression", source=src,
                              params={"expression": expr, "seconds": float(d.get("seconds", 2))})
        if t == "actuator":
            params = {k: v for k, v in d.items() if k != "type"}
            if not params.get("motion"):
                return None
            return make_event("action", "motion", source=src, params=params)
    except (KeyError, TypeError, ValueError):
        return None
    return None


class BackendClient:
    def __init__(self, base_url: str, pet_id: str, publish: Callable[[dict], None],
                 status_fn: Optional[Callable[[], dict]] = None, timeout: float = 2.0,
                 talk_timeout: float = 15.0, token: str = "", heartbeat_s: float = 60.0,
                 queue_size: int = 32, backoff_max: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.pet_id = pet_id
        self.publish = publish
        self.status_fn = status_fn or (lambda: {})
        self.timeout = timeout
        self.talk_timeout = talk_timeout
        self.token = token
        self.heartbeat_s = heartbeat_s
        self.q: "queue.Queue[dict]" = queue.Queue(maxsize=queue_size)
        self.online: Optional[bool] = None
        self.last_error: Optional[str] = None
        self.sent = 0
        self.dropped = 0
        self.last_reply: Optional[dict] = None
        self._backoff = 0.0
        self._backoff_until = 0.0
        self.backoff_max = backoff_max
        self._stop = threading.Event()
        self._last_contact = 0.0
        self.thread: Optional[threading.Thread] = None

    # -- 主執行緒呼叫（都不會阻塞）--------------------------------------------------
    def start(self) -> None:
        self.thread = threading.Thread(target=self._worker, name="backend-client", daemon=True)
        self.thread.start()
        log.info("後端：%s（寵物 %s）", self.base_url, self.pet_id)

    def stop(self) -> None:
        self._stop.set()

    def send_event(self, ev: dict, state: Optional[str] = None, stage: Optional[int] = None) -> None:
        if time.monotonic() < self._backoff_until:
            self.dropped += 1       # 離線退避中：直接丟掉，不累積
            return
        body = {"pet_id": self.pet_id, "event": {k: v for k, v in ev.items() if not k.startswith("_")},
                "state": state, "stage": stage}
        self._enqueue({"kind": "event", "body": body})

    def talk(self, text: str, state: Optional[str] = None, stage: Optional[int] = None) -> None:
        body = {"pet_id": self.pet_id, "text": text, "state": state, "stage": stage}
        self._enqueue({"kind": "talk", "body": body})

    def _enqueue(self, job: dict) -> None:
        try:
            self.q.put_nowait(job)
        except queue.Full:
            try:
                self.q.get_nowait()        # 丟最舊的
                self.dropped += 1
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(job)
            except queue.Full:
                self.dropped += 1

    def status(self) -> dict:
        return {"url": self.base_url, "pet_id": self.pet_id, "online": self.online,
                "last_error": self.last_error, "sent": self.sent, "dropped": self.dropped,
                "queued": self.q.qsize()}

    # -- 背景執行緒 -----------------------------------------------------------------
    def _post(self, path: str, body: dict, timeout: float) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Pet-Token"] = self.token
        req = urllib.request.Request(self.base_url + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=headers, method="POST")
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _mark(self, ok: bool, err: Optional[str] = None) -> None:
        if ok:
            if self.online is not True:
                log.info("後端已連上")
            self.online, self.last_error, self._backoff, self._backoff_until = True, None, 0.0, 0.0
            self._last_contact = time.monotonic()
        else:
            if self.online is not False:
                log.warning("後端連不上（%s），寵物照常運作，稍後重試", err)
            self.online, self.last_error = False, err
            self._backoff = min(self.backoff_max, max(2.0, self._backoff * 2))
            self._backoff_until = time.monotonic() + self._backoff

    def apply(self, directives: List[dict]) -> int:
        n = 0
        for d in directives or []:
            ev = directive_to_event(d) if isinstance(d, dict) else None
            if ev:
                self.publish(ev)
                n += 1
        return n

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.q.get(timeout=0.5)
            except queue.Empty:
                job = None
            if job is None:
                # 心跳：讓後端算心情衰減，也可能回指令（例如太累了去睡覺）
                if (self.heartbeat_s and time.monotonic() - self._last_contact >= self.heartbeat_s
                        and time.monotonic() >= self._backoff_until):
                    st = self.status_fn() or {}
                    job = {"kind": "event", "body": {"pet_id": self.pet_id, "state": st.get("state"),
                                                     "stage": st.get("stage"),
                                                     "event": make_event("sys", "heartbeat", source="device")}}
                    self._last_contact = time.monotonic()
                else:
                    continue
            path = "/v1/pet/talk" if job["kind"] == "talk" else "/v1/pet/event"
            timeout = self.talk_timeout if job["kind"] == "talk" else self.timeout
            try:
                reply = self._post(path, job["body"], timeout)
                self._mark(True)
                self.sent += 1
                self.last_reply = reply
                self.apply(reply.get("directives", []))
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self._mark(False, str(getattr(exc, "reason", exc))[:120])
                if job["kind"] == "talk":   # 對話要有反應：本機說一句
                    self.publish(make_event("action", "say", source="backend-offline",
                                            params={"text": OFFLINE_LINES, "seconds": 3}))
