"""測試環境變數與環境檔（KEY=VALUE）的優先順序、空值處理與舊變數相容性（Issue #12 / PROPOSAL.md 16.2）。"""
import contextlib
import importlib.util
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "shared"))

import main as pet_main  # noqa: E402
from pet.env import expand_pet_path, load_pi_env, parse_env_file  # noqa: E402


def _load_admin_mod():
    spec = importlib.util.spec_from_file_location(
        "admin", ROOT / "services" / "desktop-pet-admin" / "admin.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.format(record)
        self.records.append(record)

    def clear(self):
        self.records.clear()

    @contextlib.contextmanager
    def at_level(self, level, logger=None):
        target = logging.getLogger(logger)
        old_level = target.level
        target.setLevel(level)
        self.setLevel(level)
        target.addHandler(self)
        try:
            yield self
        finally:
            target.removeHandler(self)
            target.setLevel(old_level)


class _MonkeyPatch:
    def __init__(self):
        self._undos = []

    def setenv(self, key, value):
        old = os.environ.get(key)
        os.environ[key] = value
        if old is None:
            self._undos.append(lambda: os.environ.pop(key, None))
        else:
            self._undos.append(lambda: os.environ.__setitem__(key, old))

    def delenv(self, key, raising=True):
        if key in os.environ:
            old = os.environ.pop(key)
            self._undos.append(lambda: os.environ.__setitem__(key, old))
        elif raising:
            raise KeyError(key)

    def setattr(self, target, name, value):
        old = getattr(target, name)
        setattr(target, name, value)
        self._undos.append(lambda: setattr(target, name, old))

    def undo(self):
        while self._undos:
            self._undos.pop()()


class EnvConfigTest(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._temp_dir.name)
        self.caplog = _LogCapture()
        self.monkeypatch = _MonkeyPatch()

    def tearDown(self):
        self.monkeypatch.undo()
        self._temp_dir.cleanup()

    def test_parse_env_file_ignores_comments_and_empty_values(self):
        tmp_path = self.tmp_path
        env_file = tmp_path / "env"
        env_file.write_text(
            "# 註解行\n"
            "\n"
            "PET_BACKEND_URL=https://pet.example.com\n"
            "PET_HOTSPOT_URL=\n"
            "PET_PET_ID=seed-002\n",
            encoding="utf-8",
        )
        parsed = parse_env_file(str(env_file))
        self.assertEqual(parsed, {
            "PET_BACKEND_URL": "https://pet.example.com",
            "PET_PET_ID": "seed-002",
        })
        self.assertNotIn("PET_HOTSPOT_URL", parsed)

    def test_pi_env_priority_and_legacy_warning(self):
        tmp_path = self.tmp_path
        caplog = self.caplog
        legacy_dir = tmp_path / "repo"
        legacy_dir.mkdir()
        legacy_file = legacy_dir / "deploy.env"
        legacy_file.write_text("PET_PET_ID=from-legacy\n", encoding="utf-8")

        # 沒有 /etc/desktop-pet/env 時讀舊版 deploy.env 並印 WARNING
        with caplog.at_level(logging.WARNING, logger="pet.env"):
            merged = load_pi_env(
                env_file=None,
                environ={},
                repo_root=str(legacy_dir),
                warn_legacy=True,
            )
        if not os.path.isfile("/etc/desktop-pet/env"):
            self.assertEqual(merged.get("PET_PET_ID"), "from-legacy")
            self.assertTrue(any("deploy.env" in r.message for r in caplog.records))

        # 指定環境檔與程序環境變數：程序環境變數 > 環境檔
        custom_env = tmp_path / "custom.env"
        custom_env.write_text(
            "PET_PET_ID=from-file\nPET_WEB_LISTEN=127.0.0.1:8081\nPET_WIFI_IFACE=\n",
            encoding="utf-8",
        )
        merged2 = load_pi_env(
            env_file=str(custom_env),
            environ={"PET_PET_ID": "from-proc", "PET_WEB_LISTEN": ""},
        )
        self.assertEqual(merged2["PET_PET_ID"], "from-proc")
        # 程序環境變數為空字串時視為未設，保留環境檔的值
        self.assertEqual(merged2["PET_WEB_LISTEN"], "127.0.0.1:8081")
        self.assertNotIn("PET_WIFI_IFACE", merged2)

    def test_main_parse_args_priority_cli_over_env_over_file_over_default(self):
        tmp_path = self.tmp_path
        tok_file = tmp_path / "token"
        tok_file.write_text("my-secret\n", encoding="utf-8")
        env_file = tmp_path / "env"
        env_file.write_text(
            f"PET_HOME={tmp_path}\n"
            f"PET_BACKEND_URL=https://file.example.com\n"
            f"PET_BACKEND_TOKEN_FILE=@HOME@/token\n"
            f"PET_PET_ID=file-pet\n"
            f"PET_PACK_DIR=@HOME@/packs/seed\n"
            f"PET_WEB_LISTEN=127.0.0.1:8082\n"
            f"PET_CAMERA_PENDING_DIR=@HOME@/pending\n"
            f"PET_WIFI_IFACE=wlan1\n"
            f"PET_HOTSPOT_URL=\n",
            encoding="utf-8",
        )

        # 1. 完全沒環境檔也沒參數時：100% 維持內建預設
        empty_env = tmp_path / "nope"
        a0 = pet_main.parse_args([], env={"PET_HOME": str(tmp_path)}, env_file=str(empty_env))
        self.assertIsNone(a0.backend)
        self.assertIsNone(a0.backend_token_file)
        self.assertEqual(a0.pet_id, "seed-001")
        self.assertEqual(a0.pack, "auto")
        self.assertEqual(a0.web, "0.0.0.0:8080")
        self.assertIsNone(a0.camera_pending_dir)
        self.assertEqual(a0.wifi_iface, "wlan0")

        # 2. 環境檔生效（含 @HOME@ 展開）
        a1 = pet_main.parse_args([], env={}, env_file=str(env_file))
        self.assertEqual(a1.backend, "https://file.example.com")
        self.assertEqual(Path(a1.backend_token_file), tok_file)
        self.assertEqual(a1.pet_id, "file-pet")
        self.assertEqual(Path(a1.pack), tmp_path / "packs" / "seed")
        self.assertEqual(a1.web, "127.0.0.1:8082")
        self.assertEqual(Path(a1.camera_pending_dir), tmp_path / "pending")
        self.assertEqual(a1.wifi_iface, "wlan1")

        # 3. 程序環境變數覆蓋環境檔
        a2 = pet_main.parse_args(
            [],
            env={"PET_BACKEND_URL": "https://proc.example.com", "PET_PET_ID": "proc-pet"},
            env_file=str(env_file),
        )
        self.assertEqual(a2.backend, "https://proc.example.com")
        self.assertEqual(a2.pet_id, "proc-pet")
        self.assertEqual(a2.web, "127.0.0.1:8082")

        # 4. 命令列參數覆蓋程序環境變數與環境檔
        a3 = pet_main.parse_args(
            ["--backend", "https://cli.example.com", "--pet-id", "cli-pet", "--web", "off"],
            env={"PET_BACKEND_URL": "https://proc.example.com", "PET_PET_ID": "proc-pet"},
            env_file=str(env_file),
        )
        self.assertEqual(a3.backend, "https://cli.example.com")
        self.assertEqual(a3.pet_id, "cli-pet")
        self.assertEqual(a3.web, "off")

    def test_legacy_pet_backend_warns_and_pet_backend_url_wins(self):
        tmp_path = self.tmp_path
        caplog = self.caplog
        empty_env = tmp_path / "nope"
        with caplog.at_level(logging.WARNING, logger="main"):
            a1 = pet_main.parse_args(
                [],
                env={"PET_BACKEND": "http://192.0.2.10:8090"},
                env_file=str(empty_env),
            )
        self.assertEqual(a1.backend, "http://192.0.2.10:8090")
        self.assertTrue(any("PET_BACKEND_URL" in r.message for r in caplog.records))

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="main"):
            a2 = pet_main.parse_args(
                [],
                env={
                    "PET_BACKEND_URL": "https://new.example.com",
                    "PET_BACKEND": "http://192.0.2.10:8090",
                },
                env_file=str(empty_env),
            )
        self.assertEqual(a2.backend, "https://new.example.com")
        self.assertFalse(any("PET_BACKEND_URL" in r.message for r in caplog.records))

    def test_default_token_file_used_only_when_exists(self):
        tmp_path = self.tmp_path
        empty_env = tmp_path / "nope"
        cfg_dir = tmp_path / ".config" / "desktop-pet"
        cfg_dir.mkdir(parents=True)
        tok = cfg_dir / "backend-token"

        # 檔案不存在時為 None（不報錯）
        a1 = pet_main.parse_args([], env={"PET_HOME": str(tmp_path)}, env_file=str(empty_env))
        self.assertIsNone(a1.backend_token_file)

        # 檔案存在時自動帶入預設路徑
        tok.write_text("auto-tok\n", encoding="utf-8")
        a2 = pet_main.parse_args([], env={"PET_HOME": str(tmp_path)}, env_file=str(empty_env))
        self.assertEqual(Path(a2.backend_token_file), tok)

    def test_admin_parse_args_priority(self):
        tmp_path = self.tmp_path
        admin = _load_admin_mod()
        env_file = tmp_path / "env"
        env_file.write_text("PET_WEB_LISTEN=127.0.0.1:8085\nPET_WIFI_IFACE=wlan2\n", encoding="utf-8")

        a1 = admin.parse_args([], env={}, env_file=str(env_file))
        self.assertEqual(a1.listen, "127.0.0.1:8085")
        self.assertEqual(a1.wifi_iface, "wlan2")

        a2 = admin.parse_args([], env={"PET_WEB_LISTEN": "127.0.0.1:8086"}, env_file=str(env_file))
        self.assertEqual(a2.listen, "127.0.0.1:8086")

        a3 = admin.parse_args(
            ["--listen", "127.0.0.1:8087", "--wifi-iface", "wlan3"],
            env={"PET_WEB_LISTEN": "127.0.0.1:8086"},
            env_file=str(env_file),
        )
        self.assertEqual(a3.listen, "127.0.0.1:8087")
        self.assertEqual(a3.wifi_iface, "wlan3")
        self.assertEqual(expand_pet_path("@HOME@/x", {"PET_HOME": "/srv/u"}), "/srv/u/x")

    def test_missing_backend_url_warns(self):
        tmp_path = self.tmp_path
        caplog = self.caplog
        monkeypatch = self.monkeypatch
        empty_env = tmp_path / "nope"
        # parse_args 階段不應在 basicConfig 之前輸出警告
        with caplog.at_level(logging.WARNING, logger="main"):
            a = pet_main.parse_args([], env={}, env_file=str(empty_env))
        self.assertIsNone(a.backend)
        self.assertFalse(any("未設定後端" in r.message for r in caplog.records))

        # main 執行時在 basicConfig 之後格式化輸出警告，且只輸出一次
        caplog.clear()
        monkeypatch.setenv("PET_ENV_FILE", str(empty_env))
        monkeypatch.delenv("PET_BACKEND_URL", raising=False)
        monkeypatch.delenv("PET_BACKEND", raising=False)
        monkeypatch.setattr(pet_main, "PetApp", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("stop early")))
        with caplog.at_level(logging.WARNING, logger="main"):
            try:
                pet_main.main(["--input", "none", "--sink", "null", "--control-socket", "off", "--frames-socket", "off", "--wifi", "off"])
            except RuntimeError:
                pass
        warns = [r.message for r in caplog.records if "未設定後端" in r.message]
        self.assertEqual(len(warns), 1)
        self.assertIn("以離線單機模式執行", warns[0])


if __name__ == "__main__":
    unittest.main()
