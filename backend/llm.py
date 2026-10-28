"""OpenAI 相容的聊天客戶端（只用 urllib），以及回覆的清理與驗證。

支援 llama.cpp 的 llama-server、Ollama、LM Studio 等 OpenAI 相容服務。
Qwen3 預設會「思考」：no_think=True 時在使用者訊息後加 " /no_think"，
並送 chat_template_kwargs.enable_thinking=false；回覆裡殘留的 <think>…</think> 也會刪掉。
"""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

EMOTIONS = ["happy", "normal", "surprised", "sleepy", "bored", "sad", "shy"]
INTENTS = ["none", "sleep", "wake", "nod", "shake", "play"]

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


# 不走系統 proxy（Windows 有設 proxy 時，連 127.0.0.1 也可能被導去 proxy）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LLMError(Exception):
    """LLM 連不上、逾時、HTTP 錯誤或回覆格式錯誤。"""


def strip_think(text: str) -> str:
    """刪掉 <think>…</think>；若只有開頭沒有結尾（被截斷），刪掉 <think> 之後全部。"""
    text = _THINK_RE.sub("", text or "")
    low = text.lower()
    if "<think>" in low:
        text = text[:low.index("<think>")]
    return text.replace("</think>", "").strip()


def extract_json(text: str) -> Dict[str, Any]:
    """從模型輸出中取出第一個完整的 JSON 物件（允許前後有雜訊、```json 區塊）。"""
    text = strip_think(text)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = text.find("{", start + 1)
    raise LLMError("回覆裡找不到 JSON 物件")


def validate_reply(obj: Dict[str, Any], max_chars: int = 40) -> Dict[str, Any]:
    """驗證 {say, emotion, intent?}：say 必須是非空字串；emotion/intent 不在清單就換成預設。"""
    say = obj.get("say")
    if not isinstance(say, str) or not say.strip():
        raise LLMError("JSON 缺少 say 字串")
    say = " ".join(strip_think(say).split())
    if not say:
        raise LLMError("say 是空的")
    if len(say) > max_chars:
        say = say[:max_chars - 1] + "…"
    emotion = obj.get("emotion")
    emotion = emotion if emotion in EMOTIONS else "normal"
    intent = obj.get("intent")
    intent = intent if intent in INTENTS else "none"
    return {"say": say, "emotion": emotion, "intent": intent}


class OpenAICompatClient:
    def __init__(self, base_url: str, model: str, timeout_s: float = 8.0, temperature: float = 0.7,
                 max_tokens: int = 160, json_mode: bool = True, no_think: bool = True,
                 api_key: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = float(timeout_s)
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.json_mode = json_mode
        self.no_think = no_think
        self.api_key = api_key

    @classmethod
    def from_config(cls, c: Dict[str, Any]) -> "OpenAICompatClient":
        return cls(c["base_url"], c["model"], c.get("timeout_s", 8), c.get("temperature", 0.7),
                   c.get("max_tokens", 160), c.get("json_mode", True), c.get("no_think", True))

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def chat(self, messages: List[Dict[str, str]]) -> str:
        """送出對話，回傳助理文字（已去掉 think）。失敗丟 LLMError。"""
        msgs = [dict(m) for m in messages]
        if self.no_think:
            for m in reversed(msgs):
                if m["role"] == "user":
                    if not m["content"].rstrip().endswith("/no_think"):
                        m["content"] = m["content"].rstrip() + " /no_think"
                    break
        body: Dict[str, Any] = {"model": self.model, "messages": msgs, "temperature": self.temperature,
                                "max_tokens": self.max_tokens, "stream": False}
        if self.no_think:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(self.base_url + "/chat/completions",
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=self._headers(), method="POST")
        try:
            with _OPENER.open(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LLMError(f"HTTP {exc.code}") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise LLMError(f"連線失敗：{exc}") from exc
        except json.JSONDecodeError as exc:
            raise LLMError("伺服器回覆不是 JSON") from exc
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("回覆格式不是 OpenAI chat completions") from exc
        return strip_think(content)

    def ping(self, timeout_s: float = 2.0) -> Dict[str, Any]:
        """檢查 LLM 服務是否可連（GET /models）。"""
        t0 = time.perf_counter()
        req = urllib.request.Request(self.base_url + "/models", headers=self._headers())
        try:
            with _OPENER.open(req, timeout=timeout_s) as resp:
                ok = 200 <= resp.status < 300
                payload = json.loads(resp.read().decode("utf-8") or "{}")
            models = [m.get("id") for m in payload.get("data", []) if isinstance(m, dict)]
            return {"reachable": ok, "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "models": models[:10]}
        except Exception as exc:  # noqa: BLE001 健康檢查不能讓伺服器出錯
            return {"reachable": False, "error": str(exc)[:200]}
