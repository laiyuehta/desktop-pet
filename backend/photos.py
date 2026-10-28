"""照片存放（camera.snapshot 能力模組上傳的 JPEG）。

- 存在 <data_dir>/photos/<pet_id>/<photo_id>.jpg，photo_id 用拍攝時間（例如 20261028-213015-123）。
- 每張旁邊一個小的 <photo_id>.json（拍攝時間、原因、大小），存檔的「最近照片」清單也會記一份。
- 保留最近 N 張（photos.retention，預設 200），超過自動刪最舊的。
- 照片內容不寫進日誌；分析（看得懂圖的模型）之後再接：register_hook(fn(pet_id, meta, path))。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .store import atomic_write_json, valid_pet_id

log = logging.getLogger("backend.photos")
PHOTO_ID_RE = re.compile(r"^\d{8}-\d{6}-\d{3}(?:-\d+)?$")
MAX_PHOTO_BYTES = 8 * 1024 * 1024
REASON_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


def valid_photo_id(pid: Any) -> bool:
    return isinstance(pid, str) and bool(PHOTO_ID_RE.match(pid))


class PhotoStore:
    def __init__(self, data_dir: str, retention: int = 200):
        self.root = os.path.join(data_dir, "photos")
        self.retention = max(1, int(retention))
        self._lock = threading.Lock()
        self.hooks: List[Callable[[str, Dict[str, Any], str], None]] = []

    def register_hook(self, fn: Callable[[str, Dict[str, Any], str], None]) -> None:
        """之後接圖片分析用（目前沒有任何 hook）。"""
        self.hooks.append(fn)

    def _dir(self, pet_id: str) -> str:
        if not valid_pet_id(pet_id):
            raise ValueError("pet_id 不合法")
        return os.path.join(self.root, pet_id)

    def save(self, pet_id: str, data: bytes, reason: str = "manual", taken: Optional[float] = None) -> Dict[str, Any]:
        if not data.startswith(b"\xff\xd8") or len(data) < 64:
            raise ValueError("內容不是 JPEG")
        if len(data) > MAX_PHOTO_BYTES:
            raise ValueError("照片太大")
        reason = reason if isinstance(reason, str) and REASON_RE.match(reason) else "unknown"
        now = time.time()
        if not isinstance(taken, (int, float)) or not (now - 86400 * 30 < taken < now + 3600):
            taken = now
        d = self._dir(pet_id)
        with self._lock:
            os.makedirs(d, exist_ok=True)
            lt = time.localtime(taken)
            base = time.strftime("%Y%m%d-%H%M%S", lt) + "-%03d" % int((taken % 1) * 1000)
            pid, i = base, 1
            while os.path.exists(os.path.join(d, pid + ".jpg")):
                i += 1
                pid = f"{base}-{i}"
            tmp = os.path.join(d, f".tmp-{pid}.jpg")
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, os.path.join(d, pid + ".jpg"))
            meta = {"photo_id": pid, "pet_id": pet_id, "taken": taken, "received": now, "reason": reason,
                    "bytes": len(data)}
            atomic_write_json(os.path.join(d, pid + ".json"), meta)
            removed = self._prune(d)
        log.info("收到照片 %s/%s（%d KB，%s）%s", pet_id, pid, len(data) // 1024, reason,
                 f"，刪掉最舊的 {removed} 張" if removed else "")
        for fn in self.hooks:
            try:
                fn(pet_id, meta, os.path.join(d, pid + ".jpg"))
            except Exception:   # noqa: BLE001
                log.exception("照片 hook 失敗")
        return meta

    def _ids(self, d: str) -> List[str]:
        try:
            return sorted(f[:-4] for f in os.listdir(d) if f.endswith(".jpg") and valid_photo_id(f[:-4]))
        except OSError:
            return []

    def _prune(self, d: str) -> int:
        ids = self._ids(d)
        extra = ids[:-self.retention] if len(ids) > self.retention else []
        for pid in extra:
            for ext in (".jpg", ".json"):
                try:
                    os.remove(os.path.join(d, pid + ext))
                except OSError:
                    pass
        return len(extra)

    def list(self, pet_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        d = self._dir(pet_id)
        out = []
        for pid in reversed(self._ids(d)[-max(1, min(int(limit), self.retention)):]):
            try:
                with open(os.path.join(d, pid + ".json"), "r", encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (OSError, ValueError):
                out.append({"photo_id": pid, "pet_id": pet_id})
        return out

    def get(self, pet_id: str, photo_id: str) -> Optional[bytes]:
        if not valid_photo_id(photo_id):
            return None
        p = os.path.join(self._dir(pet_id), photo_id + ".jpg")
        try:
            with open(p, "rb") as fh:
                return fh.read()
        except OSError:
            return None

    def count(self, pet_id: str) -> int:
        return len(self._ids(self._dir(pet_id)))
