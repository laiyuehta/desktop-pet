"""Wi‑Fi 模組測試：nmcli 全部用假的（不碰真的網路）。"""
import json
import threading
import urllib.request

import pytest

import os
import sys

_ADMIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ADMIN_DIR not in sys.path:
    sys.path.insert(0, _ADMIN_DIR)

from wifi import NM, NmError, WifiManager, WifiWebAPI, classify_error, key_mgmt_for, split_terse, WIFI_TYPE


class FakeNM:
    """模擬 NetworkManager 的狀態（方法層級，跟 pet.wifi.NM 同介面）。"""

    def __init__(self):
        self.conns = {}        # uuid → dict
        self.air = {}          # 空中看得到（或隱藏但存在）的網路 ssid → {"password", "security", "signal", "hidden"}
        self.active = None
        self.n = 0
        self.calls = []
        self.dev_state = "disconnected"
        self.ap_ok = True
        self.add("Wired connection 1", None, ctype="802-3-ethernet")

    def add(self, name, ssid, ctype=WIFI_TYPE, priority=0, ts=0, password=None, hidden=False, mode="infrastructure",
            autoconnect=True):
        self.n += 1
        u = f"00000000-0000-0000-0000-{self.n:012d}"
        self.conns[u] = {"name": name, "uuid": u, "type": ctype, "priority": priority, "timestamp": ts, "ssid": ssid,
                         "psk": password, "hidden": hidden, "mode": mode, "autoconnect": autoconnect,
                         "key_mgmt": "wpa-psk" if password else None}
        return u

    # NM 介面
    def connections(self):
        return [{"name": c["name"], "uuid": u, "type": c["type"], "priority": c["priority"], "timestamp": c["timestamp"],
                 "active": u == self.active or c["type"] != WIFI_TYPE,
                 "device": ("wlan0" if u == self.active else "eth0" if c["type"] != WIFI_TYPE else "")}
                for u, c in self.conns.items()]

    def wifi_info(self, uuid):
        c = self.conns[uuid]
        return {"ssid": c["ssid"] or "", "mode": c["mode"], "hidden": c["hidden"], "secured": bool(c["key_mgmt"])}

    def devices(self):
        st = "connected" if self.active else self.dev_state
        return [{"device": "eth0", "type": "ethernet", "state": "connected", "connection": "Wired connection 1"},
                {"device": "wlan0", "type": "wifi", "state": st, "connection": self.conns[self.active]["name"] if self.active else "--"}]

    def device_ip(self, dev):
        return "192.0.2.43" if dev == "eth0" else ("192.0.2.1" if self.active and self.conns[self.active]["mode"] == "ap"
                                                       else "192.0.2.101" if self.active else None)

    def wifi_list(self, iface, rescan):
        self.calls.append(("list", rescan))
        cur = self.conns[self.active]["ssid"] if self.active else None
        return [{"in_use": s == cur, "ssid": "" if a.get("hidden") else s, "signal": a.get("signal", 50),
                 "security": a.get("security", "WPA2"), "chan": "1"} for s, a in self.air.items()]

    def add_wifi(self, name, ssid, iface, key_mgmt, hidden=False, ap=False):
        self.calls.append(("add", name, ssid, key_mgmt, hidden, ap))
        u = self.add(name, ssid, hidden=hidden, mode="ap" if ap else "infrastructure", autoconnect=False)
        self.conns[u]["key_mgmt"] = key_mgmt
        return u

    def set_psk(self, uuid, psk):
        self.calls.append(("psk", uuid))          # 不記錄密碼本身
        self.conns[uuid]["psk"] = psk

    def up(self, uuid, iface, wait):
        self.calls.append(("up", uuid, wait))
        c = self.conns[uuid]
        if c["mode"] == "ap":
            if not self.ap_ok:
                raise NmError("ap failed")
            self.active = uuid
            return
        a = self.air.get(c["ssid"])
        if a is None:
            self.active = None
            raise NmError("Error: Connection activation failed: The Wi-Fi network could not be found", "not_found")
        if a.get("password") and a["password"] != c["psk"]:
            self.active = None
            raise NmError("Error: Connection activation failed: Secrets were required, but not provided", "wrong_password")
        self.active = uuid
        c["timestamp"] = 1790000000 + self.n

    def down(self, uuid):
        self.calls.append(("down", uuid))
        if self.active == uuid:
            self.active = None

    def delete(self, uuid):
        self.calls.append(("delete", uuid))
        if self.active == uuid:
            self.active = None
        del self.conns[uuid]

    def modify(self, uuid, *kv):
        self.calls.append(("modify", uuid) + kv)
        c = self.conns[uuid]
        for k, v in zip(kv[::2], kv[1::2]):
            if k == "connection.autoconnect-priority":
                c["priority"] = int(v)
            elif k == "connection.autoconnect":
                c["autoconnect"] = v == "yes"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    nm = FakeNM()
    home = nm.add("HomeAP", "HomeAP", priority=200, ts=1789000000, password="homepass1", hidden=True)
    nm.air["HomeAP"] = {"password": "homepass1", "hidden": True, "signal": 59}
    nm.active = home
    conf = tmp_path / "hs.conf"
    conf.write_text("# test\nssid=PetRescue-AP\npassword=hotspotpw123\n")
    notes = []
    clock = Clock()
    mgr = WifiManager(nm=nm, notify=lambda t, s: notes.append(t), hotspot_conf=str(conf), clock=clock)
    return nm, mgr, home, notes, clock


# --- 解析 ---------------------------------------------------------------------
def test_split_terse_escapes():
    assert split_terse(r"a\:b:c\\d:") == ["a:b", "c\\d", ""]
    assert split_terse("x") == ["x"]


def test_classify_errors():
    assert classify_error("Error: Connection activation failed: Secrets were required, but not provided") == "wrong_password"
    assert classify_error("Error: No network with SSID 'foo' found.") == "not_found"
    assert classify_error("Error: Timeout expired (30 seconds)") == "timeout"
    assert classify_error("Error: Not authorized to control networking.") == "not_authorized"


def test_key_mgmt():
    assert key_mgmt_for("WPA2", True) == "wpa-psk"
    assert key_mgmt_for("WPA3", True) == "sae"
    assert key_mgmt_for("WPA2 WPA3", True) == "wpa-psk"
    assert key_mgmt_for("", False) is None


def test_nm_runner_parsing_and_psk_stdin():
    calls = []

    def runner(args, input_text=None, timeout=60):
        calls.append((args, input_text))
        if args[:1] == ["-t"] and "connection" in args and args[-1] == "show":
            return 0, "HomeAP:d184:802-11-wireless:200:1789000000:yes:wlan0\nWired connection 1:aaaa:802-3-ethernet:-999:0:yes:eth0\n", ""
        if "wifi" in args and "list" in args:
            return 0, "*:home\\:0503:59:WPA2:1\n :Other:40:WPA1 WPA2:6\n", ""
        if args[:2] == ["connection", "add"]:
            return 0, "Connection 'x' (12345678-1234-1234-1234-123456789abc) successfully added.\n", ""
        if args[:2] == ["connection", "edit"]:
            return 0, "Connection 'x' successfully saved.\n", ""
        return 0, "", ""

    nm = NM(runner=runner)
    c = nm.connections()
    assert c[0]["priority"] == 200 and c[0]["active"] and c[1]["priority"] == -999
    w = nm.wifi_list("wlan0", True)
    assert w[0]["ssid"] == "home:0503" and w[0]["in_use"] and w[1]["security"] == "WPA1 WPA2"
    u = nm.add_wifi("x", "x", "wlan0", "wpa-psk")
    assert u == "12345678-1234-1234-1234-123456789abc"
    nm.set_psk(u, "s3cret-pass")
    # 密碼只在 stdin，不在任何一次的指令列
    assert all("s3cret-pass" not in " ".join(a) for a, _ in calls)
    assert "s3cret-pass" in calls[-1][1]
    with pytest.raises(NmError):
        nm.set_psk(u, "bad\npass")


def test_nm_set_psk_error_hides_output():
    nm = NM(runner=lambda args, input_text=None, timeout=60: (0, "Error: failed to set 'psk' property: invalid 'zzzSECRET'", ""))
    with pytest.raises(NmError) as ei:
        nm.set_psk("u", "zzzSECRETzz")
    assert "SECRET" not in str(ei.value)


def test_nm_error_kind():
    nm = NM(runner=lambda args, input_text=None, timeout=60: (4, "", "Error: Connection activation failed: Secrets were required, but not provided."))
    with pytest.raises(NmError) as ei:
        nm.up("u", "wlan0", 30)
    assert ei.value.kind == "wrong_password"


# --- 查詢 ---------------------------------------------------------------------
def test_status_scan_saved(env):
    nm, mgr, home, _, _ = env
    nm.air["Cafe"] = {"password": "cafepass1", "signal": 70}
    nm.air["Open"] = {"security": "", "signal": 30}
    st = mgr.status()
    assert st["wifi"]["ssid"] == "HomeAP" and st["wifi"]["ip"] == "192.0.2.101"
    assert st["wired"][0]["device"] == "eth0" and st["wired"][0]["ip"] == "192.0.2.43"
    nets = mgr.scan()
    assert [n["ssid"] for n in nets] == ["Cafe", "Open"]        # 隱藏網路掃不到
    assert nets[1]["secured"] is False
    saved = mgr.saved()
    assert len(saved) == 1 and saved[0]["ssid"] == "HomeAP" and saved[0]["hidden"] and saved[0]["last_connected"]
    assert "psk" not in json.dumps(saved) and "homepass1" not in json.dumps(saved)


# --- 連線 ---------------------------------------------------------------------
def test_connect_new_success_saves_and_prioritizes(env):
    nm, mgr, home, notes, _ = env
    nm.air["Cafe"] = {"password": "cafepass1", "signal": 70}
    mgr.start_connect(ssid="Cafe", password="cafepass1", background=False)
    op = mgr.op
    assert op["result"] == "ok" and op["phase"] == "done"
    new = op["uuid"]
    assert nm.active == new and nm.conns[new]["autoconnect"]
    assert nm.conns[new]["priority"] > nm.conns[home]["priority"]       # 最新成功的排最前面
    assert nm.conns[home]["priority"] == 200                           # 原本的不動
    assert mgr.saved()[0]["ssid"] == "Cafe"
    assert any("Cafe" in n for n in notes)


def test_connect_wrong_password_reverts(env):
    nm, mgr, home, notes, _ = env
    nm.air["Cafe"] = {"password": "cafepass1"}
    before = set(nm.conns)
    mgr.start_connect(ssid="Cafe", password="wrongpass", background=False)
    op = mgr.op
    assert op["result"] == "wrong_password" and op["reverted"] and "密碼錯誤" in op["message"]
    assert nm.active == home
    assert set(nm.conns) == before                    # 失敗的新設定刪掉，原本的都在
    assert "wrongpass" not in json.dumps(op)


def test_connect_wrong_password_same_ssid_keeps_original(env):
    """用錯的密碼重連同一個 SSID：原本的設定不能被刪或改。"""
    nm, mgr, home, _, _ = env
    orig = dict(nm.conns[home])
    mgr.start_connect(ssid="HomeAP", password="wrongpass", hidden=True, background=False)
    assert mgr.op["result"] == "wrong_password" and mgr.op["reverted"]
    assert nm.conns[home] == dict(orig, timestamp=nm.conns[home]["timestamp"]) and nm.active == home


def test_connect_not_found_and_hidden(env):
    nm, mgr, home, _, _ = env
    mgr.start_connect(ssid="Nope", password="whatever1", background=False)
    assert mgr.op["result"] == "not_found" and nm.active == home
    nm.air["Secret"] = {"password": "secretpw1", "hidden": True}
    mgr.start_connect(ssid="Secret", password="secretpw1", hidden=True, background=False)
    assert mgr.op["result"] == "ok"
    assert ("add", "Secret", "Secret", "wpa-psk", True, False) in nm.calls


def test_connect_saved_by_uuid_and_replace_old_password(env):
    nm, mgr, home, _, _ = env
    nm.air["Cafe"] = {"password": "newpass99"}
    old = nm.add("Cafe", "Cafe", priority=100, password="oldpass11")
    mgr.start_connect(uuid=old, background=False)
    assert mgr.op["result"] == "wrong_password" and nm.active == home and old in nm.conns
    mgr.start_connect(ssid="Cafe", password="newpass99", background=False)
    assert mgr.op["result"] == "ok"
    assert old not in nm.conns          # 舊密碼的設定被新的取代
    assert [s["ssid"] for s in mgr.saved()].count("Cafe") == 1


def test_connect_validation(env):
    _, mgr, _, _, _ = env
    with pytest.raises(ValueError):
        mgr.start_connect(ssid="x", password="short", background=False)
    with pytest.raises(ValueError):
        mgr.start_connect(background=False)


# --- 刪除與排序 -----------------------------------------------------------------
def test_forget_and_priority(env):
    nm, mgr, home, _, _ = env
    a = nm.add("A", "A", priority=50, password="aaaaaaaa")
    b = nm.add("B", "B", priority=40, password="bbbbbbbb")
    with pytest.raises(ValueError):
        mgr.forget(home)                      # 連線中的不能刪
    saved = mgr.set_priority(uuid=b, direction="top")
    assert [s["name"] for s in saved] == ["B", "HomeAP", "A"]
    saved = mgr.set_priority(order=[a, home, b])
    assert [s["name"] for s in saved] == ["A", "HomeAP", "B"]
    with pytest.raises(ValueError):
        mgr.set_priority(order=[a])
    mgr.forget(a)
    assert a not in nm.conns


# --- 自動嘗試與熱點 --------------------------------------------------------------
def test_auto_reconnect_on_disconnect(env):
    nm, mgr, home, notes, clock = env
    other = nm.add("Backup", "Backup", priority=100, password="backup11")
    nm.air["Backup"] = {"password": "backup11"}
    assert mgr.tick() == "connected"
    del nm.air["HomeAP"]                    # 路由器關掉
    nm.active = None
    assert mgr.tick() == "connected"
    assert nm.active == other
    assert any("沒有網路" in n for n in notes)
    ups = [c for c in nm.calls if c[0] == "up"]
    assert ups[0][1] == home and ups[0][2] == 20      # 隱藏網路照樣試，每個 20 秒
    assert ups[-1][1] == other


def test_hotspot_after_timeout_and_off_when_back(env):
    nm, mgr, home, notes, clock = env
    del nm.air["HomeAP"]
    nm.active = None
    assert mgr.tick() == "disconnected"
    clock.t += 30
    assert mgr.tick() == "disconnected"
    n_up = len([c for c in nm.calls if c[0] == "up"])
    clock.t += 31                              # 一分鐘後重掃再試
    mgr.tick()
    assert len([c for c in nm.calls if c[0] == "up"]) > n_up
    clock.t += 60                              # 超過 2 分鐘 → 開熱點
    assert mgr.tick() == "hotspot"
    hs = [u for u, c in nm.conns.items() if c["name"] == "PetRescue-AP"][0]
    assert nm.conns[hs]["mode"] == "ap" and nm.conns[hs]["psk"] == "hotspotpw123" and nm.active == hs
    assert any("PetRescue-AP" in n for n in notes)
    assert mgr.saved()[0]["name"] == "HomeAP"        # 熱點不在已記錄清單
    # 熱點網址用 wlan0 實際拿到的 IP（不寫死）
    assert any("192.0.2.1:8080" in n for n in notes)
    assert mgr.status(cache_s=0)["hotspot"]["url"] == "http://192.0.2.1:8080/"
    # 在熱點模式下從網頁設定新網路 → 熱點自動關掉
    nm.air["Cafe"] = {"password": "cafepass1"}
    mgr.start_connect(ssid="Cafe", password="cafepass1", background=False)
    assert mgr.op["result"] == "ok" and nm.conns[nm.active]["ssid"] == "Cafe"
    assert mgr.tick() == "connected"


def test_hotspot_retries_known_networks(env):
    nm, mgr, home, _, clock = env
    del nm.air["HomeAP"]
    nm.active = None
    mgr.hotspot_after = 0
    assert mgr.tick() == "hotspot"
    clock.t += 200
    nm.air["HomeAP"] = {"password": "homepass1", "hidden": True}    # 路由器回來了
    assert mgr.tick() == "connected" and nm.active == home
    assert not any(c["active"] for c in nm.connections() if c["name"] == "PetRescue-AP")


def test_no_hotspot_without_conf(env, tmp_path):
    nm, mgr, home, _, clock = env
    mgr.hotspot_conf = str(tmp_path / "missing.conf")
    del nm.air["HomeAP"]
    nm.active = None
    mgr.hotspot_after = 0
    assert mgr.tick() == "disconnected"
    assert not any(c["name"] == "PetRescue-AP" for c in nm.conns.values())


def test_manual_hotspot_toggle(env):
    nm, mgr, home, _, _ = env
    nm.ap_ok = False
    assert mgr.set_hotspot(True)["ok"] is False
    assert WifiWebAPI(mgr).handle("POST", "/wifi/hotspot", {"on": True})[0] == 500
    nm.ap_ok = True
    assert mgr.set_hotspot(True)["ok"] is True
    assert mgr.mode == "hotspot"
    hs = [u for u, c in nm.conns.items() if c["name"] == "PetRescue-AP"][0]
    assert ("modify", hs, "802-11-wireless-security.pmf", "disable") in nm.calls
    mgr.set_hotspot(False)
    assert mgr.tick() == "connected" and nm.active == home


# --- 網頁 API -----------------------------------------------------------------
def test_web_api_routes(env):
    nm, mgr, home, _, _ = env
    api = WifiWebAPI(mgr)
    assert api.handle("GET", "/status", None) is None
    code, st = api.handle("GET", "/wifi/status", None)
    assert code == 200 and st["wifi"]["ssid"] == "HomeAP"
    code, body = api.handle("POST", "/wifi/forget", {"uuid": home})
    assert code == 400
    code, body = api.handle("POST", "/wifi/connect", {"ssid": "x", "password": "123"})
    assert code == 400
    assert api.handle("GET", "/wifi/nope", None)[0] == 404
    mgr.pin = "1234"
    assert api.handle("GET", "/wifi/status", None, {})[0] == 403
    assert api.handle("GET", "/wifi/status", None, {"X-Pet-PIN": "1234"})[0] == 200


def test_web_remote_http_wifi(env):
    from pet.inputs import WebRemoteInput
    nm, mgr, home, _, _ = env
    nm.air["Cafe"] = {"password": "cafepass1"}
    got = []
    w = WebRemoteInput(got.append, "127.0.0.1", 0, status_fn=lambda: {"ok": 1}, routes=[WifiWebAPI(mgr)])
    w.start()
    try:
        base = f"http://127.0.0.1:{w.port}"
        s = json.loads(urllib.request.urlopen(base + "/wifi/saved").read())
        assert s["saved"][0]["ssid"] == "HomeAP"
        assert json.loads(urllib.request.urlopen(base + "/status").read()) == {"ok": 1}
        req = urllib.request.Request(base + "/wifi/connect", data=json.dumps({"ssid": "Cafe", "password": "cafepass1"}).encode(),
                                     headers={"Content-Type": "application/json"})
        r = urllib.request.urlopen(req)
        body = r.read().decode()
        assert r.status == 202 and "cafepass1" not in body
        for _ in range(100):
            if mgr.op and mgr.op["phase"] == "done":
                break
            threading.Event().wait(0.02)
        assert mgr.op["result"] == "ok"
        st = urllib.request.urlopen(base + "/wifi/status").read().decode()
        assert "cafepass1" not in st
        assert got == []                     # Wi‑Fi 路由不會變成寵物事件
    finally:
        w.stop()


def test_hotspot_retry_when_interrupted(env):
    nm, mgr, home, _, _ = env
    mgr.sleep = lambda s: None
    real_up = nm.up
    state = {"n": 0}

    def flaky(uuid, iface, wait):
        if nm.conns[uuid]["mode"] == "ap" and state["n"] == 0:
            state["n"] += 1
            raise NmError("Error: Connection activation failed: The base network connection was interrupted")
        return real_up(uuid, iface, wait)
    nm.up = flaky
    assert mgr.set_hotspot(True)["ok"] and mgr.mode == "hotspot"
