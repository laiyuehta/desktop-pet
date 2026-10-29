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

import pytest

from tools.deploy.autoupdate import (
    AutoUpdater,
    ProcessLock,
    append_updates_jsonl,
    calculate_manifest,
    check_ci_runs,
    check_held_criteria,
    check_manual_steps,
    detect_affected_units,
    get_remote_commit,
    load_env,
    load_failed_json,
    parse_pkt_lines,
    pre_switch_check,
    prune_old_releases,
    record_failed_commit,
    run_health_check,
    safe_extract_tarball,
    switch_symlink,
    verify_manifest,
    write_manifest,
    write_status_json,
)
from pet.version import get_autoupdate_status, get_release_info


class TestGitSmartHttp(unittest.TestCase):
    def test_parse_pkt_lines(self):
        # 建立模擬 pkt-line 資料
        # 000a = 10 bytes: "000a" (4) + "test\n" (5) + 1 byte
        raw = b"000a# test\n0000003b870020a123456789abcdef0123456789abcdef01 refs/heads/main\n"
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


class TestSymlinkSwitchAndPruning(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(tempfile.mkdtemp())
        self.releases_dir = self.tmp_dir / "releases"
        self.releases_dir.mkdir()
        self.link = self.tmp_dir / "current"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_switch_symlink(self):
        rel_a = self.releases_dir / "20261029T000000Z-aaaa"
        rel_b = self.releases_dir / "20261029T010000Z-bbbb"
        rel_a.mkdir()
        rel_b.mkdir()
        (rel_a / "ver.txt").write_text("A", encoding="utf-8")
        (rel_b / "ver.txt").write_text("B", encoding="utf-8")

        switch_symlink(rel_a, self.link)
        self.assertEqual((self.link / "ver.txt").read_text(encoding="utf-8"), "A")

        switch_symlink(rel_b, self.link)
        self.assertEqual((self.link / "ver.txt").read_text(encoding="utf-8"), "B")

    def test_prune_old_releases(self):
        dirs = []
        for i in range(6):
            d = self.releases_dir / f"20261029T0{i}0000Z-commit{i}"
            d.mkdir()
            dirs.append(d)

        current = dirs[-1]
        prev = dirs[-2]

        prune_old_releases(self.releases_dir, current_release=current, previous_release=prev, keep_count=2)
        remaining = [d.name for d in self.releases_dir.iterdir() if d.is_dir()]

        # current 與 prev 必留
        self.assertIn(current.name, remaining)
        self.assertIn(prev.name, remaining)
        # 加上 2 個舊版本，總共應為 4 個
        self.assertEqual(len(remaining), 4)


class TestPolkitRuleAndSystemdTemplates(unittest.TestCase):
    def test_polkit_rule_content(self):
        rule_file = Path(__file__).resolve().parents[1] / "services" / "desktop-pet-update" / "51-desktop-pet-update.rules"
        self.assertTrue(rule_file.exists(), f"找不到 {rule_file}")
        content = rule_file.read_text(encoding="utf-8")

        # 必須包含 allowedUnits
        self.assertIn("desktop-pet.service", content)
        self.assertIn("desktop-pet-admin.service", content)
        self.assertIn("picam.service", content)
        self.assertIn("piwifi.service", content)

        # 絕對不可包含 piscreen.service 或 reboot
        self.assertNotIn("piscreen.service", content)
        self.assertNotIn("reboot", content)
        self.assertNotIn("reload-daemon", content)

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
