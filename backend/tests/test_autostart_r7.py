"""R7 開機自動啟動：supervise.py 與 start_hidden.cmd（Pi 的部分在 frontend/tests/test_autostart.py）。"""
import importlib.util
import socket
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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


class AutostartR7Test(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._temp_dir.name)
        self.monkeypatch = _MonkeyPatch()

    def tearDown(self):
        self.monkeypatch.undo()
        self._temp_dir.cleanup()

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

    def test_r7_uses_backend_venv(self):
        ps1 = (ROOT / "backend" / "autostart" / "install_task.ps1").read_text(encoding="utf-8")
        self.assertIn("backend\\.venv\\Scripts\\pythonw.exe", ps1)
        self.assertNotIn("Get-Command pythonw.exe", ps1)
        cmd = (ROOT / "backend" / "start_hidden.cmd").read_bytes().decode("ascii")
        self.assertIn(".venv\\Scripts\\python.exe", cmd)
        self.assertIn("shared", cmd)
        self.assertTrue((ROOT / "backend" / "setup_venv.ps1").is_file())
        req = (ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(req[0], "-e ../shared")


if __name__ == "__main__":
    unittest.main()
