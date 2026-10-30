"""桌寵自動更新器單元測試（PROPOSAL.md 16. 自動部署）。"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
from typing import Any, Dict, List
import unittest
from unittest import mock
import urllib.error

_root = Path(__file__).resolve().parents[2]
if str(_root / "frontend") not in sys.path:
    sys.path.insert(0, str(_root / "frontend"))
if str(_root / "shared") not in sys.path:
    sys.path.insert(0, str(_root / "shared"))

from tools.deploy.autoupdate import (
    AutoUpdater,
    ProcessLock,
    append_updates_jsonl,
    boot_check,
    calculate_manifest,
    check_ci_runs,
    check_held_criteria,
    check_manual_steps,
    create_backup,
    detect_affected_units,
    get_remote_commit,
    load_env,
    load_failed_json,
    load_update_state,
    overwrite_release,
    parse_pkt_lines,
    pre_switch_check,
    record_failed_commit,
    restore_from_backup,
    run_health_check,
    safe_extract_tarball,
    verify_manifest,
    write_manifest,
    write_status_json,
    write_update_state,
)
from pet.version import get_autoupdate_status, get_release_info


class TestGitSmartHttp(unittest.TestCase):
    def test_parse_pkt_lines(self):
        # 建立模擬 pkt-line 資料
        def make_pkt(payload: str) -> bytes:
            b = payload.encode("utf-8")
            return f"{len(b) + 4:04x}".encode("ascii") + b

        raw = make_pkt("# test\n") + b"0000" + make_pkt("870020a123456789abcdef0123456789abcdef01 refs/heads/main\n")
        lines = parse_pkt_lines(raw)
        self.assertIn("# test", lines[0])
        self.assertTrue(any("refs/heads/main" in line for line in lines))

    def test_get_remote_commit_found(self):
        commit_hash = "870020a123456789abcdef0123456789abcdef01"
        line = f"{commit_hash} refs/heads/main\x00capabilities\n"
        pkt_len = f"{len(line) + 4:04x}".encode("ascii")
        mock_data = b"001e# service=git-upload-pack\n0000" + pkt_len + line.encode("utf-8") + b"0000"

        with mock.patch("urllib.request.urlopen") as mock_url:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = mock_data
            mock_resp.__enter__.return_value = mock_resp
            mock_url.return_value = mock_resp

            res = get_remote_commit("https://github.com/laiyuehta/desktop-pet", "main")
            self.assertEqual(res, commit_hash)

    def test_get_remote_commit_not_found(self):
        commit_hash = "870020a123456789abcdef0123456789abcdef01"
        line = f"{commit_hash} refs/heads/other-branch\n"
        pkt_len = f"{len(line) + 4:04x}".encode("ascii")
        mock_data = b"001e# service=git-upload-pack\n0000" + pkt_len + line.encode("utf-8") + b"0000"

        with mock.patch("urllib.request.urlopen") as mock_url:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = mock_data
            mock_resp.__enter__.return_value = mock_resp
            mock_url.return_value = mock_resp

            res = get_remote_commit("https://github.com/laiyuehta/desktop-pet", "main")
            self.assertIsNone(res)


class TestCiCheckRuns(unittest.TestCase):
    def test_check_ci_runs_success(self):
        runs_json = {
            "check_runs": [
                {"name": "tests (Python 3.13)", "status": "completed", "conclusion": "success"},
                {"name": "secrets & personal-info scan", "status": "completed", "conclusion": "success"},
                {"name": "other-check", "status": "in_progress", "conclusion": None},
            ]
        }
        with mock.patch("urllib.request.urlopen") as mock_url:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps(runs_json).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_url.return_value = mock_resp

            status, msg = check_ci_runs("https://github.com/laiyuehta/desktop-pet", "c004feb")
            self.assertEqual(status, "success")

    def test_check_ci_runs_pending(self):
        runs_json = {
            "check_runs": [
                {"name": "tests (Python 3.13)", "status": "in_progress", "conclusion": None},
                {"name": "secrets & personal-info scan", "status": "completed", "conclusion": "success"},
            ]
        }
        with mock.patch("urllib.request.urlopen") as mock_url:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps(runs_json).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_url.return_value = mock_resp

            status, msg = check_ci_runs("https://github.com/laiyuehta/desktop-pet", "c004feb")
            self.assertEqual(status, "pending")

    def test_check_ci_runs_failed(self):
        runs_json = {
            "check_runs": [
                {"name": "tests (Python 3.13)", "status": "completed", "conclusion": "failure"},
                {"name": "secrets & personal-info scan", "status": "completed", "conclusion": "success"},
            ]
        }
        with mock.patch("urllib.request.urlopen") as mock_url:
            mock_resp = mock.MagicMock()
            mock_resp.read.return_value = json.dumps(runs_json).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_url.return_value = mock_resp

            status, msg = check_ci_runs("https://github.com/laiyuehta/desktop-pet", "c004feb")
            self.assertEqual(status, "failed")

    def test_check_ci_runs_rate_limited(self):
        with mock.patch("urllib.request.urlopen") as mock_url:
            mock_url.side_effect = urllib.error.HTTPError("url", 403, "Rate Limited", {}, None)
            status, msg = check_ci_runs("https://github.com/laiyuehta/desktop-pet", "c004feb")
            self.assertEqual(status, "pending")
            self.assertIn("限流", msg)


class TestSafeTarballExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_safe_tarball_normal(self):
        tar_path = self.tmp_dir / "test.tar.gz"
        extract_to = self.tmp_dir / "unpacked"

        with tarfile.open(tar_path, "w:gz") as tar:
            data = b"hello world\n"
            ti = tarfile.TarInfo(name="repo-commit/main.py")
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))

        extracted_root = safe_extract_tarball(tar_path, extract_to)
        self.assertTrue((extracted_root / "main.py").exists())
        self.assertEqual((extracted_root / "main.py").read_bytes(), b"hello world\n")

    def test_safe_tarball_rejects_path_traversal(self):
        tar_path = self.tmp_dir / "evil.tar.gz"
        extract_to = self.tmp_dir / "unpacked"

        with tarfile.open(tar_path, "w:gz") as tar:
            data = b"malicious\n"
            ti = tarfile.TarInfo(name="../escape.txt")
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))

        with self.assertRaises(ValueError):
            safe_extract_tarball(tar_path, extract_to)

    def test_safe_tarball_rejects_absolute_path(self):
        tar_path = self.tmp_dir / "evil_abs.tar.gz"
        extract_to = self.tmp_dir / "unpacked"

        with tarfile.open(tar_path, "w:gz") as tar:
            data = b"malicious\n"
            ti = tarfile.TarInfo(name="/etc/passwd")
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))

        with self.assertRaises(ValueError):
            safe_extract_tarball(tar_path, extract_to)


class TestManifestAndModifications(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_manifest_verification(self):
        (self.tmp_dir / "main.py").write_text("print('test')", encoding="utf-8")
        (self.tmp_dir / "pet").mkdir()
        (self.tmp_dir / "pet" / "app.py").write_text("class PetApp: pass", encoding="utf-8")

        manifest = calculate_manifest(self.tmp_dir)
        self.assertIn("main.py", manifest)
        self.assertIn("pet/app.py", manifest)

        write_manifest(self.tmp_dir, manifest)
        ok, reason = verify_manifest(self.tmp_dir)
        self.assertTrue(ok)
        self.assertIsNone(reason)

        # 修改檔案
        (self.tmp_dir / "main.py").write_text("print('tampered')", encoding="utf-8")
        ok, reason = verify_manifest(self.tmp_dir)
        self.assertFalse(ok)
        self.assertIn("檔案已修改", reason or "")

        # 刪除檔案
        (self.tmp_dir / "main.py").unlink()
        ok, reason = verify_manifest(self.tmp_dir)
        self.assertFalse(ok)
        self.assertIn("檔案遺失", reason or "")


class TestManualStepsAndHeldCriteria(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.state_dir = self.tmp_dir / "state"
        self.state_dir.mkdir()
        self.release_dir = self.tmp_dir / "release"
        self.release_dir.mkdir()
        (self.release_dir / "tools" / "deploy").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_manual_steps_held(self):
        steps_file = self.release_dir / "tools" / "deploy" / "manual-steps.txt"
        steps_file.write_text("0001 pi 建立環境檔\n", encoding="utf-8")

        has_step, reason = check_manual_steps(self.release_dir, self.state_dir, target_platform="pi")
        self.assertTrue(has_step)
        self.assertIn("0001", reason or "")

        # 標記為已套用
        applied_file = self.state_dir / "applied-steps.txt"
        applied_file.write_text("0001 pi 建立環境檔\n", encoding="utf-8")

        has_step, reason = check_manual_steps(self.release_dir, self.state_dir, target_platform="pi")
        self.assertFalse(has_step)

    def test_manual_steps_platform_filter(self):
        steps_file = self.release_dir / "tools" / "deploy" / "manual-steps.txt"
        steps_file.write_text("0002 r7 Windows 排程設定\n", encoding="utf-8")

        has_step, reason = check_manual_steps(self.release_dir, self.state_dir, target_platform="pi")
        self.assertFalse(has_step)

    def test_held_criteria_on_system_files(self):
        old_dir = self.tmp_dir / "old"
        new_dir = self.tmp_dir / "new"
        old_dir.mkdir()
        new_dir.mkdir()

        (old_dir / "services" / "desktop-pet").mkdir(parents=True)
        (new_dir / "services" / "desktop-pet").mkdir(parents=True)
        (old_dir / "services" / "desktop-pet" / "desktop-pet.service").write_text("v1", encoding="utf-8")
        (new_dir / "services" / "desktop-pet" / "desktop-pet.service").write_text("v2", encoding="utf-8")

        is_held, reason = check_held_criteria(old_dir, new_dir, self.state_dir, target_platform="pi")
        self.assertTrue(is_held)
        self.assertIn("systemd", reason or "")

    def test_held_criteria_on_rust_daemon(self):
        old_dir = self.tmp_dir / "old"
        new_dir = self.tmp_dir / "new"
        old_dir.mkdir()
        new_dir.mkdir()

        (old_dir / "services" / "piwifi").mkdir(parents=True)
        (new_dir / "services" / "piwifi").mkdir(parents=True)
        (old_dir / "services" / "piwifi" / "main.rs").write_text("v1", encoding="utf-8")
        (new_dir / "services" / "piwifi" / "main.rs").write_text("v2", encoding="utf-8")

        is_held, reason = check_held_criteria(old_dir, new_dir, self.state_dir, target_platform="pi")
        self.assertTrue(is_held)
        self.assertIn("Rust", reason or "")


class TestSelectiveRestart(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.old_dir = self.tmp_dir / "old"
        self.new_dir = self.tmp_dir / "new"
        self.old_dir.mkdir()
        self.new_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_docs_only_change_no_restart(self):
        (self.old_dir / "README.md").write_text("old doc", encoding="utf-8")
        (self.new_dir / "README.md").write_text("new doc", encoding="utf-8")

        units = detect_affected_units(self.old_dir, self.new_dir)
        self.assertEqual(units, [])

    def test_pet_code_change_restarts_desktop_pet(self):
        (self.old_dir / "pet").mkdir()
        (self.new_dir / "pet").mkdir()
        (self.old_dir / "pet" / "app.py").write_text("v1", encoding="utf-8")
        (self.new_dir / "pet" / "app.py").write_text("v2", encoding="utf-8")

        units = detect_affected_units(self.old_dir, self.new_dir)
        self.assertIn("desktop-pet.service", units)

    def test_picam_change_restarts_picam(self):
        (self.old_dir / "services" / "picam").mkdir(parents=True)
        (self.new_dir / "services" / "picam").mkdir(parents=True)
        (self.old_dir / "services" / "picam" / "picamd.py").write_text("v1", encoding="utf-8")
        (self.new_dir / "services" / "picam" / "picamd.py").write_text("v2", encoding="utf-8")

        units = detect_affected_units(self.old_dir, self.new_dir)
        self.assertIn("picam.service", units)
        self.assertNotIn("desktop-pet.service", units)


class TestBackupAndOverwriteUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.install_dir = self.tmp_dir / "desktop-pet"
        self.state_dir = self.tmp_dir / "state"
        self.backup_dir = self.tmp_dir / "backup"
        self.install_dir.mkdir(parents=True)
        self.state_dir.mkdir(parents=True)

        self.env_file = self.tmp_dir / "env"
        self.env_file.write_text(
            f"PET_INSTALL_DIR={self.install_dir}\n"
            f"PET_STATE_DIR={self.state_dir}\n"
            f"PET_BACKUP_DIR={self.backup_dir}\n"
            "PET_AUTOUPDATE_REQUIRE_CI=0\n",
            encoding="utf-8",
        )
        self.updater = AutoUpdater(env_path=self.env_file)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _make_tarball(self, files: Dict[str, str]) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for rel_path, content in files.items():
                data = content.encode("utf-8")
                ti = tarfile.TarInfo(name=f"repo/{rel_path}")
                ti.size = len(data)
                ti.mtime = 1700000000
                tar.addfile(ti, io.BytesIO(data))
        return buf.getvalue()

    def test_pause_and_dry_run_touches_no_files(self):
        # 建立初始版本檔案
        (self.install_dir / "main.py").write_text("print('v1')", encoding="utf-8")
        packs_cat = self.install_dir / "packs" / "cat"
        packs_cat.mkdir(parents=True)
        (packs_cat / "sprite.png").write_text("cat_bytes", encoding="utf-8")
        (self.install_dir / "deploy.env").write_text("URL=https://pet.example.com", encoding="utf-8")

        # 1. 測試 PAUSE 暫停檔
        (self.state_dir / "PAUSE").write_text("1", encoding="utf-8")
        res = self.updater.run_cycle(force=True)
        self.assertEqual(res, 0)
        self.assertFalse(self.backup_dir.exists())
        self.assertEqual((self.install_dir / "main.py").read_text(encoding="utf-8"), "print('v1')")
        (self.state_dir / "PAUSE").unlink()

        # 2. 測試 PET_AUTOUPDATE=0
        self.updater.conf["PET_AUTOUPDATE"] = "0"
        res = self.updater.run_cycle(force=True)
        self.assertEqual(res, 0)
        self.assertFalse(self.backup_dir.exists())
        self.updater.conf["PET_AUTOUPDATE"] = "1"

        # 3. 測試 dry_run
        self.updater.dry_run = True
        with mock.patch("tools.deploy.autoupdate.get_remote_commit", return_value="1111111111111111111111111111111111111111"):
            res = self.updater.run_cycle(force=True)
            self.assertEqual(res, 0)
            self.assertFalse(self.backup_dir.exists())
            self.assertEqual((self.install_dir / "main.py").read_text(encoding="utf-8"), "print('v1')")

    def test_successful_update_cycle_and_packs_deploy_env_unchanged(self):
        # 初始狀態：v1 包含 main.py、old_feature.py，以及 packs/、deploy.env
        (self.install_dir / "main.py").write_text("print('v1')", encoding="utf-8")
        (self.install_dir / "old_feature.py").write_text("def old(): pass", encoding="utf-8")
        packs_cat = self.install_dir / "packs" / "cat"
        packs_cat.mkdir(parents=True)
        (packs_cat / "sprite.png").write_text("cat_sprite_bytes", encoding="utf-8")
        (self.install_dir / "deploy.env").write_text("URL=https://pet.example.com", encoding="utf-8")
        write_manifest(self.install_dir, calculate_manifest(self.install_dir))

        # 新版本：main.py 更新，新增 new_feature.py，移除 old_feature.py
        new_commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        tar_bytes = self._make_tarball({
            "main.py": "print('v2')",
            "new_feature.py": "def new(): pass",
        })

        with mock.patch("tools.deploy.autoupdate.get_remote_commit", return_value=new_commit), \
             mock.patch("tools.deploy.autoupdate.pre_switch_check", return_value=(True, None)), \
             mock.patch("tools.deploy.autoupdate.run_health_check", return_value=(True, "all ok")), \
             mock.patch("tools.deploy.autoupdate.restart_units") as mock_restart, \
             mock.patch("urllib.request.urlopen") as mock_url:

            mock_url.return_value = io.BytesIO(tar_bytes)

            res = self.updater.run_cycle(force=True)
            self.assertEqual(res, 0)

        # 驗證新版檔案已覆蓋
        self.assertEqual((self.install_dir / "main.py").read_text(encoding="utf-8"), "print('v2')")
        self.assertTrue((self.install_dir / "new_feature.py").exists())

        # 驗證新版中刪除的舊檔案已被移除
        self.assertFalse((self.install_dir / "old_feature.py").exists())

        # 驗證 packs/ 與 deploy.env 全程未被改動
        self.assertTrue((packs_cat / "sprite.png").exists())
        self.assertEqual((packs_cat / "sprite.png").read_text(encoding="utf-8"), "cat_sprite_bytes")
        self.assertTrue((self.install_dir / "deploy.env").exists())
        self.assertEqual((self.install_dir / "deploy.env").read_text(encoding="utf-8"), "URL=https://pet.example.com")

        # 驗證備份目錄中保存有完整的舊檔案（含 old_feature.py）
        self.assertTrue(self.backup_dir.exists())
        self.assertTrue((self.backup_dir / "old_feature.py").exists())
        self.assertEqual((self.backup_dir / "main.py").read_text(encoding="utf-8"), "print('v1')")

        # 驗證 update_state.json 標記為 success
        st = load_update_state(self.state_dir)
        self.assertEqual(st.get("status"), "success")
        self.assertEqual(st.get("current_commit"), new_commit)

    def test_backup_failure_midway_leaves_old_backup_intact(self):
        # 既有舊備份存在（例如前一版的完整備份）
        self.backup_dir.mkdir(parents=True)
        (self.backup_dir / "main.py").write_text("print('v0_backup')", encoding="utf-8")
        (self.backup_dir / "backup_manifest.json").write_text(json.dumps({"files": ["main.py"]}), encoding="utf-8")

        # install_dir 目前有 v1 檔案
        (self.install_dir / "main.py").write_text("print('v1_current')", encoding="utf-8")
        (self.install_dir / "sub.py").write_text("print('sub')", encoding="utf-8")

        # 模擬在 create_backup 複製檔案途中發生例外（例如硬碟空間不足或寫入錯誤）
        orig_copy2 = shutil.copy2

        def failing_copy2(src, dst):
            if "sub.py" in str(src):
                raise OSError("Disk write error")
            return orig_copy2(src, dst)

        with mock.patch("shutil.copy2", side_effect=failing_copy2):
            with self.assertRaises(OSError):
                create_backup(self.install_dir, self.backup_dir)

        # 斷言：舊備份完全不受影響，依然是 v0 內容，且暫存備份已被清理
        self.assertTrue(self.backup_dir.exists())
        self.assertEqual((self.backup_dir / "main.py").read_text(encoding="utf-8"), "print('v0_backup')")
        self.assertFalse((self.backup_dir / "sub.py").exists())
        tmp_dirs = list(self.backup_dir.parent.glob(f"{self.backup_dir.name}.tmp_*"))
        self.assertEqual(tmp_dirs, [])

    def test_new_version_startup_failure_triggers_automatic_rollback(self):
        # 初始狀態：良好 v1
        (self.install_dir / "main.py").write_text("print('v1')", encoding="utf-8")
        (self.install_dir / "old_feature.py").write_text("def old(): pass", encoding="utf-8")
        packs_cat = self.install_dir / "packs" / "cat"
        packs_cat.mkdir(parents=True)
        (packs_cat / "sprite.png").write_text("cat_sprite_bytes", encoding="utf-8")
        (self.install_dir / "deploy.env").write_text("URL=https://pet.example.com", encoding="utf-8")
        write_manifest(self.install_dir, calculate_manifest(self.install_dir))

        bad_commit = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        tar_bytes = self._make_tarball({
            "main.py": "raise RuntimeError('broken v2')",
            "bad_feature.py": "def bad(): pass",
        })

        # 模擬新版本健康檢查失敗，但還原後舊版健康檢查通過
        health_calls = []
        def fake_health(conf, units=None, affected_units=None, timeout_sec=120, **kwargs):
            if not health_calls:
                health_calls.append("new_failed")
                return False, "新版本服務崩潰"
            health_calls.append("restored_ok")
            return True, "舊版本恢復正常"

        with mock.patch("tools.deploy.autoupdate.get_remote_commit", return_value=bad_commit), \
             mock.patch("tools.deploy.autoupdate.pre_switch_check", return_value=(True, None)), \
             mock.patch("tools.deploy.autoupdate.run_health_check", side_effect=fake_health), \
             mock.patch("tools.deploy.autoupdate.restart_units"), \
             mock.patch("urllib.request.urlopen") as mock_url:

            mock_url.return_value = io.BytesIO(tar_bytes)

            res = self.updater.run_cycle(force=True)
            self.assertEqual(res, 1)

        # 驗證自動回退：舊檔案完整還原，新版加入之檔案被刪除
        self.assertEqual((self.install_dir / "main.py").read_text(encoding="utf-8"), "print('v1')")
        self.assertTrue((self.install_dir / "old_feature.py").exists())
        self.assertFalse((self.install_dir / "bad_feature.py").exists())

        # packs/ 與 deploy.env 依然完好
        self.assertTrue((packs_cat / "sprite.png").exists())
        self.assertTrue((self.install_dir / "deploy.env").exists())

        # failed.json 記錄該 commit 失敗
        failed = load_failed_json(self.state_dir)
        self.assertTrue(any(f.get("commit") == bad_commit for f in failed))

        # 下一輪不再自動重試該失敗 commit
        with mock.patch("tools.deploy.autoupdate.get_remote_commit", return_value=bad_commit):
            res2 = self.updater.run_cycle(force=True)
            self.assertEqual(res2, 0)
            st_data = json.loads((self.state_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(st_data.get("last_check_result"), "commit_failed_previously")

    def test_interrupted_overwrite_detected_and_restored_by_boot_check(self):
        # 模擬覆蓋中途斷電：
        # 1. 備份目錄完整存在
        self.backup_dir.mkdir(parents=True)
        (self.backup_dir / "main.py").write_text("print('v1_intact')", encoding="utf-8")
        (self.backup_dir / "backup_manifest.json").write_text(json.dumps({"files": ["main.py"]}), encoding="utf-8")

        # 2. install_dir 處於毀損狀態（被半途覆蓋）
        (self.install_dir / "main.py").write_text("incomplete_half_written_bytes", encoding="utf-8")
        (self.install_dir / "deploy.env").write_text("URL=https://pet.example.com", encoding="utf-8")
        (self.install_dir / "packs" / "cat").mkdir(parents=True)
        (self.install_dir / "packs" / "cat" / "sprite.png").write_text("cat_sprite", encoding="utf-8")

        # 3. update_state.json 標記為 updating，並模擬中斷引發之自動 PAUSE 檔
        write_update_state(self.state_dir, {
            "status": "updating",
            "target_commit": "cccccccccccccccccccccccccccccccccccccccc",
            "previous_commit": "1111111111111111111111111111111111111111",
        })
        (self.state_dir / "PAUSE").write_text("本地檔案遭手動修改: 檔案遺失：main.py", encoding="utf-8")

        with mock.patch("tools.deploy.autoupdate.restart_units"):
            ok, msg = self.updater.boot_check()
            self.assertTrue(ok)
            self.assertEqual(msg, "power_cut_recovered")

        # 斷言檔案已自備份還原且自動 PAUSE 被清理
        self.assertFalse((self.state_dir / "PAUSE").exists())
        self.assertEqual((self.install_dir / "main.py").read_text(encoding="utf-8"), "print('v1_intact')")
        # 斷言 packs 與 deploy.env 未受影響
        self.assertTrue((self.install_dir / "deploy.env").exists())
        self.assertTrue((self.install_dir / "packs" / "cat" / "sprite.png").exists())

        # 斷言 failed.json 已記錄該 commit
        failed = load_failed_json(self.state_dir)
        self.assertTrue(any(f.get("commit") == "cccccccccccccccccccccccccccccccccccccccc" for f in failed))

    def test_first_boot_after_update_failure_triggers_rollback(self):
        # 模擬更新後第一次開機（狀態為 restarting）但驗證失敗
        self.backup_dir.mkdir(parents=True)
        (self.backup_dir / "main.py").write_text("print('v1_good')", encoding="utf-8")
        (self.backup_dir / "backup_manifest.json").write_text(json.dumps({"files": ["main.py"]}), encoding="utf-8")

        (self.install_dir / "main.py").write_text("print('v2_crashes')", encoding="utf-8")
        (self.install_dir / "new_bad.py").write_text("bad", encoding="utf-8")

        write_update_state(self.state_dir, {
            "status": "restarting",
            "target_commit": "dddddddddddddddddddddddddddddddddddddddd",
            "previous_commit": "1111111111111111111111111111111111111111",
        })

        with mock.patch("tools.deploy.autoupdate.run_health_check", return_value=(False, "啟動失敗")), \
             mock.patch("tools.deploy.autoupdate.restart_units"):
            ok, msg = self.updater.boot_check()
            self.assertFalse(ok)
            self.assertIn("first_boot_failed_rolled_back", msg)

        # 斷言已從備份還原
        self.assertEqual((self.install_dir / "main.py").read_text(encoding="utf-8"), "print('v1_good')")
        self.assertFalse((self.install_dir / "new_bad.py").exists())

    def test_normal_boot_check_only_logs_and_touches_no_files(self):
        # 正常運作開機：狀態為 success 或無狀態檔
        (self.install_dir / "main.py").write_text("print('v1')", encoding="utf-8")
        (self.install_dir / "packs").mkdir()
        (self.install_dir / "deploy.env").write_text("URL=ok", encoding="utf-8")

        write_update_state(self.state_dir, {"status": "success"})

        with mock.patch("tools.deploy.autoupdate.check_http_json", return_value={"fps": 20, "backend": {"online": True}}):
            ok, msg = self.updater.boot_check()
            self.assertTrue(ok)
            self.assertEqual(msg, "ok")

        # 若 /status 無回應，開機自檢回傳 False 但絕不動檔案
        with mock.patch("tools.deploy.autoupdate.check_http_json", return_value=None):
            ok, msg = self.updater.boot_check()
            self.assertFalse(ok)
            self.assertIn("/status 無回應", msg)
            self.assertEqual((self.install_dir / "main.py").read_text(encoding="utf-8"), "print('v1')")

        # 若舊版本檔案有缺失（例如缺少 main.py），僅記錄錯誤、不動任何檔案
        (self.install_dir / "main.py").unlink()
        with mock.patch("tools.deploy.autoupdate.check_http_json", return_value={"fps": 20, "backend": {"online": True}}):
            ok, msg = self.updater.boot_check()
            self.assertFalse(ok)
            self.assertIn("main.py 不存在", msg)
            # 確保不會去刪除或建立備份
            self.assertFalse(self.backup_dir.exists())

    def test_repo_external_files_not_deleted(self):
        # 模擬安裝目錄中存在 repo 外部檔案（例如使用者本機筆記、target 編譯產物）
        (self.install_dir / "main.py").write_text("print('v1')", encoding="utf-8")
        (self.install_dir / "local_notes.txt").write_text("my local notes", encoding="utf-8")
        wifi_bin = self.install_dir / "services" / "piwifi" / "target" / "release" / "piwifi"
        wifi_bin.parent.mkdir(parents=True)
        wifi_bin.write_text("binary_stub", encoding="utf-8")

        # 情況 A：首次更新（無上一版 MANIFEST.sha256）
        unpacked_dir = self.tmp_dir / "unpacked_v2"
        unpacked_dir.mkdir(parents=True)
        (unpacked_dir / "main.py").write_text("print('v2')", encoding="utf-8")
        (unpacked_dir / "other.py").write_text("print('other')", encoding="utf-8")

        overwritten, deleted = overwrite_release(self.install_dir, unpacked_dir, previous_manifest_files=None)
        # 首次更新不得刪除任何檔案
        self.assertEqual(deleted, [])
        self.assertTrue((self.install_dir / "local_notes.txt").exists())
        self.assertEqual((self.install_dir / "local_notes.txt").read_text(encoding="utf-8"), "my local notes")
        self.assertTrue(wifi_bin.exists())

        # 情況 B：非首次更新，前一版清單只包含 main.py 與 removed.py
        (self.install_dir / "removed.py").write_text("removed", encoding="utf-8")
        prev_manifest = {"main.py", "removed.py"}
        overwritten2, deleted2 = overwrite_release(self.install_dir, unpacked_dir, previous_manifest_files=prev_manifest)
        self.assertIn("removed.py", deleted2)
        self.assertFalse((self.install_dir / "removed.py").exists())
        # repo 外部檔案依然不受影響
        self.assertTrue((self.install_dir / "local_notes.txt").exists())
        self.assertTrue(wifi_bin.exists())

    def test_consecutive_updates_preserve_external_files(self):
        # 模擬安裝目錄中存在非 repo 外部檔案（例如使用者本機筆記 my_local_notes.txt）
        (self.install_dir / "main.py").write_text("print('v0')", encoding="utf-8")
        (self.install_dir / "my_local_notes.txt").write_text("my notes", encoding="utf-8")
        (self.install_dir / "deploy.env").write_text("URL=https://pet.example.com", encoding="utf-8")

        # 第一次更新：更新至 v1（包含 main.py 與 feat1.py）
        tar_v1 = self._make_tarball({"main.py": "print('v1')", "feat1.py": "def f1(): pass"})
        with mock.patch("tools.deploy.autoupdate.get_remote_commit", return_value="1111111111111111111111111111111111111111"), \
             mock.patch("tools.deploy.autoupdate.pre_switch_check", return_value=(True, None)), \
             mock.patch("tools.deploy.autoupdate.run_health_check", return_value=(True, "all ok")), \
             mock.patch("tools.deploy.autoupdate.restart_units"), \
             mock.patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = io.BytesIO(tar_v1)
            res1 = self.updater.run_cycle(force=True)
            self.assertEqual(res1, 0)

        # 斷言第一次更新後，my_local_notes.txt 仍在，且 MANIFEST.sha256 不包含 my_local_notes.txt
        self.assertTrue((self.install_dir / "my_local_notes.txt").exists())
        self.assertEqual((self.install_dir / "my_local_notes.txt").read_text(encoding="utf-8"), "my notes")
        manifest_text = (self.install_dir / "MANIFEST.sha256").read_text(encoding="utf-8")
        self.assertNotIn("my_local_notes.txt", manifest_text)
        self.assertIn("feat1.py", manifest_text)

        # 第二次更新：更新至 v2（移除 feat1.py，新增 feat2.py）
        tar_v2 = self._make_tarball({"main.py": "print('v2')", "feat2.py": "def f2(): pass"})
        with mock.patch("tools.deploy.autoupdate.get_remote_commit", return_value="2222222222222222222222222222222222222222"), \
             mock.patch("tools.deploy.autoupdate.pre_switch_check", return_value=(True, None)), \
             mock.patch("tools.deploy.autoupdate.run_health_check", return_value=(True, "all ok")), \
             mock.patch("tools.deploy.autoupdate.restart_units"), \
             mock.patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = io.BytesIO(tar_v2)
            res2 = self.updater.run_cycle(force=True)
            self.assertEqual(res2, 0)

        # 斷言第二次更新後：
        # 1. feat1.py 已被正確刪除
        self.assertFalse((self.install_dir / "feat1.py").exists())
        # 2. feat2.py 已被建立
        self.assertTrue((self.install_dir / "feat2.py").exists())
        # 3. 非 repo 本機檔案 my_local_notes.txt 經歷兩次更新依然完好存在！
        self.assertTrue((self.install_dir / "my_local_notes.txt").exists())
        self.assertEqual((self.install_dir / "my_local_notes.txt").read_text(encoding="utf-8"), "my notes")

    def test_pause_and_dry_run_with_unfinished_state_touches_no_files(self):
        (self.install_dir / "main.py").write_text("print('unrestored_v1')", encoding="utf-8")
        self.backup_dir.mkdir(parents=True)
        (self.backup_dir / "main.py").write_text("print('backup_v0')", encoding="utf-8")
        (self.backup_dir / "backup_manifest.json").write_text(json.dumps({"files": ["main.py"]}), encoding="utf-8")

        # 設為 updating 狀態
        write_update_state(self.state_dir, {"status": "updating", "target_commit": "aaaa"})

        # A. PAUSE 檔存在：run_cycle 絕不動檔案，也不呼叫 boot_check 還原
        (self.state_dir / "PAUSE").touch()
        with mock.patch("tools.deploy.autoupdate.restart_units") as mock_restart:
            res = self.updater.run_cycle(force=True)
            self.assertEqual(res, 0)
            mock_restart.assert_not_called()
        self.assertEqual((self.install_dir / "main.py").read_text(encoding="utf-8"), "print('unrestored_v1')")
        self.assertEqual(load_update_state(self.state_dir).get("status"), "updating")

        # 移除 PAUSE 檔
        (self.state_dir / "PAUSE").unlink()

        # B. dry_run=True：即使有未完成狀態，也絕不動檔案與服務
        dry_updater = AutoUpdater(env_path=self.env_file, dry_run=True)
        with mock.patch("tools.deploy.autoupdate.restart_units") as mock_restart:
            res = dry_updater.run_cycle(force=True)
            self.assertEqual(res, 0)
            mock_restart.assert_not_called()
        self.assertEqual((self.install_dir / "main.py").read_text(encoding="utf-8"), "print('unrestored_v1')")
        self.assertEqual(load_update_state(self.state_dir).get("status"), "updating")

    def test_backend_offline_warns_but_passes_health_check(self):
        # 1. 測試 run_health_check：後端 offline 時僅發出警告，本機桌寵達標即通過
        conf = {
            "PET_BACKEND_URL": "http://127.0.0.1:8090",
            "PET_AUTOUPDATE_HEALTH_MIN_FPS": "18",
        }
        fake_status = {
            "ok": True,
            "fps": 20.0,
            "backend": {"online": False},
        }
        with mock.patch("tools.deploy.autoupdate.check_http_json", return_value=fake_status), \
             mock.patch("subprocess.run") as mock_sub:
            mock_sub.return_value.stdout = "active\n"
            mock_sub.return_value.returncode = 0
            ok, msg = run_health_check(conf, ["desktop-pet.service"], timeout_sec=0.1, poll_interval=0.02)
            self.assertTrue(ok)
            self.assertEqual(msg, "all ok")

        # 2. 測試 run_cycle 在後端不通時觸發回退
        (self.install_dir / "main.py").write_text("print('v1_orig')", encoding="utf-8")
        write_manifest(self.install_dir, calculate_manifest(self.install_dir))
        new_commit = "cccccccccccccccccccccccccccccccccccccccc"
        tar_bytes = self._make_tarball({"main.py": "print('v2_bad_backend')"})

        with mock.patch("tools.deploy.autoupdate.get_remote_commit", return_value=new_commit), \
             mock.patch("tools.deploy.autoupdate.pre_switch_check", return_value=(True, None)), \
             mock.patch("tools.deploy.autoupdate.run_health_check", return_value=(False, "桌寵尚未連上後端")), \
             mock.patch("tools.deploy.autoupdate.restart_units"), \
             mock.patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = io.BytesIO(tar_bytes)
            res = self.updater.run_cycle(force=True)
            self.assertEqual(res, 1)

        # 驗證回退至 v1
        self.assertEqual((self.install_dir / "main.py").read_text(encoding="utf-8"), "print('v1_orig')")
        failed = load_failed_json(self.state_dir)
        self.assertTrue(any(f.get("commit") == new_commit for f in failed))

    def test_first_update_failure_without_initial_manifest_cleans_manifest_and_allows_next_cycle(self):
        # 1. 模擬線上 Pi 初次更新環境：
        #    有 main.py、notes.txt、deploy.env，但完全沒有 MANIFEST.sha256 與 RELEASE.json
        (self.install_dir / "main.py").write_text("print('v1_old')", encoding="utf-8")
        (self.install_dir / "notes.txt").write_text("my notes", encoding="utf-8")
        (self.install_dir / "deploy.env").write_text("URL=https://pet.example.com", encoding="utf-8")
        self.assertFalse((self.install_dir / "MANIFEST.sha256").exists())

        # 2. 模擬來自 GitHub 的真實 repo 結構 tarball
        #    包含 frontend/main.py, frontend/new_feature.py, shared/shared/autoupdate.py
        new_commit = "9999999999999999999999999999999999999999"
        tar_bytes = self._make_tarball({
            "frontend/main.py": "print('v2_crashes')",
            "frontend/new_feature.py": "def new(): pass",
            "shared/shared/autoupdate.py": "# shared v2",
            "README.md": "# pet",
        })

        # 3. 第一次更新：切換後健康檢查失敗觸發回退
        with mock.patch("tools.deploy.autoupdate.get_remote_commit", return_value=new_commit), \
             mock.patch("tools.deploy.autoupdate.pre_switch_check", return_value=(True, None)), \
             mock.patch("tools.deploy.autoupdate.run_health_check", return_value=(False, "啟動異常")), \
             mock.patch("tools.deploy.autoupdate.restart_units"), \
             mock.patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = io.BytesIO(tar_bytes)
            res = self.updater.run_cycle(force=True)
            self.assertEqual(res, 1)

        # 驗證回退保護：
        # a. 舊檔案還原且本機資料 notes.txt 完好
        self.assertEqual((self.install_dir / "main.py").read_text(encoding="utf-8"), "print('v1_old')")
        self.assertTrue((self.install_dir / "notes.txt").exists())
        self.assertTrue((self.install_dir / "deploy.env").exists())
        self.assertFalse((self.install_dir / "frontend" / "new_feature.py").exists())
        # b. 關鍵 Must-fix 1：回退後 MANIFEST.sha256 必須被徹底刪除，不能遺留新版雜湊！
        self.assertFalse((self.install_dir / "MANIFEST.sha256").exists())

        # 4. 下一輪更新驗證：
        #    確認下一輪不會因殘留 MANIFEST 而將缺失的 new_feature.py 誤判為「本地檔案遭手動修改」寫入 PAUSE！
        good_commit = "8888888888888888888888888888888888888888"
        good_tar = self._make_tarball({
            "frontend/main.py": "print('v2_good')",
        })
        with mock.patch("tools.deploy.autoupdate.get_remote_commit", return_value=good_commit), \
             mock.patch("tools.deploy.autoupdate.pre_switch_check", return_value=(True, None)), \
             mock.patch("tools.deploy.autoupdate.run_health_check", return_value=(True, None)), \
             mock.patch("tools.deploy.autoupdate.restart_units"), \
             mock.patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = io.BytesIO(good_tar)
            res2 = self.updater.run_cycle(force=True)
            self.assertEqual(res2, 0)

        # 驗證未被 PAUSE 阻擋且更新成功
        self.assertFalse((self.state_dir / "PAUSE").exists())
        self.assertEqual((self.install_dir / "frontend" / "main.py").read_text(encoding="utf-8"), "print('v2_good')")

    def test_interrupted_in_progress_auto_recovers_and_cleans_automatic_pause(self):
        # 模擬更新程序被 kill 或斷電：
        # 1. 備份目錄完整存在
        self.backup_dir.mkdir(parents=True)
        (self.backup_dir / "main.py").write_text("print('v1_intact')", encoding="utf-8")
        (self.backup_dir / "backup_manifest.json").write_text(json.dumps({"files": ["main.py"]}), encoding="utf-8")

        # 2. install_dir 處於毀損狀態（被半途覆蓋）
        (self.install_dir / "main.py").write_text("half_overwritten_garbage", encoding="utf-8")

        # 3. 狀態為 in_progress
        bad_commit = "7777777777777777777777777777777777777777"
        write_update_state(self.state_dir, {
            "status": "in_progress",
            "target_commit": bad_commit,
        })

        # 4. 模擬先前曾因為檔案不完整被自動寫入 PAUSE（含「本地檔案遭手動修改」字樣）
        (self.state_dir / "PAUSE").write_text("本地檔案遭手動修改: 檔案遺失：main.py", encoding="utf-8")

        # 執行 run_cycle：自動偵測中斷並回退、清理自動 PAUSE
        with mock.patch("tools.deploy.autoupdate.restart_units"):
            res = self.updater.run_cycle(force=True)
            self.assertEqual(res, 0)

        # 驗證檔案已自備份復原
        self.assertEqual((self.install_dir / "main.py").read_text(encoding="utf-8"), "print('v1_intact')")
        # 驗證自動產生的 PAUSE 檔已被清除
        self.assertFalse((self.state_dir / "PAUSE").exists())
        # 驗證 failed.json 記錄了 interrupted
        failed = load_failed_json(self.state_dir)
        self.assertTrue(any(f.get("commit") == bad_commit and f.get("stage") == "interrupted" for f in failed))

    def test_restart_units_failure_aborts_update_and_triggers_rollback(self):
        (self.install_dir / "main.py").write_text("print('v1_orig')", encoding="utf-8")
        write_manifest(self.install_dir, calculate_manifest(self.install_dir))
        new_commit = "6666666666666666666666666666666666666666"
        tar_bytes = self._make_tarball({"main.py": "print('v2_fail_restart')"})

        with mock.patch("tools.deploy.autoupdate.get_remote_commit", return_value=new_commit), \
             mock.patch("tools.deploy.autoupdate.pre_switch_check", return_value=(True, None)), \
             mock.patch("tools.deploy.autoupdate.restart_units", return_value=False), \
             mock.patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = io.BytesIO(tar_bytes)
            res = self.updater.run_cycle(force=True)
            self.assertEqual(res, 1)

        # 驗證重啟失敗中止切換並回退至 v1
        self.assertEqual((self.install_dir / "main.py").read_text(encoding="utf-8"), "print('v1_orig')")
        failed = load_failed_json(self.state_dir)
        self.assertTrue(any(f.get("commit") == new_commit for f in failed))


class TestPolkitRuleAndSystemdTemplates(unittest.TestCase):
    def test_polkit_rule_content(self):
        rule_file = Path(__file__).resolve().parents[1] / "services" / "desktop-pet-update" / "51-desktop-pet-update.rules"
        self.assertTrue(rule_file.exists(), f"找不到 {rule_file}")
        content = rule_file.read_text(encoding="utf-8")
        code_lines = [l for l in content.splitlines() if not l.strip().startswith("//")]
        code = "\n".join(code_lines)

        # 必須包含 allowedUnits
        self.assertIn("desktop-pet.service", code)
        self.assertIn("desktop-pet-admin.service", code)
        self.assertIn("picam.service", code)
        self.assertIn("piwifi.service", code)

        # 程式碼邏輯中絕對不可包含 piscreen.service 或 reboot 或 reload-daemon
        self.assertNotIn("piscreen.service", code)
        self.assertNotIn("reboot", code)
        self.assertNotIn("reload-daemon", code)

    def test_systemd_service_and_timer_templates(self):
        base = Path(__file__).resolve().parents[1] / "services" / "desktop-pet-update"
        svc = (base / "desktop-pet-update.service").read_text(encoding="utf-8")
        tmr = (base / "desktop-pet-update.timer").read_text(encoding="utf-8")

        self.assertIn("Type=oneshot", svc)
        self.assertIn("User=@USER@", svc)
        self.assertIn("EnvironmentFile=-/etc/desktop-pet/env", svc)
        self.assertIn("Nice=10", svc)
        self.assertIn("IOSchedulingClass=idle", svc)

        self.assertIn("OnBootSec=5min", tmr)
        self.assertIn("OnUnitActiveSec=5min", tmr)
        self.assertIn("RandomizedDelaySec=60", tmr)

        boot_svc = (base / "desktop-pet-boot-check.service").read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", boot_svc)
        self.assertIn("User=@USER@", boot_svc)
        self.assertIn("--boot-check", boot_svc)


class TestStatusAndUpdatesJson(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_failed_json_record_and_clear(self):
        record_failed_commit(self.tmp_dir, "c004feb12345", "ci_check", "test failure")
        records = load_failed_json(self.tmp_dir)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["commit"], "c004feb12345")
        self.assertEqual(records[0]["step"], "ci_check")

    def test_status_json_atomic_write(self):
        st = {"commit": "1234567890ab", "status": "on", "paused": False}
        write_status_json(self.tmp_dir, st)
        loaded = json.loads((self.tmp_dir / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(loaded["commit"], "1234567890ab")

    def test_updates_jsonl_append_and_rotation(self):
        for i in range(10):
            append_updates_jsonl(self.tmp_dir, {"index": i, "result": "ok"})
        lines = (self.tmp_dir / "updates.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 10)


class TestPetVersionModule(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_get_release_info_with_release_json(self):
        rel_data = {
            "commit": "abcdef1234567890abcdef1234567890abcdef12",
            "short": "abcdef123456",
            "describe": "v2.8.0+5",
            "deployed_at": "2026-10-30T00:00:00Z",
            "source": "auto",
        }
        (self.tmp_dir / "RELEASE.json").write_text(json.dumps(rel_data), encoding="utf-8")
        info = get_release_info(root_dir=self.tmp_dir)
        self.assertEqual(info["commit"], rel_data["commit"])
        self.assertEqual(info["describe"], "v2.8.0+5")

    def test_get_autoupdate_status_defaults_and_pause(self):
        st = get_autoupdate_status(state_dir=self.tmp_dir)
        self.assertEqual(st["status"], "on")
        self.assertFalse(st["paused"])

        # PAUSE 檔
        (self.tmp_dir / "PAUSE").touch()
        st_paused = get_autoupdate_status(state_dir=self.tmp_dir)
        self.assertEqual(st_paused["status"], "paused")
        self.assertTrue(st_paused["paused"])


if __name__ == "__main__":
    unittest.main()
