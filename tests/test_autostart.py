"""開機自動啟動的設定檢查：Pi 的 systemd unit、common.sh、R7 的 supervise.py 與 start_hidden.cmd。"""
import configparser
import importlib.util
import re
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "services" / "desktop-pet" / "desktop-pet.service"
SPLIT_UNIT = ROOT / "services" / "desktop-pet" / "desktop-pet-split.service"
ADMIN_UNIT = ROOT / "services" / "desktop-pet-admin" / "desktop-pet-admin.service"


def _unit(path: Path = UNIT):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read_string(path.read_text(encoding="utf-8"))
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
    # 部署值用佔位（@USER@ / @HOME@），執行期設定由 EnvironmentFile=-/etc/desktop-pet/env 載入
    assert s["User"] == "@USER@" and s["Group"] == "@USER@" and s["Restart"] == "always"
    assert s["WorkingDirectory"] == "@HOME@/desktop-pet"
    assert s["EnvironmentFile"] == "-/etc/desktop-pet/env"
    assert _unit()["Install"]["WantedBy"] == "multi-user.target"
    args = s["ExecStart"].split()
    assert args[:2] == ["/usr/bin/python3", "@HOME@/desktop-pet/main.py"]
    flags = dict(zip(args[2::2], args[3::2]))
    assert flags == {
        "--sink": "piscreen", "--input": "web", "--fps": "20",
    }
    assert "@BACKEND_URL@" not in s["ExecStart"]
    assert "--backend-token " not in s["ExecStart"] + " "

    sp = _unit(SPLIT_UNIT)["Service"]
    assert sp["EnvironmentFile"] == "-/etc/desktop-pet/env"
    assert "--web off" in sp["ExecStart"] and "--wifi off" in sp["ExecStart"]
    assert "@BACKEND_URL@" not in sp["ExecStart"]

    adm = _unit(ADMIN_UNIT)["Service"]
    assert adm["EnvironmentFile"] == "-/etc/desktop-pet/env"


def test_deploy_files_only_use_placeholders():
    files = ["services/desktop-pet/desktop-pet.service",
             "services/desktop-pet/desktop-pet-split.service",
             "services/desktop-pet-admin/desktop-pet-admin.service",
             "services/picam/picam.service",
             "tools/wifi/50-desktop-pet-wifi.rules"]
    for rel in files:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "@USER@" in text, rel
        assert "/home/" not in text and "pi-user" not in text, rel
    pc = (ROOT / "services" / "picam" / "picam.service").read_text(encoding="utf-8")
    assert "User=@USER@" in pc and "ExecStart=/usr/bin/python3 @HOME@/picam/picamd.py" in pc
    assert "EnvironmentFile=-/etc/desktop-pet/env" in pc


def test_deploy_env_example_and_gitignore():
    ex = (ROOT / "deploy.env.example").read_text(encoding="utf-8")
    lines = [l for l in ex.splitlines() if l and not l.startswith("#")]
    keys = [l.split("=", 1)[0] for l in lines]
    # 每個 KEY 只出現一次，沒有重複定義
    assert len(keys) == len(set(keys))
    kv = dict(l.split("=", 1) for l in lines)
    expected_active_keys = {
        "PET_USER", "PET_HOME", "PET_BACKEND_URL",
        "PET_BACKEND_TOKEN_FILE", "PET_PET_ID", "PET_PACK_DIR",
        "PET_WEB_LISTEN", "PET_CAMERA_PENDING_DIR", "PET_WIFI_IFACE", "PET_HOTSPOT_URL",
        "PET_PY", "PET_PYTHONW", "PET_BACKEND_HOST", "PET_BACKEND_PORT",
        "PET_SUPERVISOR_LOCK_PORT", "PET_BACKEND_CONFIG", "PET_BACKEND_DATA_DIR",
        "PET_BACKEND_LOG_DIR", "PET_BACKEND_API_TOKEN_FILE",
        "PET_LLM_LOCAL_URL", "PET_LLM_LOCAL_MODEL",
    }
    assert expected_active_keys.issubset(set(kv))
    assert kv["PET_BACKEND_URL"] == "https://pet.example.com"
    assert kv["PET_PET_ID"] == "seed-001"
    assert kv["PET_PACK_DIR"] == ""  # 留空以走 auto 模式（相容現有 Pi 路徑）
    assert kv["PET_WEB_LISTEN"] == "0.0.0.0:8080"
    assert kv["PET_BACKEND_PORT"] == "8090"
    # 自動更新與控制台範例以註解保留
    assert "#PET_AUTOUPDATE=1" in ex
    assert "#PET_CONSOLE_LISTEN=" in ex
    assert "deploy.env" in (ROOT / ".gitignore").read_text(encoding="utf-8").split()


def _render(tmp_path, template, env_text, extra_env=None):
    import subprocess
    (tmp_path / "deploy.env").write_text(env_text, encoding="utf-8")
    script = f'. "{ROOT}/tools/deploy/common.sh"; load_deploy_env && render_template "{ROOT / template}"'
    env = {"PATH": "/usr/bin:/bin", "PET_REPO_ROOT": str(tmp_path), "PET_SYS_ENV": str(tmp_path / "no-sys-env")}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=env)


def test_render_substitutes_all_placeholders(tmp_path):
    import shutil
    if not shutil.which("sh"):
        return
    r = _render(tmp_path, "services/desktop-pet/desktop-pet.service",
                "PET_USER=alice\nPET_HOME=/srv/alice\nPET_BACKEND_URL=https://b.example.org\n")
    assert r.returncode == 0, r.stderr
    assert "@" not in r.stdout.replace("multi-user", "")
    assert "User=alice" in r.stdout and "/srv/alice/desktop-pet/main.py" in r.stdout
    assert "EnvironmentFile=-/etc/desktop-pet/env" in r.stdout
    assert "警告" in r.stderr  # 讀取舊版 deploy.env 時印警告到 stderr


def test_common_sh_priority_order(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("sh"):
        return
    sys_env = tmp_path / "etc-env"
    deploy_env = tmp_path / "deploy.env"
    custom_env = tmp_path / "custom.env"
    sys_env.write_text("PET_USER=sys_user\nPET_HOME=/srv/sys\n", encoding="utf-8")
    deploy_env.write_text("PET_USER=legacy_user\nPET_HOME=/srv/legacy\n", encoding="utf-8")
    custom_env.write_text("PET_USER=custom_user\nPET_HOME=/srv/custom\n", encoding="utf-8")

    script = f'. "{ROOT}/tools/deploy/common.sh"; load_deploy_env && printf "%s|%s" "$PET_USER" "$PET_HOME"'
    # 1. /etc/desktop-pet/env 優先於舊版 deploy.env，且不印舊版警告
    r1 = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                        env={"PATH": "/usr/bin:/bin", "PET_REPO_ROOT": str(tmp_path), "PET_SYS_ENV": str(sys_env)})
    assert r1.returncode == 0 and r1.stdout == "sys_user|/srv/sys"
    assert "警告" not in r1.stderr

    # 2. PET_DEPLOY_ENV 優先於 /etc/desktop-pet/env
    r2 = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                        env={"PATH": "/usr/bin:/bin", "PET_REPO_ROOT": str(tmp_path),
                             "PET_SYS_ENV": str(sys_env), "PET_DEPLOY_ENV": str(custom_env)})
    assert r2.returncode == 0 and r2.stdout == "custom_user|/srv/custom"

    # 3. 呼叫端已設的環境變數優先於環境檔
    r3 = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                        env={"PATH": "/usr/bin:/bin", "PET_REPO_ROOT": str(tmp_path),
                             "PET_SYS_ENV": str(sys_env), "PET_DEPLOY_ENV": str(custom_env),
                             "PET_USER": "shell_user", "PET_HOME": "/srv/shell"})
    assert r3.returncode == 0 and r3.stdout == "shell_user|/srv/shell"


def test_require_sys_env(tmp_path):
    import shutil
    import subprocess
    import pytest
    if not shutil.which("sh"):
        pytest.skip("sh not available")
    sys_env = tmp_path / "etc-env"
    custom_env = tmp_path / "custom.env"
    missing_custom = tmp_path / "nonexistent.env"
    no_env = tmp_path / "no-env"
    legacy_env = tmp_path / "deploy.env"

    script = f'. "{ROOT}/tools/deploy/common.sh"; load_deploy_env && require_sys_env'

    # 1. 環境檔不存在且無舊版 deploy.env 時失敗（回傳碼非 0 且輸出錯誤引導）
    r1 = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                        env={"PATH": "/usr/bin:/bin", "PET_REPO_ROOT": str(tmp_path), "PET_SYS_ENV": str(no_env)})
    assert r1.returncode != 0
    assert "找不到環境檔" in r1.stderr

    # 2. 只有舊版 deploy.env 時相容通過（rc=0），但輸出警告
    legacy_env.write_text("PET_USER=legacy_user\nPET_HOME=/srv/legacy\n", encoding="utf-8")
    r_legacy = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", "PET_REPO_ROOT": str(tmp_path), "PET_SYS_ENV": str(no_env)})
    assert r_legacy.returncode == 0
    assert "暫由舊版" in r_legacy.stderr

    # 3. 系統環境檔存在時成功
    sys_env.write_text("PET_USER=sys_user\nPET_HOME=/srv/sys\n", encoding="utf-8")
    r2 = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                        env={"PATH": "/usr/bin:/bin", "PET_REPO_ROOT": str(tmp_path), "PET_SYS_ENV": str(sys_env)})
    assert r2.returncode == 0

    # 4. 指定 PET_DEPLOY_ENV 存在時成功（自訂覆寫）
    custom_env.write_text("PET_USER=c_user\nPET_HOME=/srv/custom\n", encoding="utf-8")
    r3 = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                        env={"PATH": "/usr/bin:/bin", "PET_REPO_ROOT": str(tmp_path),
                             "PET_SYS_ENV": str(no_env), "PET_DEPLOY_ENV": str(custom_env)})
    assert r3.returncode == 0

    # 5. 指定 PET_DEPLOY_ENV 指向不存在檔案時失敗
    r4 = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                        env={"PATH": "/usr/bin:/bin", "PET_REPO_ROOT": str(tmp_path),
                             "PET_SYS_ENV": str(no_env), "PET_DEPLOY_ENV": str(missing_custom)})
    assert r4.returncode != 0
    assert "不存在" in r4.stderr


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


def test_supervisor_loads_env_file(tmp_path):
    sup = _sup()
    env_file = tmp_path / "env"
    log_dir = tmp_path / "logs"
    env_file.write_text(
        f"PET_BACKEND_PORT=8095\nPET_SUPERVISOR_LOCK_PORT=18095\nPET_BACKEND_LOG_DIR={log_dir}\n",
        encoding="utf-8",
    )
    loaded = sup._load_env(env_file=str(env_file), environ={})
    assert loaded["PET_BACKEND_PORT"] == "8095"
    assert loaded["PET_SUPERVISOR_LOCK_PORT"] == "18095"
    assert loaded["PET_BACKEND_LOG_DIR"] == str(log_dir)


def test_start_hidden_checks_port():
    raw = (ROOT / "backend" / "start_hidden.cmd").read_bytes()
    raw.decode("ascii")  # cmd.exe 用 cp950 解析，非 ASCII 註解會把行拆壞
    text = raw.decode("ascii")
    assert "PET_BACKEND_PORT=8090" in text
    assert ":%PET_BACKEND_PORT% .*LISTENING" in text and "exit /b 0" in text
    assert "PET_BACKEND_LOG_DIR" in text
