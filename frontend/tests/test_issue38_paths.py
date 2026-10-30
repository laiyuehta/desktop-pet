"""#38 搬家後的路徑：動畫包只在 frontend/packs、deploy.env 只在 repo 根目錄（所有讀取端一致）、piwifi 打包輸出在 repo 內。"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

FRONTEND = Path(__file__).resolve().parents[1]
REPO = FRONTEND.parent

import main as pet_main  # noqa: E402
import pet.env as pet_env  # noqa: E402


def _admin():
    import importlib.util
    spec = importlib.util.spec_from_file_location("admin38", FRONTEND / "services" / "desktop-pet-admin" / "admin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class AutoPackTest(unittest.TestCase):
    def test_candidates_are_external_then_frontend_packs_only(self):
        c = pet_main.auto_pack_candidates("seed")
        self.assertEqual(c[1:], [str(FRONTEND / "packs" / "seed")])
        self.assertNotIn(str(REPO / "packs" / "seed"), c)  # 舊的 repo 根目錄 packs/ 不再找（部署時手動搬）

    def test_frontend_pack_found(self):
        new = str(FRONTEND / "packs" / "seed")
        with mock.patch.object(pet_main.AnimationPack, "try_load", side_effect=lambda p: p if p == new else None):
            self.assertEqual(pet_main.find_auto_pack("seed"), new)


class DeployEnvLocationTest(unittest.TestCase):
    def test_admin_and_pet_env_agree_on_repo_root(self):
        admin = _admin()
        self.assertEqual(admin.LEGACY_DEPLOY_ENV, str(REPO / "deploy.env"))
        self.assertEqual(os.path.abspath(pet_env.LEGACY_DEPLOY_ENV), admin.LEGACY_DEPLOY_ENV)

    def test_admin_reads_root_deploy_env(self):
        admin = _admin()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "deploy.env"
            p.write_text("PET_PET_ID=from-root\n", encoding="utf-8")
            admin.LEGACY_DEPLOY_ENV = str(p)
            admin.DEFAULT_PI_ENV_FILE = str(Path(d) / "missing")
            self.assertEqual(admin._load_admin_env(environ={})["PET_PET_ID"], "from-root")


class PiwifiPackageTest(unittest.TestCase):
    def test_package_sh_repo_root_is_repo(self):
        if not shutil.which("bash"):
            raise unittest.SkipTest("bash not available")
        script = (REPO / "piwifi" / "package.sh").read_text(encoding="utf-8")
        line = next(l for l in script.splitlines() if l.startswith("REPO_ROOT="))
        out = subprocess.run(["bash", "-c", f'SCRIPT_DIR="{REPO / "piwifi"}"; {line}; echo "$REPO_ROOT"'],
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(Path(out).resolve(), REPO.resolve())


class SharedWiringTest(unittest.TestCase):
    def test_requirements_start_with_shared(self):
        for side in ("frontend", "backend"):
            first = (REPO / side / "requirements.txt").read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(first, "-e ../shared", side)

    def test_emotion_map_covers_shared_protocol(self):
        from shared.protocol import EMOTIONS
        from pet.backend_client import EMOTION_TO_EXPRESSION
        self.assertEqual(set(EMOTION_TO_EXPRESSION), set(EMOTIONS))


if __name__ == "__main__":
    unittest.main()
