"""#52 審查：rollback 失敗時不動服務、venv 可離線建立、沒有 venv 也能 import shared（不 crash loop）。"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1]
REPO = FRONTEND.parent


def _copy_tree(dst: Path) -> None:
    for rel in ["frontend/services/desktop-pet-admin/rollback.sh", "frontend/services/desktop-pet/desktop-pet.service",
                "frontend/tools/deploy/common.sh", "frontend/requirements.txt", "shared"]:
        src = REPO / rel
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, target, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
        else:
            shutil.copy2(src, target)


class RollbackSafetyTest(unittest.TestCase):
    def test_rollback_aborts_before_touching_services_when_venv_fails(self):
        if not shutil.which("sh"):
            raise unittest.SkipTest("sh not available")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            _copy_tree(root)
            fake = Path(d) / "bin"
            fake.mkdir()
            log = Path(d) / "calls.log"
            for name, body in {"python3": "exit 1", "sudo": 'echo "sudo $*" >> "$CALLS"; exit 0',
                               "systemctl": 'echo "systemctl $*" >> "$CALLS"; exit 0'}.items():
                p = fake / name
                p.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
                p.chmod(0o755)
            env_file = Path(d) / "env"
            env_file.write_text("PET_USER=alice\nPET_HOME=/srv/alice\nPET_BACKEND_URL=https://b.example.org\n",
                                encoding="utf-8")
            r = subprocess.run(["sh", str(root / "frontend/services/desktop-pet-admin/rollback.sh")],
                               capture_output=True, text=True,
                               env={"PATH": f"{fake}:/usr/bin:/bin", "CALLS": str(log),
                                    "PET_DEPLOY_ENV": str(env_file)})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("未做任何變更", r.stderr)
            calls = log.read_text(encoding="utf-8") if log.exists() else ""
            self.assertNotIn("stop", calls)
            self.assertNotIn("disable", calls)
            self.assertNotIn("install", calls)


class OfflineVenvTest(unittest.TestCase):
    def test_ensure_frontend_venv_works_offline(self):
        if not shutil.which("sh"):
            raise unittest.SkipTest("sh not available")
        try:
            import PIL  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("system Pillow not available")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            _copy_tree(root)
            probe = subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(Path(d) / "probe")],
                                   capture_output=True)
            if probe.returncode != 0:
                raise unittest.SkipTest("python venv module not available")
            env = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "PIP_NO_INDEX": "1",
                   "PET_REPO_ROOT": str(root)}
            for _ in range(2):  # 第二次（已有 venv）也要能離線成功
                r = subprocess.run(["sh", "-c", '. "$PET_REPO_ROOT/frontend/tools/deploy/common.sh"; ensure_frontend_venv'],
                                   capture_output=True, text=True, env=env)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            py = root / "frontend/.venv/bin/python"
            out = subprocess.run([str(py), "-c", "import shared; print(shared.__file__)"],
                                 capture_output=True, text=True, check=True).stdout
            self.assertTrue(out.strip().startswith(str(root / "shared")), out)


class SharedFallbackTest(unittest.TestCase):
    def _run(self, args, cwd):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        return subprocess.run([sys.executable, "-s", *args], cwd=cwd, capture_output=True, text=True, env=env)

    def test_frontend_main_without_installed_shared(self):
        with tempfile.TemporaryDirectory() as d:
            r = self._run([str(FRONTEND / "main.py"), "--help"], d)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_backend_without_installed_shared(self):
        r = self._run(["-m", "backend.keys", "--help"], str(REPO))
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
