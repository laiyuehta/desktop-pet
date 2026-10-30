"""R7（Windows 後端）自動更新器單元測試（PROPOSAL.md 16. 自動部署、Issue #14）。"""
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

# 加入搜尋路徑
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))
if str(ROOT / "shared") not in sys.path:
    sys.path.insert(0, str(ROOT / "shared"))

from backend.autoupdate import (
    AutoUpdaterR7,
    check_held_criteria_r7,
    detect_affected_r7,
    pre_switch_check_r7,
    run_health_check_r7,
)
from shared.autoupdate import (
    calculate_manifest,
    create_backup,
    load_failed_json,
    load_update_state,
    overwrite_release,
    write_manifest,
    write_update_state,
)


class TestR7AutoupdateBasics(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.base = Path(self.temp_dir)
        self.old_dir = self.base / "old"
        self.new_dir = self.base / "new"
        self.old_dir.mkdir()
        self.new_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_detect_affected_backend_changes(self):
        # 1. backend 程式碼變更需重啟後端
        (self.old_dir / "backend").mkdir()
        (self.new_dir / "backend").mkdir()
        (self.old_dir / "backend" / "server.py").write_text("v1", encoding="utf-8")
        (self.new_dir / "backend" / "server.py").write_text("v2", encoding="utf-8")
        affected = detect_affected_r7(self.old_dir, self.new_dir)
        self.assertEqual(affected, ["desktop-pet-backend"])

        # 2. 純文件變更不重啟
        (self.old_dir / "backend" / "server.py").write_text("v2", encoding="utf-8")
        (self.old_dir / "README.md").write_text("doc1", encoding="utf-8")
        (self.new_dir / "README.md").write_text("doc2", encoding="utf-8")
        affected2 = detect_affected_r7(self.old_dir, self.new_dir)
        self.assertEqual(affected2, [])

    @mock.patch("backend.autoupdate.check_http_json")
    def test_run_health_check_r7_success(self, mock_check_http):
        mock_check_http.return_value = {"ok": True}
        ok, reason = run_health_check_r7(host="127.0.0.1", port=8090, timeout=1.0)
        self.assertTrue(ok)
        self.assertIsNone(reason)

    @mock.patch("backend.autoupdate.check_http_json")
    def test_run_health_check_r7_commit_mismatch(self, mock_check_http):
        mock_check_http.return_value = {"ok": True}
        inst_dir = self.base / "inst"
        inst_dir.mkdir()
        (inst_dir / "RELEASE.json").write_text(json.dumps({"commit": "old_commit"}), encoding="utf-8")

        ok, reason = run_health_check_r7(
            host="127.0.0.1",
            port=8090,
            target_commit="target_new_commit",
            timeout=1.0,
            install_dir=inst_dir,
        )
        self.assertFalse(ok)
        self.assertIn("不符", reason)

    @mock.patch("backend.autoupdate.check_http_json")
    def test_run_health_check_r7_offline(self, mock_check_http):
        mock_check_http.return_value = None
        ok, reason = run_health_check_r7(host="127.0.0.1", port=8090, timeout=0.1)
        self.assertFalse(ok)
        self.assertIn("超時", reason)


class TestR7AutoUpdaterCycle(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.base = Path(self.temp_dir)
        self.inst = self.base / "repo"
        self.backup = self.base / "backup"
        self.state = self.base / "state"
        self.inst.mkdir()
        self.state.mkdir()

        self.updater = AutoUpdaterR7(
            install_dir=self.inst,
            backup_dir=self.backup,
            state_dir=self.state,
            require_ci=False,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_tarball(self, files: Dict[str, str]) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for rel_path, content in files.items():
                data = content.encode("utf-8")
                ti = tarfile.TarInfo(name=f"repo/{rel_path}")
                ti.size = len(data)
                tar.addfile(ti, io.BytesIO(data))
        return buf.getvalue()

    def test_successful_r7_update_and_preserves_data(self):
        # 初始檔案與受保護本機資料
        (self.inst / "backend").mkdir()
        (self.inst / "backend" / "server.py").write_text("server_v1", encoding="utf-8")
        data_dir = self.inst / "data"
        data_dir.mkdir()
        (data_dir / "pet_seed.json").write_text('{"mood": "happy"}', encoding="utf-8")
        (self.inst / "local_notes.txt").write_text("local host notes", encoding="utf-8")

        new_commit = "1111111111111111111111111111111111111111"
        tar_bytes = self._make_tarball({
            "backend/server.py": "server_v2",
            "backend/new_mod.py": "new_mod",
        })

        with mock.patch("shared.autoupdate.get_remote_commit", return_value=new_commit), \
             mock.patch.object(self.updater, "pre_switch_check", return_value=(True, None)), \
             mock.patch.object(self.updater, "run_health_check", return_value=(True, None)), \
             mock.patch.object(self.updater, "restart_affected", return_value=True), \
             mock.patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = io.BytesIO(tar_bytes)

            res = self.updater.run_cycle(force=True)
            self.assertEqual(res, 0)

        # 驗證新版本覆蓋
        self.assertEqual((self.inst / "backend" / "server.py").read_text(encoding="utf-8"), "server_v2")
        self.assertTrue((self.inst / "backend" / "new_mod.py").is_file())

        # 驗證本機資料與非 repo 檔案完好如初
        self.assertTrue((data_dir / "pet_seed.json").is_file())
        self.assertEqual((data_dir / "pet_seed.json").read_text(encoding="utf-8"), '{"mood": "happy"}')
        self.assertTrue((self.inst / "local_notes.txt").is_file())
        self.assertEqual((self.inst / "local_notes.txt").read_text(encoding="utf-8"), "local host notes")

        # 驗證 RELEASE.json
        rel_info = json.loads((self.inst / "RELEASE.json").read_text(encoding="utf-8"))
        self.assertEqual(rel_info.get("commit"), new_commit)

    def test_failed_health_check_triggers_r7_rollback(self):
        (self.inst / "backend").mkdir()
        (self.inst / "backend" / "server.py").write_text("server_v1_working", encoding="utf-8")
        write_manifest(self.inst, calculate_manifest(self.inst))

        bad_commit = "2222222222222222222222222222222222222222"
        tar_bytes = self._make_tarball({
            "backend/server.py": "server_v2_broken",
            "backend/bad_file.py": "bad",
        })

        with mock.patch("shared.autoupdate.get_remote_commit", return_value=bad_commit), \
             mock.patch.object(self.updater, "pre_switch_check", return_value=(True, None)), \
             mock.patch.object(self.updater, "run_health_check", return_value=(False, "500 Internal Error")), \
             mock.patch.object(self.updater, "restart_affected", return_value=True), \
             mock.patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = io.BytesIO(tar_bytes)

            res = self.updater.run_cycle(force=True)
            self.assertEqual(res, 1)

        # 斷言自動回退：還原 server_v1，清除 bad_file.py
        self.assertEqual((self.inst / "backend" / "server.py").read_text(encoding="utf-8"), "server_v1_working")
        self.assertFalse((self.inst / "backend" / "bad_file.py").exists())

        # failed.json 記錄該 commit
        failed = load_failed_json(self.state)
        self.assertTrue(any(f.get("commit") == bad_commit for f in failed))

    def test_scheduled_task_scripts_exist_and_valid(self):
        install_script = ROOT / "backend" / "autostart" / "install_update_task.ps1"
        uninstall_script = ROOT / "backend" / "autostart" / "uninstall_update_task.ps1"

        self.assertTrue(install_script.is_file(), "install_update_task.ps1 必須存在")
        self.assertTrue(uninstall_script.is_file(), "uninstall_update_task.ps1 必須存在")

        text = install_script.read_text(encoding="utf-8")
        self.assertIn("DesktopPet Update", text)
        self.assertIn("autoupdate.py", text)
        self.assertIn("Register-ScheduledTask", text)


if __name__ == "__main__":
    unittest.main()
