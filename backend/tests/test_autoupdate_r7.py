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
    check_http_health_r7,
    detect_affected_r7,
    main as autoupdate_main,
    pre_switch_check_r7,
    run_health_check_r7,
    stop_backend_process,
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
        self.state_dir = self.base / "state"
        self.old_dir.mkdir()
        self.new_dir.mkdir()
        self.state_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_default_install_dir_is_repo_root(self):
        updater = AutoUpdaterR7(state_dir=self.state_dir, require_ci=False)
        self.assertEqual(updater.install_dir, Path(__file__).resolve().parents[2])

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

    def test_check_held_criteria_r7_held_file(self):
        (self.state_dir / "HELD").write_text("管理者手動暫停", encoding="utf-8")
        held, reason = check_held_criteria_r7(self.new_dir, self.state_dir)
        self.assertTrue(held)
        self.assertEqual(reason, "管理者手動暫停")

    def test_check_held_criteria_r7_backend_manual_steps(self):
        # 建立 backend/manual-steps.txt
        be_dir = self.new_dir / "backend"
        be_dir.mkdir(parents=True)
        (be_dir / "manual-steps.txt").write_text(
            "step-001 r7 請手動執行 db 遷移腳本\n"
            "step-pi pi 這只適用於 Pi\n",
            encoding="utf-8",
        )

        # 尚未套用 step-001 -> 應觸發 held
        held, reason = check_held_criteria_r7(self.new_dir, self.state_dir)
        self.assertTrue(held)
        self.assertIn("step-001 (r7)", reason)
        self.assertNotIn("step-pi", reason)

        # 模擬已套用 step-001
        (self.state_dir / "applied-steps.txt").write_text("step-001 2026-10-30\n", encoding="utf-8")
        held2, reason2 = check_held_criteria_r7(self.new_dir, self.state_dir)
        self.assertFalse(held2)
        self.assertIsNone(reason2)

    def test_check_held_criteria_r7_never_touches_frontend(self):
        # 如果只有 frontend 有手動步驟，R7 絕不受影響
        fe_dir = self.new_dir / "frontend" / "tools" / "deploy"
        fe_dir.mkdir(parents=True)
        (fe_dir / "manual-steps.txt").write_text("step-fe pi 只有樹莓派需要更新 apt\n", encoding="utf-8")

        held, reason = check_held_criteria_r7(self.new_dir, self.state_dir)
        self.assertFalse(held)
        self.assertIsNone(reason)

    @mock.patch("backend.autoupdate.check_http_health_r7")
    def test_run_health_check_r7_success_with_header_commit(self, mock_check_http):
        mock_check_http.return_value = ({"ok": True}, {"x-commit": "1111111111111111111111111111111111111111"})
        ok, reason = run_health_check_r7(
            host="127.0.0.1",
            port=8090,
            target_commit="1111111111111111111111111111111111111111",
            timeout=1.0,
        )
        self.assertTrue(ok)
        self.assertIsNone(reason)

    @mock.patch("backend.autoupdate.check_http_health_r7")
    def test_run_health_check_r7_commit_mismatch_fails(self, mock_check_http):
        mock_check_http.return_value = ({"ok": True}, {"x-commit": "old_commit_00000000000000000000000000000"})
        ok, reason = run_health_check_r7(
            host="127.0.0.1",
            port=8090,
            target_commit="new_commit_11111111111111111111111111111",
            timeout=1.0,
        )
        self.assertFalse(ok)
        self.assertIn("不符", reason)

        # 當伺服器回傳 "dev" 時，也必須嚴格視為 commit 不符而失敗（防止舊版或開發中伺服器放行）
        mock_check_http.return_value = ({"ok": True}, {"x-commit": "dev"})
        ok_dev, reason_dev = run_health_check_r7(
            host="127.0.0.1",
            port=8090,
            target_commit="new_commit_11111111111111111111111111111",
            timeout=1.0,
        )
        self.assertFalse(ok_dev)
        self.assertIn("不符", reason_dev)

    @mock.patch("backend.autoupdate.check_http_health_r7")
    def test_run_health_check_r7_offline(self, mock_check_http):
        mock_check_http.return_value = (None, {})
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
             mock.patch.object(self.updater, "restart_after_rollback", return_value=True) as mock_rb_restart, \
             mock.patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value = io.BytesIO(tar_bytes)

            res = self.updater.run_cycle(force=True)
            self.assertEqual(res, 1)

        # 斷言自動回退：還原 server_v1，清除 bad_file.py
        self.assertEqual((self.inst / "backend" / "server.py").read_text(encoding="utf-8"), "server_v1_working")
        self.assertFalse((self.inst / "backend" / "bad_file.py").exists())
        self.assertTrue(mock_rb_restart.called, "回退後必須執行 restart_after_rollback 重啟後端")

        # failed.json 記錄該 commit
        failed = load_failed_json(self.state)
        self.assertTrue(any(f.get("commit") == bad_commit for f in failed))

    def test_ensure_dependencies_pip_cwd_and_conditions(self):
        (self.inst / "backend").mkdir()
        (self.backup / "backend").mkdir(parents=True)
        (self.inst / "backend" / "requirements.txt").write_text("pytest==8.0.0\n", encoding="utf-8")
        (self.backup / "backend" / "requirements.txt").write_text("pytest==8.0.0\n", encoding="utf-8")

        staged = self.base / "staged"
        staged_be = staged / "backend"
        staged_be.mkdir(parents=True)

        # 1. requirements.txt 與 shared 均未變更 -> 不呼叫 pip
        (staged_be / "requirements.txt").write_text("pytest==8.0.0\n", encoding="utf-8")
        with mock.patch("subprocess.run") as mock_run:
            ok, err = self.updater.ensure_dependencies(staged)
            self.assertTrue(ok)
            mock_run.assert_not_called()

        # 2. requirements.txt 變更 -> 呼叫 pip，且 cwd 必須為 backend 目錄
        (staged_be / "requirements.txt").write_text("pytest==8.1.0\n", encoding="utf-8")
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            ok, err = self.updater.ensure_dependencies(staged)
            self.assertTrue(ok)
            self.assertTrue(mock_run.called)
            kwargs = mock_run.call_args[1]
            self.assertEqual(Path(kwargs.get("cwd")).resolve(), (self.inst / "backend").resolve())

    def test_two_cycles_with_second_cycle_rollback_preserves_local_and_c1_files(self):
        """完整兩週期測試：第一週期成功升級至 C1，第二週期升級至 C2 失敗回退，驗證 C1 與本機檔案完整保留。"""
        # 初始化安裝目錄，包含本機記錄與資料庫
        (self.inst / "backend").mkdir()
        (self.inst / "backend" / "server.py").write_text("v0_initial", encoding="utf-8")
        (self.inst / "local_notes.txt").write_text("my local notes", encoding="utf-8")
        (self.inst / "packs.moved-old").mkdir()
        (self.inst / "packs.moved-old" / "pack.json").write_text("{}", encoding="utf-8")

        # 週期 1：更新至 commit_1
        c1 = "1111111111111111111111111111111111111111"
        tar_c1 = self._make_tarball({
            "backend/server.py": "v1_code",
            "backend/module1.py": "mod1_code",
        })
        with mock.patch("shared.autoupdate.get_remote_commit", return_value=c1), \
             mock.patch.object(self.updater, "pre_switch_check", return_value=(True, None)), \
             mock.patch.object(self.updater, "run_health_check", return_value=(True, None)), \
             mock.patch.object(self.updater, "restart_affected", return_value=True), \
             mock.patch("urllib.request.urlopen", return_value=io.BytesIO(tar_c1)):
            res1 = self.updater.run_cycle(force=True)
            self.assertEqual(res1, 0)

        # 驗證週期 1 成功
        self.assertEqual((self.inst / "backend" / "server.py").read_text(encoding="utf-8"), "v1_code")
        self.assertTrue((self.inst / "backend" / "module1.py").is_file())
        self.assertTrue((self.inst / "local_notes.txt").is_file())
        self.assertTrue((self.inst / "packs.moved-old" / "pack.json").is_file())

        # 週期 2：更新至 commit_2，但健康檢查失敗
        c2 = "2222222222222222222222222222222222222222"
        tar_c2 = self._make_tarball({
            "backend/server.py": "v2_broken",
            "backend/module2_bad.py": "bad_code",
        })
        with mock.patch("shared.autoupdate.get_remote_commit", return_value=c2), \
             mock.patch.object(self.updater, "pre_switch_check", return_value=(True, None)), \
             mock.patch.object(self.updater, "run_health_check", return_value=(False, "crash on startup")), \
             mock.patch.object(self.updater, "restart_affected", return_value=True), \
             mock.patch.object(self.updater, "restart_after_rollback", return_value=True), \
             mock.patch("urllib.request.urlopen", return_value=io.BytesIO(tar_c2)):
            res2 = self.updater.run_cycle(force=True)
            self.assertEqual(res2, 1)

        # 斷言回退結果：
        # 1. server.py 還原為 v1_code
        self.assertEqual((self.inst / "backend" / "server.py").read_text(encoding="utf-8"), "v1_code")
        # 2. module1.py 完好保留
        self.assertEqual((self.inst / "backend" / "module1.py").read_text(encoding="utf-8"), "mod1_code")
        # 3. module2_bad.py 被完全刪除
        self.assertFalse((self.inst / "backend" / "module2_bad.py").exists())
        # 4. 本機檔案未被破壞或刪除
        self.assertEqual((self.inst / "local_notes.txt").read_text(encoding="utf-8"), "my local notes")
        self.assertTrue((self.inst / "packs.moved-old" / "pack.json").is_file())

    def test_scheduled_task_scripts_exist_and_valid(self):
        install_script = ROOT / "backend" / "autostart" / "install_update_task.ps1"
        uninstall_script = ROOT / "backend" / "autostart" / "uninstall_update_task.ps1"

        self.assertTrue(install_script.is_file(), "install_update_task.ps1 必須存在")
        self.assertTrue(uninstall_script.is_file(), "uninstall_update_task.ps1 必須存在")

        text = install_script.read_text(encoding="utf-8")
        self.assertIn("DesktopPet Update", text)
        self.assertIn("autoupdate.py", text)
        self.assertIn("Register-ScheduledTask", text)
        self.assertIn("--install-dir", text)


if __name__ == "__main__":
    unittest.main()
