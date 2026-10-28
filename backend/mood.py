"""心情與成長（規則式 stub，數值與規則都在 config.json，之後可以換掉）。

- 心情值 0~100：happiness（開心）、affection（親密）、energy（精神）
- 隨時間：happiness、affection 往中性值衰減；醒著 energy 下降、睡覺時回復
- 互動：config.mood.events 裡的加成（pat、poke、talk……），同時累積成長值
- 成長：成長值只累積；auto_advance 預設關閉（規則未定），開啟才依門檻升階段
"""

from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Optional

# 互動時偶爾說的固定台詞（不經 LLM）
CHATTER = {
    "pat": ["好舒服～", "再摸一下嘛", "嘿嘿", "喜歡被摸頭！"],
    "poke": ["哎呀！", "幹嘛戳我啦", "嗯？"],
    "press": ["嗶！", "按到我了～"],
    "person_near": ["你來啦！", "哈囉～"],
    "wake": ["早安…", "嗯…我醒了"],
}

EMOTION_FOR_EVENT = {"pat": "happy", "poke": "surprised", "person_near": "happy"}


def clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


class MoodEngine:
    def __init__(self, cfg: Dict[str, Any], rng: Optional[random.Random] = None):
        self.cfg = cfg
        self.mood_cfg = cfg["mood"]
        self.growth_cfg = cfg["growth"]
        self.chatter_cfg = cfg.get("chatter", {})
        self.rng = rng or random.Random()

    # -- 時間衰減 ---------------------------------------------------------------
    def decay(self, rec: Dict[str, Any], now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        hours = max(0.0, (now - rec.get("last_update", now)) / 3600.0)
        rec["last_update"] = now
        if hours <= 0:
            return
        m, neutral = rec["mood"], self.mood_cfg["neutral"]
        d = self.mood_cfg["decay_per_hour"]
        for key in ("happiness", "affection"):
            target = neutral[key]
            step = d[key] * hours
            if m[key] > target:
                m[key] = max(target, m[key] - step)
            else:
                m[key] = min(target, m[key] + step)
        if rec.get("device_state") == "sleep":
            m["energy"] = clamp(m["energy"] + self.mood_cfg["energy_recover_per_hour_sleep"] * hours)
        else:
            m["energy"] = clamp(m["energy"] - d["energy_awake"] * hours)

    # -- 事件 -------------------------------------------------------------------
    def apply_event(self, rec: Dict[str, Any], event: Dict[str, Any], now: Optional[float] = None) -> List[dict]:
        """更新心情/成長，回傳要給裝置的指令列表。"""
        now = time.time() if now is None else now
        name = str(event.get("name", ""))
        rec["counters"][name] = rec["counters"].get(name, 0) + 1
        directives: List[dict] = []
        bonus = self.mood_cfg["events"].get(name)
        if bonus:
            for key in ("happiness", "affection", "energy"):
                if key in bonus:
                    rec["mood"][key] = clamp(rec["mood"][key] + bonus[key])
            directives += self.add_growth(rec, float(bonus.get("growth", 0.0)))
            directives += self._chatter(rec, name, now)
        # 精神太低又醒著 → 建議去睡覺（只在心跳時判斷，且 30 分鐘最多一次，
        # 避免使用者剛叫醒又被送回去睡）
        low = self.mood_cfg.get("sleep_when_energy_below")
        if (low is not None and name == "heartbeat" and rec["mood"]["energy"] < low
                and rec.get("device_state") in ("idle", "bored")
                and (rec.get("last_sleep_hint") is None
                     or now - rec["last_sleep_hint"] > float(self.mood_cfg.get("sleep_hint_cooldown_s", 1800)))):
            rec["last_sleep_hint"] = now
            directives.append({"type": "say", "text": "好睏喔…", "seconds": 3})
            directives.append({"type": "set_state", "state": "sleep"})
        return directives

    def _chatter(self, rec: Dict[str, Any], name: str, now: float) -> List[dict]:
        c = self.chatter_cfg
        lines = CHATTER.get(name)
        if not c.get("enabled", True) or not lines or rec.get("device_state") == "sleep":
            return []
        if now - rec.get("last_chatter", 0.0) < float(c.get("cooldown_s", 45)):
            return []
        if self.rng.random() >= float(c.get("chance", 0.35)):
            return []
        rec["last_chatter"] = now
        out = [{"type": "say", "text": self.rng.choice(lines), "seconds": 3}]
        if name in EMOTION_FOR_EVENT:
            out.append({"type": "emotion", "emotion": EMOTION_FOR_EVENT[name], "seconds": 2})
        return out

    # -- 成長 -------------------------------------------------------------------
    def add_growth(self, rec: Dict[str, Any], amount: float) -> List[dict]:
        g = rec["growth"]
        g["value"] = round(g["value"] + amount, 3)
        return self.check_stage(rec)

    def check_stage(self, rec: Dict[str, Any]) -> List[dict]:
        """升階段規則（placeholder）：auto_advance 開啟且成長值達門檻才升。"""
        if not self.growth_cfg.get("auto_advance", False):
            return []
        g = rec["growth"]
        thresholds = sorted((int(k), float(v)) for k, v in self.growth_cfg.get("thresholds", {}).items())
        new_stage = g["stage"]
        for stage, need in thresholds:
            if g["value"] >= need and stage > new_stage:
                new_stage = stage
        if new_stage == g["stage"]:
            return []
        g["stage"] = new_stage
        return [{"type": "set_stage", "stage": new_stage},
                {"type": "say", "text": "我好像長大了！", "seconds": 4},
                {"type": "actuator", "motion": "nod", "times": 2}]
