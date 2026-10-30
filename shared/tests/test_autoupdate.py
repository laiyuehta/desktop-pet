"""共用自動更新核心單元測試（PROPOSAL.md 16. 自動部署、Issue #13、Issue #14）。"""
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

# 加入 shared 模組搜尋路徑
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.autoupdate import (
    AutoUpdaterCore,
    ProcessLock,
    append_updates_jsonl,
    calculate_manifest,
    check_ci_runs,
    clear_failed_records,
    create_backup,
    get_remote_commit,
    is_protected_path,
    load_failed_json,
    load_update_state,
    overwrite_release,
    parse_pkt_lines,
    record_failed_commit,
    restore_from_backup,
    safe_extract_tarball,
    verify_manifest,
    write_manifest,
    write_status_json,
    write_update_state,
)


class TestGitSmartHttp(unittest.TestCase):
    def test_parse_pkt_lines(self):
        def make_pkt(payload: str) -> bytes:
            b = payload.encode("utf-8")
            return f"{len(b) + 4:04x}".encode("ascii") + b

        # 模擬 GitHub 真實格式：001e# service=git-upload-pack\n0000 + 包含 \0capabilities
        raw = (
            make_pkt("# service=git-upload-pack\n")
            + b"0000"
            + make_pkt("870020a123456789abcdef0123456789abcdef01 HEAD\x00multi_ack thin-pack side-band\n")
            + make_pkt("870020a123456789abcdef0123456789abcdef01 refs/heads/main\n")
            + b"0000"
        )
        lines = parse_pkt_lines(raw)
        self.assertTrue(any("refs/heads/main" in l for l in lines))

    @mock.patch("urllib.request.urlopen")
    def test_get_remote_commit_success(self, mock_urlopen):
        commit = "870020a123456789abcdef0123456789abcdef01"
        payload = f"{commit} refs/heads/main\n".encode("utf-8")
        pkt = f"{len(payload) + 4:04x}".encode("ascii") + payload
        data = b"001e# service=git-upload-pack\n0000" + pkt + b"0000"

        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = data
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        result = get_remote_commit("https://github.com/laiyuehta/desktop-pet", "main")
        self.assertEqual(result, commit)


class TestCiRuns(unittest.TestCase):
    @mock.patch("urllib.request.urlopen")
    def test_check_ci_runs_all_success(self, mock_urlopen):
        data = {
            "total_count": 2,
            "check_runs": [
                {"name": "tests (Python 3.13)", "status": "completed", "conclusion": "success"},
                {"name": "secrets & personal-info scan", "status": "completed", "conclusion": "success"},
            ],
        }
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps(data).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        status, reason = check_ci_runs("https://github.com/laiyuehta/desktop-pet", "c" * 40)
        self.assertEqual(status, "success")
        self.assertIsNone(reason)

    @mock.patch("urllib.request.urlopen")
    def test_check_ci_runs_failure(self, mock_urlopen):
        data = {
            "total_count": 2,
            "check_runs": [
                {"name": "tests (Python 3.13)", "status": "completed", "conclusion": "failure"},
                {"name": "secrets & personal-info scan", "status": "completed", "conclusion": "success"},
            ],
        }
        mock_resp = mock.MagicMock()
        mock_resp.read.return_value = json.dumps(data).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        status, reason = check_ci_runs("https://github.com/laiyuehta/desktop-pet", "c" * 40)
        self.assertEqual(status, "failed")
        self.assertIn("未通過", reason)


class TestTarballAndSecurity(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.base = Path(self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_safe_extract_prevents_path_traversal(self):
        tar_path = self.base / "evil.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            ti = tarfile.TarInfo(name="../escaped.txt")
            data = b"malicious content"
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))

        dest = self.base / "extract"
        with self.assertRaises(ValueError):
            safe_extract_tarball(tar_path, dest)

    def test_protected_paths(self):
        self.assertTrue(is_protected_path(Path(".venv/bin/python")))
        self.assertTrue(is_protected_path(Path("frontend/.venv/pyvenv.cfg")))
        self.assertTrue(is_protected_path(Path("backend/.venv/Scripts/python.exe")))
        self.assertTrue(is_protected_path(Path("packs/seed/idle.png")))
        self.assertTrue(is_protected_path(Path("deploy.env")))
        self.assertTrue(is_protected_path(Path("config.json")))
        self.assertTrue(is_protected_path(Path("data/pet.json")))
        self.assertTrue(is_protected_path(Path(".lock")))
        self.assertFalse(is_protected_path(Path("frontend/main.py")))
        self.assertFalse(is_protected_path(Path("backend/server.py")))


class TestManifestAndPreservation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.base = Path(self.temp_dir)
        self.inst = self.base / "install"
        self.inst.mkdir()
        self.backup = self.base / "backup"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_two_consecutive_updates_never_delete_non_repo_files(self):
        """Must-fix 驗證：連續兩次更新，非 repo 的本機檔案（設定、筆記等）絕不會被誤刪。"""
        # 初始狀態：安裝目錄內有 main.py 以及本機筆記 my_local_notes.txt
        (self.inst / "main.py").write_text("print('v0')", encoding="utf-8")
        (self.inst / "my_local_notes.txt").write_text("user notes", encoding="utf-8")

        # 模擬第 1 版更新（v1）：包含 main.py 與 helper.py
        v1_dir = self.base / "v1"
        v1_dir.mkdir()
        (v1_dir / "main.py").write_text("print('v1')", encoding="utf-8")
        (v1_dir / "helper.py").write_text("helper = 1", encoding="utf-8")

        # 執行第 1 次更新
        create_backup(self.inst, self.backup)
        overwritten1, deleted1 = overwrite_release(self.inst, v1_dir, self.backup, previous_manifest_files=set())
        # 僅為新版覆蓋之檔案計算 manifest
        manifest1 = calculate_manifest(self.inst, files=overwritten1)
        write_manifest(self.inst, manifest1)

        # 驗證第 1 次更新結果
        self.assertTrue((self.inst / "my_local_notes.txt").is_file(), "第 1 次更新後本機檔案必須存在")
        manifest_text1 = (self.inst / "MANIFEST.sha256").read_text(encoding="utf-8")
        self.assertNotIn("my_local_notes.txt", manifest_text1, "MANIFEST 絕不應包含非 repo 本機檔案")

        # 模擬第 2 版更新（v2）：移除了 helper.py，新增 extra.py
        v2_dir = self.base / "v2"
        v2_dir.mkdir()
        (v2_dir / "main.py").write_text("print('v2')", encoding="utf-8")
        (v2_dir / "extra.py").write_text("extra = 2", encoding="utf-8")

        # 執行第 2 次更新（讀取第 1 次產生的 MANIFEST 作為 previous_manifest_files）
        prev_manifest = set(manifest1.keys())
        create_backup(self.inst, self.backup)
        overwritten2, deleted2 = overwrite_release(self.inst, v2_dir, self.backup, previous_manifest_files=prev_manifest)
        manifest2 = calculate_manifest(self.inst, files=overwritten2)
        write_manifest(self.inst, manifest2)

        # 驗證第 2 次更新結果
        self.assertTrue((self.inst / "my_local_notes.txt").is_file(), "第 2 次更新後本機檔案絕不可被刪除！")
        self.assertIn("helper.py", deleted2, "新版已移除之舊發布檔 helper.py 應被刪除")
        self.assertFalse((self.inst / "helper.py").exists(), "helper.py 應已刪除")
        self.assertTrue((self.inst / "extra.py").is_file(), "新檔案 extra.py 應存在")
        self.assertEqual((self.inst / "my_local_notes.txt").read_text(encoding="utf-8"), "user notes")

    def test_backup_and_restore(self):
        (self.inst / "app.py").write_text("old", encoding="utf-8")
        create_backup(self.inst, self.backup, incoming_files=["app.py", "bad.py"])
        self.assertTrue((self.backup / "app.py").is_file())
        self.assertTrue((self.backup / "backup_manifest.json").is_file())

        # 模擬新版覆蓋並新增 bad.py
        (self.inst / "app.py").write_text("new_broken", encoding="utf-8")
        (self.inst / "bad.py").write_text("bad", encoding="utf-8")

        # 還原
        ok = restore_from_backup(self.inst, self.backup)
        self.assertTrue(ok)
        self.assertEqual((self.inst / "app.py").read_text(encoding="utf-8"), "old")
        self.assertFalse((self.inst / "bad.py").exists(), "還原時應清除新版帶入的多餘檔案")


class TestProcessLock(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.lock_file = Path(self.temp_dir) / "test.lock"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_acquire_and_release(self):
        lock = ProcessLock(self.lock_file)
        self.assertTrue(lock.acquire())
        self.assertTrue(self.lock_file.exists())
        self.assertEqual(self.lock_file.read_text(encoding="utf-8").strip(), str(os.getpid()))

        # 同一個 lock 物件重複 acquire 回傳 True
        self.assertTrue(lock.acquire())
        lock.release()
        # 釋放後檔案保留避免 unlink 競態，但後續 ProcessLock 可再次順利獲取
        lock2 = ProcessLock(self.lock_file)
        self.assertTrue(lock2.acquire())
        lock2.release()


if __name__ == "__main__":
    unittest.main()
