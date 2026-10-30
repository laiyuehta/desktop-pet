"""裝置端的後端（大腦）客戶端。

- 背景執行緒送事件到後端，主迴圈完全不等網路（send_event 只是放進佇列）。
- 每個請求逾時很短（預設 2 秒；對話因為要等 LLM，預設 15 秒）。
- 後端連不上時安靜降級：記錄一次 log、暫停送事件一段時間（退避），佇列滿了丟最舊的；
  寵物照常待機、眨眼、睡覺。
- 後端回傳的指令（directives）轉成既有的事件丟回事件匯流排，由 PetApp 照原本的流程處理：
    set_state / set_stage → control 事件
    say / emotion / actuator → action 事件（say 會在畫面上顯示對話框）
    skill（例如 camera.snapshot）→ skill 事件，由對應的能力模組處理
"""

from __future__ import annotations

import functools
import ipaddress
import json
import logging
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from .events import make_event

log = logging.getLogger("pet.backend")


def _is_private_or_local(ip: ipaddress._BaseAddress) -> bool:
    if ip.is_loopback:
        return True
    if ip.version == 4:
        # RFC 1918: 10/8, 172.16/12, 192.168/16; RFC 3927: 169.254/16
        b = ip.packed
        if b[0] == 10:
            return True
        if b[0] == 172 and 16 <= b[1] <= 31:
            return True
        if b[0] == 192 and b[1] == 168:
            return True
        if b[0] == 169 and b[1] == 254:
            return True
        return False
    if ip.version == 6:
        # ULA (fc00::/7) or link-local (fe80::/10)
        b = ip.packed
        if (b[0] & 0xFE) == 0xFC:
            return True
        if b[0] == 0xFE and (b[1] & 0xC0) == 0x80:
            return True
        return False
    return False


_SAFETY_CACHE: Dict[Tuple[str, bool], Tuple[float, bool]] = {}


def is_safe_for_insecure_token(url: str, allow_insecure_lan: bool = False) -> bool:
    """檢查目標後端網址是否安全可送出金鑰（HTTPS、localhost 或 RFC 1918 私有 IP）。"""
    if allow_insecure_lan:
        return True
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "https":
            return True
        host = parsed.hostname or ""
        if not host:
            return True
        if host.lower() in ("localhost", "127.0.0.1", "::1"):
            return True
        try:
            ip = ipaddress.ip_address(host)
            return _is_private_or_local(ip)
        except ValueError:
            pass

        now = time.monotonic()
        cache_key = (host.lower(), allow_insecure_lan)
        cached = _SAFETY_CACHE.get(cache_key)
        if cached is not None:
            ts, is_safe = cached
            # 成功結果快取 300 秒（失敗結果絕不快取，避免開機網路未就緒而永久阻斷）
            if now - ts < 300.0:
                return is_safe

        try:
            addr = socket.gethostbyname(host)
            ip = ipaddress.ip_address(addr)
            is_safe = _is_private_or_local(ip)
            if is_safe:
                _SAFETY_CACHE[cache_key] = (now, True)
            return is_safe
        except Exception as exc:
            log.warning("解析後端主機名稱失敗 (%s)：%s", host, exc)
            return False
    except Exception:
        return False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 後端情緒 → 裝置表情（pet/eyes.py 的 EXPRESSIONS）
EMOTION_TO_EXPRESSION = {
    "happy": "happy", "normal": "normal", "surprised": "surprised", "sleepy": "sleepy",
    "bored": "bored", "sad": "sad", "shy": "squint",
}


def load_emotion_levels(config_path: Optional[str] = None) -> Dict[str, int]:
    """從狀態設定檔讀取情緒等級上限，作為單一來源。未指定時依序讀取 PET_CONFIG 或 repo 的 config/states.json。"""
    path = config_path or os.environ.get("PET_CONFIG") or os.path.join(ROOT, "config", "states.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        levels = data.get("emotions", {}).get("levels")
        if not levels:
            log.warning("設定檔 (%s) 未設定 emotions.levels", path)
            return {}
        return {str(k): int(v) for k, v in levels.items()}
    except Exception as exc:
        log.warning("載入情緒等級設定失敗 (%s)：%s", path, exc)
        return {}


_DEFAULT_LEVELS_CACHE: Optional[Tuple[Optional[str], float, Dict[str, int]]] = None


def get_default_emotion_levels() -> Dict[str, int]:
    """依 PET_CONFIG 或 repo 預設狀態檔解析情緒等級；快取結果避免指令重複讀檔與警告洗版。"""
    global _DEFAULT_LEVELS_CACHE
    env_cfg = os.environ.get("PET_CONFIG")
    target_path = env_cfg if env_cfg else os.path.join(ROOT, "config", "states.json")
    try:
        mtime = os.path.getmtime(target_path)
    except OSError:
        mtime = -1.0

    if _DEFAULT_LEVELS_CACHE is not None:
        cached_path, cached_mtime, cached_levels = _DEFAULT_LEVELS_CACHE
        if cached_path == target_path and cached_mtime == mtime:
            return cached_levels

    levels = load_emotion_levels(env_cfg) if env_cfg else {}
    if not levels:
        repo_default = os.path.join(ROOT, "config", "states.json")
        levels = load_emotion_levels(repo_default)

    _DEFAULT_LEVELS_CACHE = (target_path, mtime, levels)
    return levels


def __getattr__(name: str) -> Any:
    if name == "EMOTION_MAX_LEVEL":
        return get_default_emotion_levels()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


OFFLINE_LINES = "嗯…？（大腦在休息）"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def emotion_state_name(emotion: str, level: Any, max_levels: Optional[Dict[str, int]] = None) -> Optional[str]:
    """把 (emotion, level) 轉成對應的情緒強度狀態名稱；不認得或等級不合法回傳 None。"""
    levels = max_levels if max_levels is not None else get_default_emotion_levels()
    max_lv = levels.get(emotion)
    if max_lv is None or isinstance(level, bool):
        return None
    try:
        lv = int(level)
    except (TypeError, ValueError):
        return None
    if lv < 1:
        return None
    lv = min(max_lv, lv)
    return emotion if max_lv == 1 else f"{emotion}_{lv}"


def directive_to_event(d: Dict[str, Any], max_levels: Optional[Dict[str, int]] = None) -> Optional[dict]:
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
            emo = str(d.get("emotion"))
            if d.get("level") is not None:
                st = emotion_state_name(emo, d.get("level"), max_levels=max_levels)
                if not st:
                    log.warning("忽略未知或無效的情緒等級指令：%s (level=%s)", emo, d.get("level"))
                    return None
                return make_event("control", "set_state", source=src, state=st,
                                  emotion=emo, level=int(d["level"]))
            expr = EMOTION_TO_EXPRESSION.get(emo, None)
            if not expr:
                return None
            return make_event("action", "expression", source=src,
                              params={"expression": expr, "seconds": float(d.get("seconds", 2))})
        if t == "skill":
            name = str(d.get("name") or "")
            if not name:
                return None
            params = {k: v for k, v in d.items() if k not in ("type", "name")}
            params.setdefault("reason", "backend")
            return make_event("skill", name, source=src, params=params)
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
                 queue_size: int = 32, backoff_max: float = 60.0,
                 max_levels: Optional[Dict[str, int]] = None,
                 token_file: Optional[str] = None,
                 allow_insecure_lan: bool = False):
        self.base_url = base_url.rstrip("/")
        self.pet_id = pet_id
        self.publish = publish
        self.status_fn = status_fn or (lambda: {})
        self.timeout = timeout
        self.talk_timeout = talk_timeout
        self.token = token
        self.token_file = token_file
        self.allow_insecure_lan = allow_insecure_lan
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
        self.max_levels = max_levels
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
    def _post_request(self, path: str, body: dict, timeout: float, token: str) -> dict:
        headers = {"Content-Type": "application/json"}
        if token and is_safe_for_insecure_token(self.base_url, self.allow_insecure_lan):
            headers["X-Pet-Token"] = token
        elif token:
            log.warning("非 HTTPS 且非私有 IP/localhost，拒絕送出金鑰（可設定 --allow-insecure-lan 放行）：%s", self.base_url)
        req = urllib.request.Request(self.base_url + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=headers, method="POST")
        with _OPENER.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, path: str, body: dict, timeout: float) -> dict:
        try:
            return self._post_request(path, body, timeout, self.token)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and self.token_file:
                try:
                    exp_path = os.path.expanduser(self.token_file)
                    if os.path.isfile(exp_path):
                        with open(exp_path, "r", encoding="utf-8") as fh:
                            new_tok = fh.read().strip()
                        if new_tok and new_tok != self.token:
                            log.info("收到 401，已自 %s 重新載入新金鑰並重試一次", exp_path)
                            self.token = new_tok
                            return self._post_request(path, body, timeout, self.token)
                except Exception as read_exc:
                    log.warning("重新讀取金鑰檔失敗：%s", read_exc)
            raise

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
            ev = directive_to_event(d, max_levels=self.max_levels) if isinstance(d, dict) else None
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
