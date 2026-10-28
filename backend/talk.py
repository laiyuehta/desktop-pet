"""對話：用寵物人設叫 LLM 回一句話，嚴格要求 JSON {say, emotion, intent?}。

失敗（LLM 沒開、逾時、回覆不是合格 JSON）時重試一次；
第一次就花超過一半逾時時間的話不重試（避免讓裝置等太久），
最後還是失敗就用固定台詞（fallback），所以對話永遠有回覆。
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Dict, List, Optional

from .llm import EMOTIONS, INTENTS, LLMError, extract_json, validate_reply
from .router import Router

log = logging.getLogger("backend.talk")

STAGE_NAMES = {1: "種子", 2: "長腳", 3: "長耳朵"}
STATE_NAMES = {"idle": "待機", "happy": "開心", "bored": "無聊", "sleep": "睡覺"}

PERSONA = """你是一隻放在桌上的電子寵物，名字叫「種子」。
外型：金屬感的淡藍色小圓球機器人，球身有分割線，頭上有兩根呆毛天線，兩顆會發光的黃色圓燈眼，額頭一顆小菱形，小小的三角形嘴巴。
個性：可愛、好奇、有點黏人、反應單純，像小動物或小小孩；不會假裝自己是人類，也不會說自己是 AI 助理。
說話規則：
- 一律用繁體中文（台灣用語），一次只說一句，非常短（15 個字以內最好，最多 30 字）。
- 可以用「～」「！」，可以有可愛的狀聲詞，不要用 emoji。
- 不懂的東西就老實說不懂，用寵物的口吻。
- 不提供危險、成人或醫療法律建議；遇到這類問題就撒嬌帶過。
輸出格式：只輸出一個 JSON 物件，不要有其他文字：
{"say": "要說的話", "emotion": "EMOTIONS", "intent": "INTENTS"}
emotion 從 EMOTIONS 選一個；intent 可省略，預設 none（sleep=想睡覺、wake=醒來、nod=點頭、shake=搖頭、play=想玩）。"""

FALLBACK = {
    "default": [("嗯？", "normal"), ("咕嚕咕嚕～", "happy"), ("我在聽喔！", "happy"),
                ("嘿嘿，聽不太懂", "shy"), ("再說一次嘛～", "normal")],
    "greet": [("哈囉～", "happy"), ("你好呀！", "happy")],
    "sleep": [("呼…呼…", "sleepy")],
}


def persona_prompt() -> str:
    return (PERSONA.replace("EMOTIONS", " / ".join(EMOTIONS), 1)
            .replace("INTENTS", " / ".join(INTENTS), 1)
            .replace("EMOTIONS", "、".join(EMOTIONS)))


def intent_directives(intent: str) -> List[dict]:
    if intent == "sleep":
        return [{"type": "set_state", "state": "sleep"}]
    if intent == "wake":
        return [{"type": "set_state", "state": "idle"}]
    if intent == "play":
        return [{"type": "set_state", "state": "happy"}]
    if intent in ("nod", "shake"):
        return [{"type": "actuator", "motion": intent}]
    return []


class TalkService:
    def __init__(self, cfg: Dict[str, Any], router: Router, rng: Optional[random.Random] = None):
        self.cfg = cfg
        self.llm_cfg = cfg["llm"]
        self.router = router
        self.rng = rng or random.Random()

    def build_messages(self, rec: Dict[str, Any], text: str) -> List[Dict[str, str]]:
        m = rec["mood"]
        ctx = (f"（目前狀態：{STATE_NAMES.get(rec.get('device_state'), rec.get('device_state'))}；"
               f"成長階段：{STAGE_NAMES.get(rec['growth']['stage'], rec['growth']['stage'])}；"
               f"開心 {m['happiness']:.0f}/100、親密 {m['affection']:.0f}/100、精神 {m['energy']:.0f}/100）")
        msgs = [{"role": "system", "content": persona_prompt() + "\n" + ctx}]
        turns = [x for x in rec.get("memory", []) if x.get("kind") == "talk"]
        for t in turns[-int(self.llm_cfg.get("history_turns", 6)):]:
            msgs.append({"role": "user", "content": t["text"]})
            msgs.append({"role": "assistant", "content": json.dumps(
                {"say": t["reply"], "emotion": t.get("emotion", "normal")}, ensure_ascii=False)})
        msgs.append({"role": "user", "content": text})
        return msgs

    def fallback(self, text: str, rec: Dict[str, Any]) -> Dict[str, Any]:
        key = "default"
        if rec.get("device_state") == "sleep":
            key = "sleep"
        elif any(w in text for w in ("你好", "哈囉", "嗨", "hello", "hi")):
            key = "greet"
        say, emo = self.rng.choice(FALLBACK[key])
        return {"say": say, "emotion": emo, "intent": "none"}

    def reply(self, rec: Dict[str, Any], text: str) -> Dict[str, Any]:
        """回傳 {say, emotion, intent, source, latency_ms, error?}。"""
        t0 = time.perf_counter()
        provider = self.router.pick({"kind": "pet_talk", "text": text})
        timeout = float(self.llm_cfg["local"].get("timeout_s", 8))
        attempts = 1 + int(self.llm_cfg["local"].get("retries", 1))
        msgs = self.build_messages(rec, text)
        err = None
        for i in range(attempts):
            t_try = time.perf_counter()
            try:
                raw = provider.chat(msgs)
                out = validate_reply(extract_json(raw), int(self.llm_cfg.get("max_say_chars", 40)))
                out.update(source=provider.name, latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                           attempts=i + 1)
                return out
            except LLMError as exc:
                err = str(exc)
                log.warning("LLM 第 %d 次失敗：%s", i + 1, err)
                if time.perf_counter() - t_try > timeout / 2:
                    break   # 已經很慢了，不再重試
        out = self.fallback(text, rec)
        out.update(source="fallback", latency_ms=round((time.perf_counter() - t0) * 1000, 1), error=err)
        return out
