"""後端連線安全控管：速率限制、鎖定、信任代理與錯誤嘗試記錄。

- 來源 IP 判定：僅當連線來自 trusted_proxies 時，才信任 X-Forwarded-For 最右側之 IP。
- 速率限制：每把金鑰每分鐘上限 120 次請求；照片上傳每分鐘上限 10 次。
- 錯誤鎖定：同來源 IP 於 5 分鐘內累計 10 次錯誤，鎖定 15 分鐘（回傳 429）。
- 全域防護：全域於 1 分鐘內累計超過 100 次錯誤，拒絕未認證連線。
- 錯誤記錄：絕不記錄金鑰本體；未知金鑰只記 SHA-256 前 8 碼；同來源錯誤每分鐘聚合記錄。
"""

from __future__ import annotations

import collections
import ipaddress
import logging
import threading
import time
from typing import Any, Deque, Dict, List, Optional, Tuple

log = logging.getLogger("backend.security")


def parse_ip(ip_str: str) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return ipaddress.ip_address(ip_str.strip())
    except ValueError:
        return None


def is_ip_in_trusted(ip_str: str, trusted_list: List[str]) -> bool:
    target = parse_ip(ip_str)
    if not target:
        return False
    for t in trusted_list:
        t = t.strip()
        if not t:
            continue
        try:
            if "/" in t:
                if target in ipaddress.ip_network(t, strict=False):
                    return True
            else:
                tip = ipaddress.ip_address(t)
                if target == tip:
                    return True
        except ValueError:
            continue
    return False


def get_client_ip(peer_ip: str, headers: Any, trusted_proxies: Optional[List[str]] = None) -> str:
    """解析真實客戶端 IP。"""
    trusted = trusted_proxies or ["127.0.0.1", "::1"]
    if is_ip_in_trusted(peer_ip, trusted):
        xff = headers.get("X-Forwarded-For", "")
        if xff:
            # 依 RFC 7239，最靠近反向代理的客戶端 IP 位於最右側
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                candidate = parts[-1]
                if parse_ip(candidate):
                    return candidate
    return peer_ip


class SecurityGuard:
    """安全守護核心（執行緒安全，狀態僅存記憶體，重啟即清除）。"""

    def __init__(
        self,
        trusted_proxies: Optional[List[str]] = None,
        key_rate_limit: int = 120,
        photo_rate_limit: int = 10,
        lockout_threshold: int = 10,
        lockout_window_s: float = 300.0,
        lockout_duration_s: float = 900.0,
        global_error_threshold: int = 100,
        global_window_s: float = 60.0,
    ):
        self.trusted_proxies = trusted_proxies or ["127.0.0.1", "::1"]
        self.key_rate_limit = key_rate_limit
        self.photo_rate_limit = photo_rate_limit
        self.lockout_threshold = lockout_threshold
        self.lockout_window_s = lockout_window_s
        self.lockout_duration_s = lockout_duration_s
        self.global_error_threshold = global_error_threshold
        self.global_window_s = global_window_s

        self._lock = threading.Lock()

        # key_id -> Deque[float] (timestamps)
        self._key_req_times: Dict[str, Deque[float]] = collections.defaultdict(collections.deque)
        # key_id -> Deque[float] (photo timestamps)
        self._key_photo_times: Dict[str, Deque[float]] = collections.defaultdict(collections.deque)

        # ip -> Deque[float] (error timestamps)
        self._ip_errors: Dict[str, Deque[float]] = collections.defaultdict(collections.deque)
        # ip -> lockout_until (timestamp)
        self._ip_lockout: Dict[str, float] = {}

        # Deque[float] (all global error timestamps)
        self._global_errors: Deque[float] = collections.deque()

        # 錯誤聚合紀錄： (ip, reason) -> {"count": int, "first_seen": float, "last_log": float, "target": str}
        self._error_aggregates: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def resolve_ip(self, peer_ip: str, headers: Any) -> str:
        return get_client_ip(peer_ip, headers, self.trusted_proxies)

    def is_locked(self, ip: str, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.time()
        with self._lock:
            locked_until = self._ip_lockout.get(ip, 0.0)
            if t < locked_until:
                return True
            if locked_until > 0.0:
                del self._ip_lockout[ip]
            return False

    def is_global_strict(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.time()
        with self._lock:
            while self._global_errors and t - self._global_errors[0] > self.global_window_s:
                self._global_errors.popleft()
            return len(self._global_errors) >= self.global_error_threshold

    def check_rate_limit(self, key_id: str, is_photo: bool = False, now: Optional[float] = None) -> bool:
        """檢查速率限制。若通過則記錄並回傳 True；若超標回傳 False。"""
        t = now if now is not None else time.time()
        with self._lock:
            # 1. 照片額外限制
            if is_photo:
                dq_p = self._key_photo_times[key_id]
                while dq_p and t - dq_p[0] > 60.0:
                    dq_p.popleft()
                if len(dq_p) >= self.photo_rate_limit:
                    return False
                dq_p.append(t)

            # 2. 一般請求限制
            dq_r = self._key_req_times[key_id]
            while dq_r and t - dq_r[0] > 60.0:
                dq_r.popleft()
            if len(dq_r) >= self.key_rate_limit:
                return False
            dq_r.append(t)
            return True

    def record_error(
        self,
        ip: str,
        reason: str,
        path: str = "",
        method: str = "GET",
        key_id: Optional[str] = None,
        token_hash_prefix: Optional[str] = None,
        now: Optional[float] = None,
    ) -> bool:
        """記錄認證或存取錯誤。回傳是否因此次錯誤觸發了新鎖定。"""
        t = now if now is not None else time.time()
        triggered_lock = False

        with self._lock:
            # 1. 記錄全域錯誤
            self._global_errors.append(t)

            # 2. 記錄特定 IP 錯誤與鎖定判斷
            dq = self._ip_errors[ip]
            while dq and t - dq[0] > self.lockout_window_s:
                dq.popleft()
            dq.append(t)

            if len(dq) >= self.lockout_threshold:
                self._ip_lockout[ip] = t + self.lockout_duration_s
                triggered_lock = True
                dq.clear()

            # 3. 聚合日誌輸出
            agg_key = (ip, reason)
            target = f"key={key_id}" if key_id else (f"hash={token_hash_prefix}" if token_hash_prefix else "none")
            entry = self._error_aggregates.get(agg_key)
            if not entry:
                entry = {"count": 1, "first_seen": t, "last_log": t, "target": target, "path": path}
                self._error_aggregates[agg_key] = entry
                log.warning(
                    "[安全防護] 來源 IP %s: %s %s 存取被拒 (原因: %s, 對象: %s)",
                    ip, method, path, reason, target
                )
            else:
                entry["count"] += 1
                # 每分鐘合併輸出一次
                if t - entry["last_log"] >= 60.0:
                    log.warning(
                        "[安全防護] 來源 IP %s: 過去 1 分鐘累計 %d 次 %s（最後路徑: %s, 對象: %s）",
                        ip, entry["count"], reason, path, target
                    )
                    entry["count"] = 0
                    entry["last_log"] = t

        if triggered_lock:
            log.warning(
                "[安全防護] 來源 IP %s 於 5 分鐘內錯誤達 %d 次，已鎖定 15 分鐘",
                ip, self.lockout_threshold
            )
        return triggered_lock
