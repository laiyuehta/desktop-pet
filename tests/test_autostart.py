"""開機自動啟動的設定檢查：Pi 的 systemd unit、R7 的 supervise.py。"""
import configparser
import re
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
    # 部署值都是佔位（install.sh 依 deploy.env 代換），repo 裡沒有實際使用者、家目錄、後端網址
    assert s["User"] == "@USER@" and s["Group"] == "@USER@" and s["Restart"] == "always"
    assert s["WorkingDirectory"] == "@HOME@/desktop-pet"
    assert _unit()["Install"]["WantedBy"] == "multi-user.target"
    args = s["ExecStart"].split()
    assert args[:2] == ["/usr/bin/python3", "@HOME@/desktop-pet/main.py"]
    flags = dict(zip(args[2::2], args[3::2]))
    assert flags == {
        "--sink": "piscreen", "--input": "web", "--web": "0.0.0.0:8080", "--fps": "20",
        "--backend": "@BACKEND_URL@", "--pack": "@HOME@/desktop-pet/packs/seed",
        "--backend-token-file": "@HOME@/.config/desktop-pet/backend-token",
    }
    assert "--backend-token " not in s["ExecStart"] + " "


def test_deploy_files_only_use_placeholders():
    files = ["services/desktop-pet/desktop-pet.service", "services/picam/picam.service",
             "tools/wifi/50-desktop-pet-wifi.rules"]
    for rel in files:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "@USER@" in text, rel
        assert "/home/" not in text and "pi-user" not in text, rel
    pc = (ROOT / "services" / "picam" / "picam.service").read_text(encoding="utf-8")
    assert "User=@USER@" in pc and "ExecStart=/usr/bin/python3 @HOME@/picam/picamd.py" in pc


def test_deploy_env_example_and_gitignore():
    ex = (ROOT / "deploy.env.example").read_text(encoding="utf-8")
    kv = dict(l.split("=", 1) for l in ex.splitlines() if l and not l.startswith("#"))
    assert set(kv) == {"PET_USER", "PET_HOME", "PET_BACKEND_URL"}
    assert kv["PET_BACKEND_URL"] == "https://pet.example.com"
    assert "deploy.env" in (ROOT / ".gitignore").read_text(encoding="utf-8").split()


def _render(tmp_path, template, env_text):
    import subprocess
    (tmp_path / "deploy.env").write_text(env_text, encoding="utf-8")
    script = f'. "{ROOT}/tools/deploy/common.sh"; load_deploy_env && render_template "{ROOT / template}"'
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "PET_REPO_ROOT": str(tmp_path)})


def test_render_substitutes_all_placeholders(tmp_path):
    import shutil
    if not shutil.which("sh"):
        return
    r = _render(tmp_path, "services/desktop-pet/desktop-pet.service",
                "PET_USER=alice\nPET_HOME=/srv/alice\nPET_BACKEND_URL=https://b.example.org\n")
    assert r.returncode == 0, r.stderr
    assert "@" not in r.stdout.replace("multi-user", "")
    assert "User=alice" in r.stdout and "/srv/alice/desktop-pet/main.py" in r.stdout
    assert "--backend https://b.example.org" in r.stdout


def test_install_scripts_never_write_deploy_env():
    for rel in ["services/desktop-pet/install.sh", "services/picam/install.sh", "tools/wifi/install.sh",
                "tools/deploy/common.sh", "services/camcast/start.sh"]:
        code = "\n".join(l for l in (ROOT / rel).read_text(encoding="utf-8").splitlines()
                         if not l.lstrip().startswith("#"))
        assert not re.search(r">\s*\S*deploy\.env", code), rel


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
