"""每隻寵物一個 JSON 存檔（data/<pet_id>.json），原子寫入。

原子寫入：先寫同資料夾的暫存檔 → flush + fsync → os.replace 覆蓋。
Windows 上檔案被防毒或其他程式短暫鎖住時 os.replace 會丟 PermissionError，
所以重試幾次。
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import time
from typing import Any, Dict

PET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SCHEMA_VERSION = 1


def valid_pet_id(pet_id: Any) -> bool:
    return isinstance(pet_id, str) and bool(PET_ID_RE.match(pet_id))


def new_record(pet_id: str, neutral: Dict[str, float]) -> Dict[str, Any]:
    now = time.time()
    return {
        "schema": SCHEMA_VERSION,
        "pet_id": pet_id,
        "created": now,
        "last_seen": None,          # 最後一次收到裝置訊息的時間
        "last_update": now,         # 心情最後一次計算衰減的時間
        "device_state": "idle",     # 裝置回報的目前狀態
        "growth": {"value": 0.0, "stage": 1},
        "mood": {"happiness": float(neutral.get("happiness", 50)),
                 "affection": float(neutral.get("affection", 30)),
                 "energy": float(neutral.get("energy", 70))},
        "counters": {},             # 各種事件次數
        "memory": [],               # 最近互動（短期記憶）
        "last_chatter": 0.0,
    }


def atomic_write_json(path: str, data: Any, retries: int = 5) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        for i in range(retries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == retries - 1:
                    raise
                time.sleep(0.05 * (i + 1))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


class PetStore:
    def __init__(self, data_dir: str, neutral: Dict[str, float], memory_size: int = 20):
        self.data_dir = data_dir
        self.neutral = neutral
        self.memory_size = memory_size
        self._locks: Dict[str, threading.RLock] = {}
        self._guard = threading.Lock()
        os.makedirs(data_dir, exist_ok=True)

    def lock(self, pet_id: str) -> threading.RLock:
        """同一隻寵物的讀改寫要包在 with store.lock(pet_id) 裡。"""
        with self._guard:
            return self._locks.setdefault(pet_id, threading.RLock())

    def path(self, pet_id: str) -> str:
        if not valid_pet_id(pet_id):
            raise ValueError("pet_id 只能用英數字、底線、連字號（1~64 字）")
        return os.path.join(self.data_dir, f"{pet_id}.json")

    def exists(self, pet_id: str) -> bool:
        return os.path.exists(self.path(pet_id))

    def load(self, pet_id: str) -> Dict[str, Any]:
        p = self.path(pet_id)
        base = new_record(pet_id, self.neutral)
        if not os.path.exists(p):
            return base
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # 舊存檔缺欄位時補預設值
        for k, v in base.items():
            if k not in data:
                data[k] = copy.deepcopy(v)
            elif isinstance(v, dict) and isinstance(data[k], dict):
                for k2, v2 in v.items():
                    data[k].setdefault(k2, v2)
        return data

    def save(self, rec: Dict[str, Any]) -> None:
        rec["memory"] = rec.get("memory", [])[-self.memory_size:]
        atomic_write_json(self.path(rec["pet_id"]), rec)

    def remember(self, rec: Dict[str, Any], item: Dict[str, Any]) -> None:
        item = dict(item)
        item.setdefault("ts", time.time())
        rec.setdefault("memory", []).append(item)
        del rec["memory"][:-self.memory_size]
