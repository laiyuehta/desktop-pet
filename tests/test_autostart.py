"""開機自動啟動的設定檢查：Pi 的 systemd unit、R7 的 supervise.py。"""
import configparser
import importlib.util
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "services" / "desktop-pet" / "desktop-pet.service"


def _unit():
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read_string(UNIT.read_text(encoding="utf-8"))
    return cp


def test_unit_never_hard_depends_on_piscreen():
    text = "\n".join(l for l in UNIT.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
    for bad in ("Requires=", "BindsTo=", "PartOf=", "Requisite=", "PropagatesStopTo=", "PropagatesReloadTo="):
        assert bad not in text, bad
    u = _unit()["Unit"]
    for name in ("piscreen.service", "picam.service", "network-online.target"):
        assert name in u["After"].split()
        assert name in u["Wants"].split()


def test_unit_service_settings():
    s = _unit()["Service"]
    assert s["User"] == "pi-user" and s["Restart"] == "always"
    assert _unit()["Install"]["WantedBy"] == "multi-user.target"
    args = s["ExecStart"].split()
    assert args[:2] == ["/usr/bin/python3", "/home/pi-user/desktop-pet/main.py"]
    flags = dict(zip(args[2::2], args[3::2]))
    assert flags == {
        "--sink": "piscreen", "--input": "web", "--web": "0.0.0.0:8080", "--fps": "20",
        "--backend": "https://pet.example.com", "--pack": "/home/pi-user/desktop-pet/packs/seed",
        "--backend-token-file": "/home/pi-user/.config/desktop-pet/backend-token",
    }
    assert "--backend-token " not in s["ExecStart"] + " "


def _sup():
    spec = importlib.util.spec_from_file_location("supervise", ROOT / "backend" / "autostart" / "supervise.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_supervisor_single_instance(monkeypatch, tmp_path):
    sup = _sup()
    monkeypatch.setattr(sup, "LOCK_PORT", _free_port())
    first = sup.take_lock()
    try:
        assert first is not None
        assert sup.take_lock() is None
    finally:
        first.close()


def test_supervisor_skips_when_port_busy_and_restarts(monkeypatch, tmp_path):
    sup = _sup()
    monkeypatch.setattr(sup, "LOG", str(tmp_path / "sup.log"))
    busy = iter([True, True, False, False])
    monkeypatch.setattr(sup, "port_in_use", lambda port=None: next(busy))
    starts, sleeps = [], []
    sup.run_forever(start=lambda: starts.append(1) or 1, sleep=sleeps.append, max_runs=2)
    assert len(starts) == 2
    assert sleeps[:2] == [15, 15]
    assert sleeps[2] == 5.0 and sleeps[3] == 10.0
    assert "不重複啟動" in (tmp_path / "sup.log").read_text(encoding="utf-8")


def test_start_hidden_checks_port():
    raw = (ROOT / "backend" / "start_hidden.cmd").read_bytes()
    raw.decode("ascii")  # cmd.exe 用 cp950 解析，非 ASCII 註解會把行拆壞
    text = raw.decode("ascii")
    assert ':8090 .*LISTENING' in text and "exit /b 0" in text
