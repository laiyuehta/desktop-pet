"""#38 搬家後的路徑：動畫包只在 repo 外的 ~/.local/share/desktop-pet/packs、deploy.env 只在 repo 根目錄（所有讀取端一致）、piwifi 打包輸出在 repo 內。"""
import logging
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
    def test_xdg_data_home_respected(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/srv/xdg"}):
            self.assertEqual(pet_main.auto_pack_candidates("seed"), ["/srv/xdg/desktop-pet/packs/seed"])

    def test_relative_xdg_ignored_and_bake_shares_default(self):
        from pet.pack import default_packs_dir
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "rel/path"}):
            self.assertEqual(default_packs_dir(), os.path.expanduser("~/.local/share/desktop-pet/packs"))
        src = (FRONTEND / "tools" / "bake.py").read_text(encoding="utf-8")
        self.assertIn("default_packs_dir()", src)

    def test_missing_pack_warns_loudly(self):
        import io
        import contextlib
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": "/srv/none", "JOURNAL_STREAM": ""}), contextlib.redirect_stderr(err):
            with self.assertLogs("t38w", level="WARNING") as cm:
                pet_main.warn_no_auto_pack("seed", logging.getLogger("t38w"))
        for text in ("\n".join(cm.output), err.getvalue()):
            self.assertIn("/srv/none/desktop-pet/packs/seed", text)
            self.assertIn("即時骨架繪圖", text)

    def test_auto_pack_only_outside_repo(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_DATA_HOME"}
        with mock.patch.dict(os.environ, env, clear=True):
            c = pet_main.auto_pack_candidates("seed")
            self.assertEqual(c, [os.path.join(os.path.expanduser("~/.local/share/desktop-pet/packs"), "seed")])
        for path in c:
            self.assertFalse(Path(path).resolve().is_relative_to(REPO.resolve()), path)

    def test_bake_default_output_outside_repo(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("bake38", FRONTEND / "tools" / "bake.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.DEFAULT_PACKS_DIR, "~/.local/share/desktop-pet/packs")

    def test_pack_found(self):
        want = pet_main.auto_pack_candidates("seed")[0]
        with mock.patch.object(pet_main.AnimationPack, "try_load", side_effect=lambda p: p if p == want else None):
            self.assertEqual(pet_main.find_auto_pack("seed"), want)


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
