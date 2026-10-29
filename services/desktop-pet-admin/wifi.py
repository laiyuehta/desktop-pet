"""Wi‑Fi 設定（網頁遙控用）：透過 NetworkManager 的 nmcli 掃描、連線、記錄、救援熱點。

規格見 PROPOSAL.md「網頁遙控：Wi‑Fi 設定」。重點：
- 只動 Wi‑Fi 介面（預設 wlan0）與 Wi‑Fi 連線設定；有線網路（eth0）只讀取顯示，不改。
- 已記錄的網路就是 NetworkManager 的連線設定；優先順序 = autoconnect-priority，上次連線 = connection.timestamp。
- 密碼只透過 stdin 交給 `nmcli connection edit`（不出現在指令列、日誌、API 回應）。
- 桌寵以一般使用者執行，權限由 polkit 規則開放（tools/wifi/50-desktop-pet-wifi.rules），不用 sudo。
- WifiManager 在桌寵程式裡跑一個背景執行緒：斷線時依優先順序自動嘗試、從網頁換網路失敗自動切回、
  一直連不上就開設定熱點（預設 PetRescue-AP，密碼在 Pi 的設定檔，不進 repo）。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid as uuidlib
from typing import Callable, Dict, List, Optional, Tuple

try:
    import errno
    import fcntl
except ImportError:
    fcntl = None
    errno = None

log = logging.getLogger("pet.wifi")

DEFAULT_HOTSPOT_CONF = "/etc/desktop-pet/wifi-hotspot.conf"
DEFAULT_WIFI_LOCK = "/run/desktop-pet/wifi.lock"
HOTSPOT_SSID = "PetRescue-AP"
HOTSPOT_PORT = 8080
# 熱點網址：熱點開著時用 wlan0 實際拿到的 IP（NetworkManager 共用模式的閘道）組出來，不寫死；
# 查不到 IP 時才用 PET_HOTSPOT_URL（可不設）。
HOTSPOT_URL_FALLBACK = os.environ.get("PET_HOTSPOT_URL", "")
WIFI_TYPE = "802-11-wireless"
PRIORITY_BASE = 200
PRIORITY_STEP = 10


class NmError(Exception):
    def __init__(self, msg: str, kind: str = "failed"):
        super().__init__(msg)
        self.kind = kind


def split_terse(line: str) -> List[str]:
    """拆 `nmcli -t` 的一行：欄位用 : 分隔，值裡的 : 和 \\ 會被跳脫成 \\: 和 \\\\。"""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def classify_error(text: str) -> str:
    t = (text or "").lower()
    if "secrets were required" in t or "no secrets" in t or "802-1x supplicant" in t \
            or "invalid key" in t or ("psk" in t and "invalid" in t):
        return "wrong_password"
    if "no network with ssid" in t or "could not be found" in t or ("not found" in t and "ssid" in t):
        return "not_found"
    if "timeout" in t or "timed out" in t:
        return "timeout"
    if "not authorized" in t or "insufficient privileges" in t or "permission" in t:
        return "not_authorized"
    return "failed"


RESULT_TEXT = {
    "ok": "連線成功", "wrong_password": "密碼錯誤", "not_found": "找不到這個網路", "timeout": "連線逾時",
    "not_authorized": "沒有權限（Pi 上的 polkit 規則還沒裝？）", "failed": "連線失敗", "busy": "正在處理別的連線",
}


class NM:
    """nmcli 的薄包裝：組指令、解析輸出。runner 可以換成假的（測試用）。"""

    def __init__(self, binary: str = "nmcli", runner: Optional[Callable] = None):
        self.binary = binary
        self.runner = runner or self._run

    def _run(self, args: List[str], input_text: Optional[str] = None, timeout: float = 60) -> Tuple[int, str, str]:
        try:
            p = subprocess.run([self.binary] + args, input=input_text, capture_output=True, text=True,
                               timeout=timeout, env=dict(os.environ, LC_ALL="C", LANG="C"))
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "timeout expired"
        except OSError as exc:
            return 127, "", str(exc)

    def run(self, args: List[str], input_text: Optional[str] = None, timeout: float = 60, check: bool = True) -> str:
        rc, out, err = self.runner(args, input_text=input_text, timeout=timeout)
        log.debug("nmcli %s → %s", " ".join(args), rc)       # args 裡不會有密碼
        if rc != 0 and check:
            msg = (err or out or f"nmcli 結束碼 {rc}").strip()
            raise NmError(msg, classify_error(msg))
        return out

    def rows(self, args: List[str], **kw) -> List[List[str]]:
        return [split_terse(l) for l in self.run(["-t"] + args, **kw).splitlines() if l.strip()]

    # -- 查詢 ------------------------------------------------------------------
    def connections(self) -> List[dict]:
        out = []
        for r in self.rows(["-f", "NAME,UUID,TYPE,AUTOCONNECT-PRIORITY,TIMESTAMP,ACTIVE,DEVICE", "connection", "show"]):
            if len(r) < 7:
                continue
            out.append({"name": r[0], "uuid": r[1], "type": r[2], "priority": int(r[3] or 0) if r[3].lstrip("-").isdigit() else 0,
                        "timestamp": int(r[4]) if r[4].isdigit() else 0, "active": r[5] == "yes", "device": r[6]})
        return out

    def wifi_info(self, uuid: str) -> dict:
        r = self.rows(["-f", "802-11-wireless.ssid,802-11-wireless.mode,802-11-wireless.hidden,"
                             "802-11-wireless-security.key-mgmt", "connection", "show", uuid])
        d = {}
        for row in r:
            if len(row) >= 2:
                d[row[0]] = row[1]
        return {"ssid": d.get("802-11-wireless.ssid", ""), "mode": d.get("802-11-wireless.mode", "infrastructure") or "infrastructure",
                "hidden": d.get("802-11-wireless.hidden") == "yes",
                "secured": bool(d.get("802-11-wireless-security.key-mgmt"))}

    def devices(self) -> List[dict]:
        return [{"device": r[0], "type": r[1], "state": r[2], "connection": r[3]}
                for r in self.rows(["-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"]) if len(r) >= 4]

    def device_ip(self, dev: str) -> Optional[str]:
        for r in self.rows(["-f", "IP4.ADDRESS", "device", "show", dev], check=False):
            if len(r) >= 2 and r[1]:
                return r[1].split("/")[0]
        return None

    def wifi_list(self, iface: str, rescan: bool) -> List[dict]:
        args = ["-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN", "device", "wifi", "list", "ifname", iface,
                "--rescan", "yes" if rescan else "no"]
        out = []
        for r in self.rows(args, timeout=30):
            if len(r) < 5:
                continue
            out.append({"in_use": r[0].strip() == "*", "ssid": r[1], "signal": int(r[2]) if r[2].isdigit() else 0,
                        "security": r[3].strip(), "chan": r[4]})
        return out

    # -- 修改 ------------------------------------------------------------------
    def add_wifi(self, name: str, ssid: str, iface: str, key_mgmt: Optional[str], hidden: bool = False,
                 ap: bool = False) -> str:
        args = ["connection", "add", "type", "wifi", "ifname", iface, "con-name", name, "ssid", ssid,
                "autoconnect", "no", "save", "yes"]
        if hidden:
            args += ["802-11-wireless.hidden", "yes"]
        if ap:
            args += ["802-11-wireless.mode", "ap", "802-11-wireless.band", "bg", "ipv4.method", "shared",
                     "ipv6.method", "disabled"]
        if key_mgmt:
            args += ["wifi-sec.key-mgmt", key_mgmt]
            if ap:
                # Pi 3B（brcmfmac）實測：不關 PMF 熱點會卡在「supplicant-timeout」開不起來
                args += ["wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp",
                         "wifi-sec.pmf", "disable"]
        out = self.run(args)
        m = re.search(r"\(([0-9a-fA-F-]{36})\)", out)
        if not m:
            raise NmError(f"新增連線失敗：{out.strip()}")
        return m.group(1)

    def set_psk(self, uuid: str, psk: str) -> None:
        """密碼走 stdin（nmcli 互動編輯模式），不會出現在指令列或 /proc。"""
        if "\n" in psk or "\r" in psk:
            raise NmError("密碼不能有換行", "failed")
        script = f"set 802-11-wireless-security.psk {psk}\nsave persistent\nquit\n"
        rc, out, err = self.runner(["connection", "edit", uuid], input_text=script, timeout=20)
        text = (out or "") + (err or "")
        if rc != 0 or "error" in text.lower():
            # 不把 nmcli 的原始輸出往外丟（避免任何可能夾帶密碼的訊息）
            kind = "wrong_password" if "psk" in text.lower() or "passphrase" in text.lower() else "failed"
            raise NmError("設定密碼失敗（密碼格式不對？）", kind)

    def up(self, uuid: str, iface: str, wait: int) -> None:
        self.run(["-w", str(int(wait)), "connection", "up", uuid, "ifname", iface], timeout=wait + 15)

    def down(self, uuid: str) -> None:
        self.run(["connection", "down", uuid], timeout=20, check=False)

    def delete(self, uuid: str) -> None:
        self.run(["connection", "delete", uuid], timeout=20)

    def modify(self, uuid: str, *kv: str) -> None:
        self.run(["connection", "modify", uuid] + list(kv), timeout=20)


def key_mgmt_for(security: str, has_password: bool) -> Optional[str]:
    s = (security or "").upper()
    if not has_password and (not s or s in ("--", "NONE")):
        return None
    if "WEP" in s:
        return None    # 第一期不支援 WEP
    if "WPA3" in s and "WPA2" not in s and "WPA1" not in s:
        return "sae"
    return "wpa-psk"


def read_hotspot_conf(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = {}
            for line in fh:
                k, sep, v = line.strip().partition("=")
                if sep and not k.startswith("#"):
                    d[k.strip()] = v.strip()
            return d if d.get("password") else None
    except OSError:
        return None


class WifiManager:
    def __init__(self, nm: Optional[NM] = None, iface: str = "wlan0", notify: Optional[Callable[[str, float], None]] = None,
                 try_secs: int = 20, rescan_secs: float = 60, revert_secs: int = 30, hotspot_after: float = 120,
                 hotspot_retry: float = 180, hotspot: bool = True, hotspot_conf: str = DEFAULT_HOTSPOT_CONF,
                 hotspot_ssid: str = HOTSPOT_SSID, clock: Callable[[], float] = time.monotonic, pin: Optional[str] = None,
                 lock_file: Optional[str] = DEFAULT_WIFI_LOCK):
        self.nm = nm or NM()
        self.iface = iface
        self.lock_file = lock_file
        self._lock_fd = None
        self.has_lock = True
        self.notify = notify or (lambda text, secs: None)
        self.try_secs, self.rescan_secs, self.revert_secs = try_secs, rescan_secs, revert_secs
        self.hotspot_after, self.hotspot_retry = hotspot_after, hotspot_retry
        self.hotspot_enabled = hotspot
        self.hotspot_conf, self.hotspot_ssid = hotspot_conf, hotspot_ssid
        self.clock = clock
        self.pin = pin                      # 預留：之後要帶出門時可設 PIN（目前不用）
        self.lock = threading.RLock()       # 一次只做一件會改網路的事
        self.op: Optional[dict] = None      # 網頁觸發的連線進度
        self.mode = "unknown"               # connected / disconnected / connecting / hotspot / unavailable
        self.disconnected_since: Optional[float] = None
        self.next_round = 0.0
        self.hotspot_since: Optional[float] = None
        self.last_web = -1e9
        self.last_notice = ""
        self.last_notice_at = -1e9
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._status_cache: Tuple[float, Optional[dict]] = (-1e9, None)
        self._hotspot_checked = False
        self.sleep = time.sleep

    # -- 權限（預留 PIN）-----------------------------------------------------------
    def authorize(self, headers) -> bool:
        if not self.pin:
            return True
        return (headers.get("X-Pet-PIN") if headers else None) == self.pin

    # -- 查詢 ------------------------------------------------------------------
    def _active_wifi(self, conns: Optional[List[dict]] = None) -> Optional[dict]:
        for c in conns if conns is not None else self.nm.connections():
            if c["type"] == WIFI_TYPE and c["active"] and c["device"] == self.iface:
                return c
        return None

    def _is_hotspot(self, c: Optional[dict]) -> bool:
        return bool(c) and (c["name"] == self.hotspot_ssid or self.nm.wifi_info(c["uuid"])["mode"] == "ap")

    def saved(self) -> List[dict]:
        out = []
        for c in self.nm.connections():
            if c["type"] != WIFI_TYPE or c["name"] == self.hotspot_ssid:
                continue
            info = self.nm.wifi_info(c["uuid"])
            if info["mode"] != "infrastructure":
                continue
            out.append({"uuid": c["uuid"], "name": c["name"], "ssid": info["ssid"], "hidden": info["hidden"],
                        "secured": info["secured"], "priority": c["priority"], "active": c["active"],
                        "last_connected": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(c["timestamp"]))
                        if c["timestamp"] else None, "_ts": c["timestamp"]})
        out.sort(key=lambda s: (-s["priority"], -s["_ts"]))
        for s in out:
            s.pop("_ts")
        return out

    def scan(self, rescan: bool = True) -> List[dict]:
        try:
            nets = self.nm.wifi_list(self.iface, rescan)
        except NmError:
            nets = self.nm.wifi_list(self.iface, False)     # 熱點模式或掃描太頻繁時用快取
        saved = {s["ssid"]: s for s in self.saved()}
        best: Dict[str, dict] = {}
        for n in nets:
            if not n["ssid"] or n["ssid"] == self.hotspot_ssid:
                continue
            cur = best.get(n["ssid"])
            if cur is None or n["signal"] > cur["signal"] or n["in_use"]:
                best[n["ssid"]] = n
        out = [{"ssid": s, "signal": n["signal"], "security": n["security"] or "",
                "secured": bool(n["security"]) and n["security"] != "--", "active": n["in_use"],
                "saved": s in saved, "uuid": saved[s]["uuid"] if s in saved else None} for s, n in best.items()]
        out.sort(key=lambda n: (-int(n["active"]), -n["signal"]))
        return out

    def _hotspot_ip(self) -> Optional[str]:
        if self.mode != "hotspot":
            return None
        try:
            return self.nm.device_ip(self.iface)
        except NmError:
            return None

    def hotspot_url(self) -> Optional[str]:
        """熱點網址（熱點開著時用實際 IP；否則 PET_HOTSPOT_URL，沒設就 None）。"""
        ip = self._hotspot_ip()
        if ip:
            return f"http://{ip}:{HOTSPOT_PORT}/"
        return HOTSPOT_URL_FALLBACK or None

    def _hotspot_hostport(self) -> str:
        ip = self._hotspot_ip()
        return f"{ip}:{HOTSPOT_PORT}" if ip else f"熱點閘道:{HOTSPOT_PORT}"

    def status(self, cache_s: float = 1.5) -> dict:
        t, st = self._status_cache
        if st is not None and self.clock() - t < cache_s:
            return dict(st, op=dict(self.op) if self.op else None)
        wifi = {"device": self.iface, "state": "unavailable", "ssid": None, "signal": None, "ip": None, "uuid": None}
        wired = []
        try:
            devs = self.nm.devices()
            conns = self.nm.connections()
            for d in devs:
                if d["device"] == self.iface:
                    wifi["state"] = d["state"]
                elif d["type"] == "ethernet":
                    wired.append({"device": d["device"], "state": d["state"], "connection": d["connection"],
                                  "ip": self.nm.device_ip(d["device"]) if d["state"].startswith("connected") else None})
            act = self._active_wifi(conns)
            if act:
                info = self.nm.wifi_info(act["uuid"])
                wifi.update(ssid=info["ssid"], uuid=act["uuid"], ip=self.nm.device_ip(self.iface),
                            hotspot=info["mode"] == "ap")
                if info["mode"] != "ap":
                    for n in self.nm.wifi_list(self.iface, False):
                        if n["in_use"]:
                            wifi["signal"] = n["signal"]
        except NmError as exc:
            wifi["error"] = str(exc)
        st = {"mode": self.mode, "wifi": wifi, "wired": wired, "pin_required": bool(self.pin),
              "hotspot": {"ssid": self.hotspot_ssid, "active": self.mode == "hotspot", "url": self.hotspot_url(),
                          "enabled": self.hotspot_enabled, "configured": read_hotspot_conf(self.hotspot_conf) is not None}}
        self._status_cache = (self.clock(), st)
        return dict(st, op=dict(self.op) if self.op else None)

    def brief(self) -> dict:
        st = self._status_cache[1] or {}
        return {"mode": self.mode, "ssid": (st.get("wifi") or {}).get("ssid")}

    # -- 網頁操作 ----------------------------------------------------------------
    def _say(self, text: str, secs: float = 6.0, repeat_every: float = 0.0) -> None:
        now = self.clock()
        if text == self.last_notice and (not repeat_every or now - self.last_notice_at < repeat_every):
            return
        self.last_notice, self.last_notice_at = text, now
        try:
            self.notify(text, secs)
        except Exception:     # 顯示失敗不影響網路
            log.exception("顯示網路狀態失敗")

    def start_connect(self, ssid: Optional[str] = None, password: Optional[str] = None, hidden: bool = False,
                      uuid: Optional[str] = None, background: bool = True) -> dict:
        """網頁「連線」：背景執行，進度在 status()['op']。"""
        if not uuid and not (ssid and ssid.strip()):
            raise ValueError("需要 ssid 或 uuid")
        if password is not None and not (8 <= len(password) <= 63) and password != "":
            raise ValueError("WPA 密碼長度要 8～63 個字")
        if self.op and self.op.get("phase") not in ("done",):
            raise ValueError("正在連線中，請稍候")
        self.last_web = self.clock()
        op = {"id": uuidlib.uuid4().hex[:8], "ssid": ssid, "uuid": uuid, "phase": "connecting",
              "result": None, "message": "連線中…", "reverted": False, "started": time.time(), "finished": None}
        self.op = op
        args = (op, ssid, password or None, hidden, uuid)
        if background:
            threading.Thread(target=self._connect_job, args=args, name="wifi-connect", daemon=True).start()
        else:
            self._connect_job(*args)
        return {k: v for k, v in op.items()}

    def _connect_job(self, op: dict, ssid: Optional[str], password: Optional[str], hidden: bool,
                     uuid: Optional[str]) -> None:
        with self.lock:
            try:
                self._do_connect(op, ssid, password, hidden, uuid)
            except Exception as exc:     # 不讓背景執行緒默默死掉
                log.exception("Wi-Fi 連線工作失敗")
                op.update(phase="done", result="failed", message=f"連線失敗：{exc}", finished=time.time())
            self._status_cache = (-1e9, None)

    def _do_connect(self, op, ssid, password, hidden, uuid) -> None:
        conns = self.nm.connections()
        prev = self._active_wifi(conns)
        prev_uuid = prev["uuid"] if prev and not self._is_hotspot(prev) else None
        saved = self.saved()
        created = None
        target = None
        if uuid:
            target = next((s for s in saved if s["uuid"] == uuid), None)
            if not target:
                raise ValueError("找不到這個已記錄的網路")
            ssid = target["ssid"]
            op["ssid"] = ssid
        else:
            same = [s for s in saved if s["ssid"] == ssid]
            if same and password is None:
                target = same[0]
        self._say(f"正在連 Wi‑Fi：{ssid}", 8)
        self.mode = "connecting"
        try:
            if target is None:
                sec = ""
                if not hidden:
                    for n in self.nm.wifi_list(self.iface, False):
                        if n["ssid"] == ssid:
                            sec = n["security"]
                            break
                km = key_mgmt_for(sec, bool(password))
                if km is None and password:
                    km = "wpa-psk"
                if km and not password:
                    raise NmError("這個網路需要密碼", "wrong_password")
                name = ssid
                names = {c["name"] for c in conns}
                i = 2
                while name in names:
                    name = f"{ssid} ({i})"
                    i += 1
                created = self.nm.add_wifi(name, ssid, self.iface, km, hidden=hidden)
                if password:
                    self.nm.set_psk(created, password)
                up_uuid = created
            else:
                up_uuid = target["uuid"]
            if self.hotspot_since is not None:
                self._hotspot_off(reconnect=False)
            self.nm.up(up_uuid, self.iface, self.revert_secs)
        except NmError as exc:
            kind = exc.kind if exc.kind in RESULT_TEXT else "failed"
            log.warning("連 Wi‑Fi %s 失敗：%s（%s）", ssid, RESULT_TEXT[kind], kind)
            if created:
                try:
                    self.nm.delete(created)
                except NmError:
                    pass
            op.update(phase="reverting" if prev_uuid else "done", result=kind,
                      message=RESULT_TEXT[kind] + ("，切回原本的網路…" if prev_uuid else ""))
            if prev_uuid:
                try:
                    self.nm.up(prev_uuid, self.iface, self.revert_secs)
                    op["reverted"] = True
                    op["message"] = RESULT_TEXT[kind] + "，已切回原本的網路"
                except NmError as exc2:
                    op["message"] = RESULT_TEXT[kind] + f"，切回原本的網路也失敗（{exc2.kind}）"
            self._say(RESULT_TEXT[kind], 6)
            op.update(phase="done", finished=time.time())
            self.mode = "connected" if op["reverted"] else "unknown"
            return
        # 成功：自動記錄、排到最前面
        new_uuid = created or up_uuid
        self._promote(new_uuid, autoconnect=bool(created))   # 原本就有的設定只在需要時調優先值
        if created:
            for s in saved:      # 同一個 SSID 的舊設定（舊密碼）已經被取代
                if s["ssid"] == ssid and s["uuid"] != new_uuid:
                    try:
                        self.nm.delete(s["uuid"])
                    except NmError:
                        pass
        op.update(phase="done", result="ok", uuid=new_uuid, message=f"已連上 {ssid}", finished=time.time())
        self.mode = "connected"
        self.disconnected_since = None
        self._say(f"連上 Wi‑Fi：{ssid}", 5)
        log.info("已連上 Wi‑Fi %s（從網頁）", ssid)

    def _promote(self, uuid: str, autoconnect: bool = False) -> None:
        others = [c["priority"] for c in self.nm.connections() if c["type"] == WIFI_TYPE and c["uuid"] != uuid
                  and c["name"] != self.hotspot_ssid]
        cur = next((c["priority"] for c in self.nm.connections() if c["uuid"] == uuid), 0)
        top = max(others) if others else PRIORITY_BASE - PRIORITY_STEP
        kv = []
        if cur <= top:
            kv += ["connection.autoconnect-priority", str(max(top + PRIORITY_STEP, PRIORITY_BASE))]
        if autoconnect:
            kv += ["connection.autoconnect", "yes"]
        if kv:
            self.nm.modify(uuid, *kv)

    def forget(self, uuid: str) -> dict:
        self.last_web = self.clock()
        with self.lock:
            s = next((s for s in self.saved() if s["uuid"] == uuid), None)
            if not s:
                raise ValueError("找不到這個已記錄的網路")
            if s["active"]:
                raise ValueError("這是目前連線中的網路，請先連到別的網路再刪除")
            self.nm.delete(uuid)
            log.info("已刪除記錄的 Wi‑Fi %s", s["ssid"])
            return {"ok": True, "ssid": s["ssid"]}

    def set_priority(self, order: Optional[List[str]] = None, uuid: Optional[str] = None,
                     direction: Optional[str] = None) -> List[dict]:
        """order = 由高到低的 uuid 清單；或 uuid + direction（up/down/top）。"""
        self.last_web = self.clock()
        with self.lock:
            saved = self.saved()
            ids = [s["uuid"] for s in saved]
            if order is None:
                if uuid not in ids:
                    raise ValueError("找不到這個已記錄的網路")
                i = ids.index(uuid)
                ids.pop(i)
                j = {"up": max(0, i - 1), "down": min(len(ids), i + 1), "top": 0}.get(direction or "", None)
                if j is None:
                    raise ValueError("direction 要是 up / down / top")
                ids.insert(j, uuid)
            else:
                if sorted(order) != sorted(ids):
                    raise ValueError("order 要包含全部已記錄的網路（uuid）")
                ids = list(order)
            n = len(ids)
            cur = {s["uuid"]: s["priority"] for s in saved}
            for i, u in enumerate(ids):
                p = PRIORITY_BASE + PRIORITY_STEP * (n - 1 - i)
                if cur.get(u) != p:
                    self.nm.modify(u, "connection.autoconnect-priority", str(p))
            return self.saved()

    def set_hotspot(self, on: bool) -> dict:
        """手動開關設定熱點（測試與救援用）。"""
        self.last_web = self.clock()
        with self.lock:
            ok = True
            if on:
                ok = self._hotspot_on()
            else:
                self._hotspot_off(reconnect=True)
            self._status_cache = (-1e9, None)
        if not ok:
            return {"ok": False, "mode": self.mode, "error": "開熱點失敗（沒有熱點設定檔或網卡不支援）"}
        return {"ok": True, "mode": self.mode}

    # -- 熱點 ------------------------------------------------------------------
    def _hotspot_uuid(self) -> Optional[str]:
        conf = read_hotspot_conf(self.hotspot_conf)
        if not conf:
            log.warning("沒有熱點設定檔 %s（或沒有 password），不開熱點", self.hotspot_conf)
            return None
        for c in self.nm.connections():
            if c["name"] == self.hotspot_ssid and c["type"] == WIFI_TYPE:
                if not self._hotspot_checked:
                    self.nm.modify(c["uuid"], "802-11-wireless-security.pmf", "disable")   # 舊版建立的熱點補上
                    self._hotspot_checked = True
                return c["uuid"]
        u = self.nm.add_wifi(self.hotspot_ssid, conf.get("ssid", self.hotspot_ssid), self.iface, "wpa-psk", ap=True)
        self.nm.set_psk(u, conf["password"])
        log.info("已建立設定熱點連線 %s", self.hotspot_ssid)
        return u

    def _hotspot_on(self) -> bool:
        u = self._hotspot_uuid()
        if not u:
            return False
        for attempt in (1, 2):
            try:
                self.nm.up(u, self.iface, 30)
                break
            except NmError as exc:
                # 別的程式（例如系統的 wifi-watchdog）剛好在切換時去連別的網路，會打斷熱點；等一下再試一次
                if attempt == 1 and "interrupted" in str(exc).lower():
                    log.info("開熱點被打斷，5 秒後再試一次")
                    self.sleep(5)
                    continue
                log.warning("開熱點失敗：%s", exc)
                return False
        self.mode = "hotspot"
        self.hotspot_since = self.clock()
        log.info("設定熱點 %s 已開啟（%s）", self.hotspot_ssid, self.hotspot_url())
        self._say(f"設定熱點：{self.hotspot_ssid}，連上後開 {self._hotspot_hostport()}", 10)
        return True

    def _hotspot_off(self, reconnect: bool = True) -> None:
        for c in self.nm.connections():
            if c["name"] == self.hotspot_ssid and c["active"]:
                self.nm.down(c["uuid"])
                log.info("設定熱點已關閉")
        self.hotspot_since = None
        self.mode = "disconnected"
        if reconnect:
            self.next_round = 0.0

    # -- 自動嘗試（背景）---------------------------------------------------------
    def _candidates(self) -> List[dict]:
        saved = [s for s in self.saved() if s["ssid"]]
        if not saved:
            return []
        try:
            visible = {n["ssid"] for n in self.nm.wifi_list(self.iface, True)}
        except NmError:
            visible = set()
        return [s for s in saved if s["ssid"] in visible or s["hidden"]]

    def _try_saved(self) -> bool:
        for s in self._candidates():
            self.mode = "connecting"
            self._say(f"正在連 Wi‑Fi：{s['ssid']}", 6)
            try:
                self.nm.up(s["uuid"], self.iface, self.try_secs)
                self.mode = "connected"
                log.info("自動連上 Wi‑Fi %s", s["ssid"])
                self._say(f"連上 Wi‑Fi：{s['ssid']}", 5)
                return True
            except NmError as exc:
                log.info("自動連 %s 失敗：%s", s["ssid"], exc.kind)
        return False

    def tick(self) -> str:
        """背景執行緒每幾秒呼叫一次（測試可以直接呼叫）。回傳目前模式。"""
        if not self.lock.acquire(blocking=False):
            return self.mode                       # 網頁正在換網路
        try:
            now = self.clock()
            devs = {d["device"]: d for d in self.nm.devices()}
            dev = devs.get(self.iface)
            if not dev or dev["state"] in ("unavailable", "unmanaged"):
                self.mode = "unavailable"
                return self.mode
            act = self._active_wifi()
            if act and self._is_hotspot(act):
                self.mode = "hotspot"
                if self.hotspot_since is None:
                    self.hotspot_since = now
                self._say(f"設定熱點：{self.hotspot_ssid}，連上後開 {self._hotspot_hostport()}", 8, repeat_every=30)
                # 定時關熱點試一下已記錄的網路（有人正在用網頁設定時不打擾）
                if now - self.hotspot_since >= self.hotspot_retry and now - self.last_web >= 120 and self.saved():
                    log.info("熱點開了 %.0f 秒，先關掉試已記錄的網路", now - self.hotspot_since)
                    self._hotspot_off(reconnect=False)
                    if self._try_saved():
                        self.disconnected_since = None
                        return self.mode
                    self._hotspot_on()
                return self.mode
            if act:
                if self.mode != "connected" and self.disconnected_since is not None:
                    self._say(f"連上 Wi‑Fi：{self.nm.wifi_info(act['uuid'])['ssid']}", 5)
                self.mode = "connected"
                self.disconnected_since = None
                return self.mode
            if dev["state"].startswith("connecting") or "config" in dev["state"] or "ip" in dev["state"] \
                    or "prepare" in dev["state"] or "need-auth" in dev["state"]:
                self.mode = "connecting"           # NetworkManager 自己正在連
                return self.mode
            # 沒連上
            if self.disconnected_since is None:
                self.disconnected_since = now
                self.next_round = now
                log.info("Wi‑Fi 沒有連線，開始自動嘗試已記錄的網路")
            self.mode = "disconnected"
            self._say("沒有網路…正在找 Wi‑Fi", 6, repeat_every=60)
            if now >= self.next_round:
                if self._try_saved():
                    self.disconnected_since = None
                    return self.mode
                self.mode = "disconnected"
                self.next_round = self.clock() + self.rescan_secs
            if self.hotspot_enabled and self.clock() - self.disconnected_since >= self.hotspot_after:
                log.info("%.0f 秒都連不上已記錄的網路，開設定熱點", self.clock() - self.disconnected_since)
                if not self._hotspot_on():
                    self.next_round = self.clock() + self.rescan_secs
            return self.mode
        except NmError as exc:
            log.warning("Wi‑Fi 檢查失敗：%s", exc)
            return self.mode
        finally:
            self.lock.release()

    def _try_acquire_lock(self) -> bool:
        if fcntl is None or not self.lock_file:
            self.has_lock = True
            return True
        if self.has_lock and self._lock_fd:
            return True

        # 1. 建立目錄並開啟鎖檔
        if self._lock_fd is None:
            try:
                dir_name = os.path.dirname(self.lock_file)
                if dir_name:
                    os.makedirs(dir_name, exist_ok=True)
                self._lock_fd = open(self.lock_file, "a+")
            except (IOError, OSError) as exc:
                # 目錄無法建立或權限不足（例如一般使用者在 /run 下無權限）：
                # 絕不能中斷 Wi-Fi 管理與救援熱點！記 WARNING 並直接管理 Wi-Fi。
                log.warning("無法建立或開啟 Wi‑Fi 鎖檔 %s (%s)，略過檔案鎖直接啟用 Wi‑Fi 管理", self.lock_file, exc)
                self.has_lock = True
                return True

        # 2. 申請互斥鎖（只有 flock 拋出 BlockingIOError 才算鎖被佔用）
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.has_lock = True
            return True
        except (BlockingIOError, IOError, OSError) as exc:
            err = getattr(exc, "errno", None)
            if isinstance(exc, BlockingIOError) or (errno is not None and err in (getattr(errno, "EAGAIN", 11), getattr(errno, "EWOULDBLOCK", 11))):
                self.has_lock = False
                return False
            log.warning("Wi‑Fi 鎖檔 flock 失敗 %s (%s)，略過檔案鎖直接啟用 Wi‑Fi 管理", self.lock_file, exc)
            self.has_lock = True
            return True

    def start(self, interval: float = 5.0, retry_lock_interval: float = 10.0) -> None:
        self._stop.clear()
        acquired = self._try_acquire_lock()
        if not acquired:
            log.warning("無法取得 Wi‑Fi 管理鎖 %s（可能已有其他實例在跑），已排定每 %.0f 秒重試接手",
                        self.lock_file, retry_lock_interval)

        def loop():
            while not self._stop.is_set():
                if not self.has_lock:
                    if self._try_acquire_lock():
                        log.info("已成功取得 Wi‑Fi 管理鎖，接手管理")
                    else:
                        self._stop.wait(retry_lock_interval)
                        continue
                try:
                    self.tick()
                except Exception:
                    log.exception("Wi‑Fi 背景檢查出錯")
                self._stop.wait(interval)

        self._thread = threading.Thread(target=loop, name="wifi", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._lock_fd:
            try:
                if fcntl is not None:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_fd.close()
            except Exception:
                pass
            self._lock_fd = None
        self.has_lock = True

    @staticmethod
    def available(iface: str = "wlan0") -> bool:
        return bool(shutil.which("nmcli")) and os.path.exists(f"/sys/class/net/{iface}")


class WifiWebAPI:
    """網頁遙控的 /wifi/* 路由。回傳 (HTTP 狀態碼, JSON 物件)；不會回傳任何密碼。"""

    def __init__(self, mgr: WifiManager):
        self.mgr = mgr

    def handle(self, method: str, path: str, data: Optional[dict], headers=None) -> Optional[Tuple[int, object]]:
        if not path.startswith("/wifi/"):
            return None
        if not self.mgr.authorize(headers):
            return 403, {"ok": False, "error": "需要 PIN"}
        try:
            if method == "GET" and path == "/wifi/status":
                return 200, self.mgr.status()
            if method == "GET" and path == "/wifi/scan":
                return 200, {"ok": True, "networks": self.mgr.scan(True)}
            if method == "GET" and path == "/wifi/saved":
                return 200, {"ok": True, "saved": self.mgr.saved()}
            if method == "POST":
                if not getattr(self.mgr, "has_lock", True):
                    return 409, {"ok": False, "error": "另一個 Wi‑Fi 管理在執行"}
                d = data if isinstance(data, dict) else {}
                if path == "/wifi/connect":
                    pw = d.get("password")
                    op = self.mgr.start_connect(ssid=(d.get("ssid") or "").strip() or None,
                                                password=pw if isinstance(pw, str) else None,
                                                hidden=bool(d.get("hidden")), uuid=d.get("uuid") or None)
                    return 202, {"ok": True, "op": op}
                if path == "/wifi/forget":
                    return 200, self.mgr.forget(str(d.get("uuid") or ""))
                if path == "/wifi/priority":
                    order = d.get("order")
                    return 200, {"ok": True, "saved": self.mgr.set_priority(
                        order=list(order) if isinstance(order, list) else None,
                        uuid=d.get("uuid"), direction=d.get("direction"))}
                if path == "/wifi/hotspot":
                    r = self.mgr.set_hotspot(bool(d.get("on")))
                    return (200 if r["ok"] else 500), r
        except ValueError as exc:
            return 400, {"ok": False, "error": str(exc)}
        except NmError as exc:
            return 500, {"ok": False, "error": RESULT_TEXT.get(exc.kind, "失敗"), "kind": exc.kind}
        return 404, {"ok": False, "error": "not found"}
