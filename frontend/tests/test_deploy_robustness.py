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
_had_venv = (FRONTEND / ".venv").exists()


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
            self.assertIn("服務未變更", r.stderr)
            calls = log.read_text(encoding="utf-8") if log.exists() else ""
            self.assertNotIn("stop", calls)
            self.assertNotIn("disable", calls)
            self.assertNotIn("install", calls)


class OfflineVenvTest(unittest.TestCase):
    """在暫存目錄的 repo 複本裡建 venv（不碰真的 repo）。只依賴 PATH 上的 python3：
    它沒有 venv 模組或沒有（系統層級的）Pillow 時直接 skip，結果不看目前執行測試的 python。"""

    def test_ensure_frontend_venv_works_offline(self):
        if not shutil.which("sh") or not shutil.which("python3"):
            raise unittest.SkipTest("sh/python3 not available")
        with tempfile.TemporaryDirectory() as d:
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PIP_NO_INDEX": "1", "HOME": d}
            # ensure_frontend_venv 用的是 PATH 上的 python3：先用同樣的條件探測
            probe = Path(d) / "probe"
            r = subprocess.run(["python3", "-m", "venv", "--system-site-packages", str(probe)],
                               capture_output=True, env=env)
            if r.returncode != 0:
                raise unittest.SkipTest("python3 venv module not available")
            r = subprocess.run([str(probe / "bin" / "python"), "-s", "-c", "import PIL"], capture_output=True, env=env)
            if r.returncode != 0:
                raise unittest.SkipTest("python3 has no system-level Pillow (apt python3-pil); offline venv not possible here")
            root = Path(d) / "repo"
            _copy_tree(root)
            env["PET_REPO_ROOT"] = str(root)
            for _ in range(2):  # 第二次（已有 venv）也要能離線成功
                r = subprocess.run(["sh", "-c", '. "$PET_REPO_ROOT/frontend/tools/deploy/common.sh"; ensure_frontend_venv'],
                                   capture_output=True, text=True, env=env)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            py = root / "frontend/.venv/bin/python"
            out = subprocess.run([str(py), "-c", "import shared; print(shared.__file__)"],
                                 capture_output=True, text=True, check=True, env=env).stdout
            self.assertTrue(out.strip().startswith(str(root / "shared")), out)
            # requirements.txt 新增依賴 → 會重跑 pip（離線裝不到就失敗，不會假裝成功）
            req = root / "frontend/requirements.txt"
            req.write_text(req.read_text(encoding="utf-8") + "desktop-pet-no-such-package==0.0.1\n", encoding="utf-8")
            r = subprocess.run(["sh", "-c", '. "$PET_REPO_ROOT/frontend/tools/deploy/common.sh"; ensure_frontend_venv'],
                               capture_output=True, text=True, env=env)
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((FRONTEND / ".venv").exists() and not _had_venv, "測試不可在 repo 裡建立 frontend/.venv")


class SharedFallbackTest(unittest.TestCase):
    """真的測到 fallback：用 python -S -E（不載入 site-packages、不看 PYTHONPATH），裝好的 shared 套件就看不到；
    只有 pet/__init__.py、backend/__init__.py 的 _ensure_shared_importable() 會把 repo 的 shared/ 加進 sys.path。
    拿掉 fallback 這兩個測試就會失敗。"""

    def _run(self, code):
        with tempfile.TemporaryDirectory() as d:
            return subprocess.run([sys.executable, "-S", "-E", "-c", code], cwd=d, capture_output=True, text=True,
                                  env={"PATH": os.environ.get("PATH", "")})

    def _check(self, r):
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.strip().splitlines()
        self.assertEqual(lines[0], "not-installed", "shared 不應該在 -S 環境中直接可用（測試前提）")
        self.assertTrue(lines[1].startswith(str(REPO / "shared" / "shared")), lines[1])

    def test_frontend_pet_falls_back_to_repo_shared(self):
        r = self._run(
            "import sys, importlib.util\n"
            f"sys.path.insert(0, {str(FRONTEND)!r})\n"
            "s = importlib.util.find_spec('shared')\n"
            "print('not-installed' if s is None or not s.origin else 'installed')\n"
            "import pet\n"
            "import shared.apikey, shared.protocol, shared.envfile\n"
            "print(shared.__file__)\n")
        self._check(r)

    def test_backend_falls_back_to_repo_shared(self):
        r = self._run(
            "import sys, importlib.util\n"
            f"sys.path.insert(0, {str(REPO)!r})\n"
            "s = importlib.util.find_spec('shared')\n"
            "print('not-installed' if s is None or not s.origin else 'installed')\n"
            "import backend.keys\n"
            "import shared\n"
            "print(shared.__file__)\n")
        self._check(r)

if __name__ == "__main__":
    unittest.main()
