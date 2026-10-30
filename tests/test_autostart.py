"""開機自動啟動的設定檢查：Pi 的 systemd unit、common.sh、R7 的 supervise.py 與 start_hidden.cmd。"""
import configparser
import importlib.util
import os
import re
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "frontend" / "services" / "desktop-pet" / "desktop-pet.service"
SPLIT_UNIT = ROOT / "frontend" / "services" / "desktop-pet" / "desktop-pet-split.service"
ADMIN_UNIT = ROOT / "frontend" / "services" / "desktop-pet-admin" / "desktop-pet-admin.service"


def _unit(path: Path = UNIT):
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str
    cp.read_string(path.read_text(encoding="utf-8"))
    return cp


def _sup():
    spec = importlib.util.spec_from_file_location("supervise", ROOT / "backend" / "autostart" / "supervise.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _MonkeyPatch:
    def __init__(self):
        self._undos = []

    def setattr(self, target, name, value):
        old = getattr(target, name)
        setattr(target, name, value)
        self._undos.append(lambda: setattr(target, name, old))

    def undo(self):
        while self._undos:
            self._undos.pop()()


class AutostartTest(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._temp_dir.name)
        self.monkeypatch = _MonkeyPatch()

    def tearDown(self):
        self.monkeypatch.undo()
        self._temp_dir.cleanup()

    def test_unit_never_hard_depends_on_piscreen(self):
        text = "\n".join(l for l in UNIT.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
        for bad in ("Requires=", "BindsTo=", "PartOf=", "Requisite=", "PropagatesStopTo=", "PropagatesReloadTo="):
            self.assertNotIn(bad, text)
        u = _unit()["Unit"]
        for name in ("piscreen.service", "picam.service", "network-online.target"):
            self.assertIn(name, u["After"].split())
            self.assertIn(name, u["Wants"].split())

    def test_unit_service_settings(self):
        s = _unit()["Service"]
        self.assertEqual(s["User"], "@USER@")
        self.assertEqual(s["Group"], "@USER@")
        self.assertEqual(s["Restart"], "always")
        self.assertEqual(s["WorkingDirectory"], "@HOME@/desktop-pet/frontend")
        self.assertEqual(s["EnvironmentFile"], "-/etc/desktop-pet/env")
        self.assertEqual(_unit()["Install"]["WantedBy"], "multi-user.target")
        args = s["ExecStart"].split()
        self.assertEqual(args[:2], ["/usr/bin/python3", "@HOME@/desktop-pet/frontend/main.py"])
        flags = dict(zip(args[2::2], args[3::2]))
        self.assertEqual(flags, {
            "--sink": "piscreen", "--input": "web", "--fps": "20",
        })
        self.assertNotIn("@BACKEND_URL@", s["ExecStart"])
        self.assertNotIn("--backend-token ", s["ExecStart"] + " ")

        sp = _unit(SPLIT_UNIT)["Service"]
        self.assertEqual(sp["EnvironmentFile"], "-/etc/desktop-pet/env")
        self.assertIn("--web off", sp["ExecStart"])
        self.assertIn("--wifi off", sp["ExecStart"])
        self.assertNotIn("@BACKEND_URL@", sp["ExecStart"])

        adm = _unit(ADMIN_UNIT)["Service"]
        self.assertEqual(adm["EnvironmentFile"], "-/etc/desktop-pet/env")

    def test_deploy_files_only_use_placeholders(self):
        files = [
            "frontend/services/desktop-pet/desktop-pet.service",
            "frontend/services/desktop-pet/desktop-pet-split.service",
            "frontend/services/desktop-pet-admin/desktop-pet-admin.service",
            "frontend/services/picam/picam.service",
            "frontend/tools/wifi/50-desktop-pet-wifi.rules"
        ]
        for rel in files:
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("@USER@", text, rel)
            self.assertNotIn("/home/", text, rel)
            self.assertNotIn("pi-user", text, rel)
        pc = (ROOT / "frontend" / "services" / "picam" / "picam.service").read_text(encoding="utf-8")
        self.assertIn("User=@USER@", pc)
        self.assertIn("ExecStart=/usr/bin/python3 @HOME@/picam/picamd.py", pc)
        self.assertIn("EnvironmentFile=-/etc/desktop-pet/env", pc)

    def test_deploy_env_example_and_gitignore(self):
        ex = (ROOT / "deploy.env.example").read_text(encoding="utf-8")
        lines = [l for l in ex.splitlines() if l and not l.startswith("#")]
        keys = [l.split("=", 1)[0] for l in lines]
        self.assertEqual(len(keys), len(set(keys)))
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
        self.assertTrue(expected_active_keys.issubset(set(kv)))
        self.assertEqual(kv["PET_BACKEND_URL"], "https://pet.example.com")
        self.assertEqual(kv["PET_PET_ID"], "seed-001")
        self.assertEqual(kv["PET_PACK_DIR"], "")
        self.assertEqual(kv["PET_WEB_LISTEN"], "0.0.0.0:8080")
        self.assertEqual(kv["PET_BACKEND_PORT"], "8090")
        self.assertIn("#PET_AUTOUPDATE=1", ex)
        self.assertIn("#PET_CONSOLE_LISTEN=", ex)
        self.assertIn("deploy.env", (ROOT / ".gitignore").read_text(encoding="utf-8").split())

    def _render(self, template, env_text, extra_env=None):
        (self.tmp_path / "deploy.env").write_text(env_text, encoding="utf-8")
        common_sh = (ROOT / "frontend" / "tools" / "deploy" / "common.sh").as_posix()
        template_path = (ROOT / template).as_posix()
        script = f'. "{common_sh}"; load_deploy_env && render_template "{template_path}"'
        env = {
            "PATH": "/usr/bin:/bin",
            "PET_REPO_ROOT": self.tmp_path.as_posix(),
            "PET_SYS_ENV": (self.tmp_path / "no-sys-env").as_posix()
        }
        if extra_env:
            env.update(extra_env)
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env)

    def test_render_substitutes_all_placeholders(self):
        if not shutil.which("sh"):
            raise unittest.SkipTest("sh not available")
        r = self._render("frontend/services/desktop-pet/desktop-pet.service",
                         "PET_USER=alice\nPET_HOME=/srv/alice\nPET_BACKEND_URL=https://b.example.org\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("@", r.stdout.replace("multi-user", ""))
        self.assertIn("User=alice", r.stdout)
        self.assertIn("/srv/alice/desktop-pet/frontend/main.py", r.stdout)
        self.assertIn("EnvironmentFile=-/etc/desktop-pet/env", r.stdout)
        self.assertIn("警告", r.stderr)

    def test_common_sh_priority_order(self):
        if not shutil.which("sh"):
            raise unittest.SkipTest("sh not available")
        tmp_path = self.tmp_path
        sys_env = tmp_path / "etc-env"
        deploy_env = tmp_path / "deploy.env"
        custom_env = tmp_path / "custom.env"
        sys_env.write_text("PET_USER=sys_user\nPET_HOME=/srv/sys\n", encoding="utf-8")
        deploy_env.write_text("PET_USER=legacy_user\nPET_HOME=/srv/legacy\n", encoding="utf-8")
        custom_env.write_text("PET_USER=custom_user\nPET_HOME=/srv/custom\n", encoding="utf-8")

        common_sh = (ROOT / "frontend" / "tools" / "deploy" / "common.sh").as_posix()
        script = f'. "{common_sh}"; load_deploy_env && printf "%s|%s" "$PET_USER" "$PET_HOME"'
        base_env = {"PATH": "/usr/bin:/bin", "PET_REPO_ROOT": tmp_path.as_posix()}

        # 1. /etc/desktop-pet/env 優先於舊版 deploy.env，且不印舊版警告
        r1 = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            env=dict(base_env, PET_SYS_ENV=sys_env.as_posix()))
        self.assertEqual(r1.returncode, 0)
        self.assertEqual(r1.stdout, "sys_user|/srv/sys")
        self.assertNotIn("警告", r1.stderr)

        # 2. PET_DEPLOY_ENV 優先於 /etc/desktop-pet/env
        r2 = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            env=dict(base_env, PET_SYS_ENV=sys_env.as_posix(), PET_DEPLOY_ENV=custom_env.as_posix()))
        self.assertEqual(r2.returncode, 0)
        self.assertEqual(r2.stdout, "custom_user|/srv/custom")

        # 3. 呼叫端已設的環境變數優先於環境檔
        r3 = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            env=dict(base_env, PET_SYS_ENV=sys_env.as_posix(), PET_DEPLOY_ENV=custom_env.as_posix(),
                                     PET_USER="shell_user", PET_HOME="/srv/shell"))
        self.assertEqual(r3.returncode, 0)
        self.assertEqual(r3.stdout, "shell_user|/srv/shell")

    def test_require_sys_env(self):
        if not shutil.which("sh"):
            raise unittest.SkipTest("sh not available")
        tmp_path = self.tmp_path
        sys_env = tmp_path / "etc-env"
        custom_env = tmp_path / "custom.env"
        missing_custom = tmp_path / "nonexistent.env"
        no_env = tmp_path / "no-env"
        legacy_env = tmp_path / "deploy.env"

        common_sh = (ROOT / "frontend" / "tools" / "deploy" / "common.sh").as_posix()
        script = f'. "{common_sh}"; load_deploy_env && require_sys_env'
        base_env = {"PATH": "/usr/bin:/bin", "PET_REPO_ROOT": tmp_path.as_posix(), "PET_SYS_ENV": no_env.as_posix()}

        # 1. 環境檔不存在且無舊版 deploy.env 時失敗（回傳碼非 0 且輸出錯誤引導）
        r1 = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=base_env)
        self.assertNotEqual(r1.returncode, 0)
        self.assertIn("找不到環境檔", r1.stderr)
        self.assertIn("錯誤：", r1.stderr)

        # 2. 只有舊版 deploy.env 時相容通過（rc=0），但輸出警告
        legacy_env.write_text("PET_USER=legacy_user\nPET_HOME=/srv/legacy\n", encoding="utf-8")
        r_legacy = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=base_env)
        self.assertEqual(r_legacy.returncode, 0)
        self.assertIn("暫由舊版", r_legacy.stderr)

        # 3. 系統環境檔存在時成功
        sys_env.write_text("PET_USER=sys_user\nPET_HOME=/srv/sys\n", encoding="utf-8")
        env_3 = dict(base_env, PET_SYS_ENV=sys_env.as_posix())
        r2 = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env_3)
        self.assertEqual(r2.returncode, 0)

        # 4. 指定 PET_DEPLOY_ENV 存在時成功（自訂覆寫）
        custom_env.write_text("PET_USER=c_user\nPET_HOME=/srv/custom\n", encoding="utf-8")
        env_4 = dict(base_env, PET_DEPLOY_ENV=custom_env.as_posix())
        r3 = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env_4)
        self.assertEqual(r3.returncode, 0)

        # 5. 指定 PET_DEPLOY_ENV 指向不存在檔案時失敗
        env_5 = dict(base_env, PET_DEPLOY_ENV=missing_custom.as_posix())
        r4 = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env_5)
        self.assertNotEqual(r4.returncode, 0)
        self.assertIn("不存在", r4.stderr)

        # 6. require_sys_env --warn：環境檔不存在時不中止（rc=0）且輸出「警告：」非「錯誤：」
        script_warn = f'. "{common_sh}"; load_deploy_env && require_sys_env --warn'
        r_warn = subprocess.run(["sh", "-c", script_warn], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", env=base_env)
        self.assertEqual(r_warn.returncode, 0)
        self.assertIn("警告：找不到環境檔", r_warn.stderr)
        self.assertNotIn("錯誤", r_warn.stderr)

        # 7. require_sys_env --warn：PET_DEPLOY_ENV 指向不存在檔案但系統環境檔存在時（rc=0），輸出警告且不中斷
        env_7 = dict(base_env, PET_SYS_ENV=sys_env.as_posix(), PET_DEPLOY_ENV=missing_custom.as_posix())
        r_missing_custom_warn = subprocess.run(
            ["sh", "-c", script_warn], capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env_7
        )
        self.assertEqual(r_missing_custom_warn.returncode, 0)
        self.assertIn("警告：PET_DEPLOY_ENV 指定的環境檔不存在", r_missing_custom_warn.stderr)
        self.assertNotIn("錯誤", r_missing_custom_warn.stderr)

    def test_rollback_sh_env_handling(self):
        """驗證 rollback.sh 實際執行：
        (a) PET_DEPLOY_ENV 有設且檔案存在時 exit 0、無「找不到環境檔」警告；
        (b) 完全沒有環境檔時 exit 0 且仍跑到「回退完成」；
        (c) PET_DEPLOY_ENV 指向不存在檔案但系統環境檔存在時印警告且順利完成。
        """
        if not shutil.which("sh") or os.name == "nt":
            raise unittest.SkipTest("sh test requires posix environment")

        stub_bin = self.tmp_path / "bin"
        stub_bin.mkdir()
        for name in ["sudo", "systemctl", "systemd-analyze", "sleep"]:
            stub = stub_bin / name
            stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            stub.chmod(0o755)

        base_path = f"{stub_bin.as_posix()}:{os.environ.get('PATH', '')}"
        sys_env = self.tmp_path / "etc-env"
        custom_env = self.tmp_path / "custom.env"
        no_env = self.tmp_path / "no-env"
        missing_custom = self.tmp_path / "nonexistent.env"
        rollback_script = (ROOT / "frontend" / "services" / "desktop-pet-admin" / "rollback.sh").as_posix()

        base_env = {
            "PATH": base_path,
            "PET_USER": "testuser",
            "PET_HOME": self.tmp_path.as_posix(),
            "PET_BACKEND_URL": "https://test.example.com",
            "PET_SYS_ENV": no_env.as_posix(),
        }

        # (a) PET_DEPLOY_ENV 有設且檔案存在：exit 0，無「找不到環境檔」或「錯誤」訊息
        custom_env.write_text("PET_USER=c_user\nPET_HOME=/srv/custom\nPET_BACKEND_URL=https://c.example.com\n", encoding="utf-8")
        env_a = dict(base_env, PET_DEPLOY_ENV=custom_env.as_posix())
        ra = subprocess.run(["sh", rollback_script], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env_a)
        self.assertEqual(ra.returncode, 0, ra.stderr)
        self.assertIn("回退完成！", ra.stdout)
        self.assertNotIn("找不到環境檔", ra.stderr)
        self.assertNotIn("錯誤", ra.stderr)

        # (b) 完全沒有環境檔：exit 0，印警告指引且仍順利跑到「回退完成」
        env_b = dict(base_env)
        env_b.pop("PET_DEPLOY_ENV", None)
        rb = subprocess.run(["sh", rollback_script], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env_b)
        self.assertEqual(rb.returncode, 0, rb.stderr)
        self.assertIn("回退完成！", rb.stdout)
        self.assertIn("警告：找不到環境檔", rb.stderr)
        self.assertNotIn("錯誤", rb.stderr)

        # (c) PET_DEPLOY_ENV 不存在但系統環境檔存在：exit 0，印警告且順利完成
        sys_env.write_text("PET_USER=s_user\nPET_HOME=/srv/sys\nPET_BACKEND_URL=https://s.example.com\n", encoding="utf-8")
        env_c = dict(base_env, PET_DEPLOY_ENV=missing_custom.as_posix(), PET_SYS_ENV=sys_env.as_posix())
        rc = subprocess.run(["sh", rollback_script], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", env=env_c)
        self.assertEqual(rc.returncode, 0, rc.stderr)
        self.assertIn("回退完成！", rc.stdout)
        self.assertIn("警告：PET_DEPLOY_ENV 指定的環境檔不存在", rc.stderr)
        self.assertNotIn("錯誤", rc.stderr)

    def test_install_scripts_never_write_deploy_env(self):
        for rel in ["frontend/services/desktop-pet/install.sh", "frontend/services/picam/install.sh", "frontend/tools/wifi/install.sh",
                    "frontend/tools/deploy/common.sh", "frontend/services/camcast/start.sh"]:
            code = "\n".join(l for l in (ROOT / rel).read_text(encoding="utf-8").splitlines()
                             if not l.lstrip().startswith("#"))
            self.assertFalse(re.search(r">\s*\S*deploy\.env", code), rel)

    def test_supervisor_single_instance(self):
        sup = _sup()
        self.monkeypatch.setattr(sup, "LOCK_PORT", _free_port())
        first = sup.take_lock()
        try:
            self.assertIsNotNone(first)
            self.assertIsNone(sup.take_lock())
        finally:
            first.close()

    def test_supervisor_skips_when_port_busy_and_restarts(self):
        sup = _sup()
        log_path = self.tmp_path / "sup.log"
        self.monkeypatch.setattr(sup, "LOG", str(log_path))
        busy = iter([True, True, False, False])
        self.monkeypatch.setattr(sup, "port_in_use", lambda port=None: next(busy))
        starts, sleeps = [], []
        sup.run_forever(start=lambda: starts.append(1) or 1, sleep=sleeps.append, max_runs=2)
        self.assertEqual(len(starts), 2)
        self.assertEqual(sleeps[:2], [15, 15])
        self.assertEqual(sleeps[2], 5.0)
        self.assertEqual(sleeps[3], 10.0)
        self.assertIn("不重複啟動", log_path.read_text(encoding="utf-8"))

    def test_supervisor_loads_env_file(self):
        sup = _sup()
        env_file = self.tmp_path / "env"
        log_dir = self.tmp_path / "logs"
        env_file.write_text(
            f"PET_BACKEND_PORT=8095\nPET_SUPERVISOR_LOCK_PORT=18095\nPET_BACKEND_LOG_DIR={log_dir}\n",
            encoding="utf-8",
        )
        loaded = sup._load_env(env_file=str(env_file), environ={})
        self.assertEqual(loaded["PET_BACKEND_PORT"], "8095")
        self.assertEqual(loaded["PET_SUPERVISOR_LOCK_PORT"], "18095")
        self.assertEqual(loaded["PET_BACKEND_LOG_DIR"], str(log_dir))

    def test_start_hidden_checks_port(self):
        raw = (ROOT / "backend" / "start_hidden.cmd").read_bytes()
        raw.decode("ascii")  # cmd.exe 用 cp950 解析，非 ASCII 註解會把行拆壞
        text = raw.decode("ascii")
        self.assertIn("PET_BACKEND_PORT=8090", text)
        self.assertIn(":%PET_BACKEND_PORT% .*LISTENING", text)
        self.assertIn("exit /b 0", text)
        self.assertIn("PET_BACKEND_LOG_DIR", text)


if __name__ == "__main__":
    unittest.main()
