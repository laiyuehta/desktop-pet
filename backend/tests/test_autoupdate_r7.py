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
        write_manifest(self.old_dir, calculate_manifest(self.old_dir))
        affected = detect_affected_r7(self.old_dir, self.new_dir)
        self.assertEqual(affected, ["desktop-pet-backend"])

        # 2. 純文件變更不重啟
        (self.old_dir / "backend" / "server.py").write_text("v2", encoding="utf-8")
        (self.old_dir / "README.md").write_text("doc1", encoding="utf-8")
        (self.new_dir / "README.md").write_text("doc2", encoding="utf-8")
        write_manifest(self.old_dir, calculate_manifest(self.old_dir))
        affected2 = detect_affected_r7(self.old_dir, self.new_dir)
        self.assertEqual(affected2, [])

        # 3. backend/requirements.txt 變更需重啟後端
        (self.old_dir / "backend" / "requirements.txt").write_text("pytest==8.0.0\n", encoding="utf-8")
        (self.new_dir / "backend" / "requirements.txt").write_text("pytest==8.1.0\n", encoding="utf-8")
        write_manifest(self.old_dir, calculate_manifest(self.old_dir))
        affected3 = detect_affected_r7(self.old_dir, self.new_dir)
        self.assertEqual(affected3, ["desktop-pet-backend"])

        # 4. 刪除 backend 模組需重啟後端
        (self.new_dir / "backend" / "requirements.txt").write_text("pytest==8.0.0\n", encoding="utf-8")
        (self.old_dir / "backend" / "old_mod.py").write_text("def old(): pass", encoding="utf-8")
        write_manifest(self.old_dir, calculate_manifest(self.old_dir))
        # old_mod.py 不在 new_dir
        affected4 = detect_affected_r7(self.old_dir, self.new_dir)
        self.assertEqual(affected4, ["desktop-pet-backend"])

    def test_detect_affected_r7_untracked_files_ignored_and_missing_manifest_fallback(self):
        """驗證：舊版有未追蹤檔案時不被誤判為刪檔，無 MANIFEST 時安全退回預設重啟。"""
        # A. 無 MANIFEST 時退回預設重啟
        (self.old_dir / "backend").mkdir(parents=True, exist_ok=True)
        (self.new_dir / "backend").mkdir(parents=True, exist_ok=True)
        (self.old_dir / "backend" / "server.py").write_text("same", encoding="utf-8")
        (self.new_dir / "backend" / "server.py").write_text("same", encoding="utf-8")
        # 此時無 MANIFEST.sha256
        self.assertEqual(detect_affected_r7(self.old_dir, self.new_dir), ["desktop-pet-backend"])

        # B. 寫入 MANIFEST.sha256 後，新增本機未追蹤檔案（.env, server.log, config.local.json 等）
        write_manifest(self.old_dir, calculate_manifest(self.old_dir))
        (self.old_dir / "backend" / ".env").write_text("SECRET=123", encoding="utf-8")
        (self.old_dir / "backend" / "server.log").write_text("log line", encoding="utf-8")
        (self.old_dir / "backend" / "config.local.json").write_text("{}", encoding="utf-8")
        (self.old_dir / "backend" / "backend-token").write_text("token", encoding="utf-8")
        (self.old_dir / "shared").mkdir(parents=True, exist_ok=True)
        (self.old_dir / "shared" / "notes.json").write_text("{}", encoding="utf-8")

        # 新版只改前端檔案與文件
        (self.new_dir / "frontend" / "pet").mkdir(parents=True, exist_ok=True)
        (self.new_dir / "frontend" / "pet" / "app.py").write_text("new frontend", encoding="utf-8")
        (self.new_dir / "README.md").write_text("new doc", encoding="utf-8")

        # 斷言：未追蹤檔案不應被當成已刪除檔案，affected 應為 []
        affected = detect_affected_r7(self.old_dir, self.new_dir)
        self.assertEqual(affected, [])

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
    def test_run_health_check_r7_require_commit_match_false_passes_on_commit_mismatch(self, mock_check_http):
        mock_check_http.return_value = ({"ok": True}, {"x-commit": "old_commit_00000000000000000000000000000"})
        ok, reason = run_health_check_r7(
            host="127.0.0.1",
            port=8090,
            target_commit="new_commit_11111111111111111111111111111",
            timeout=1.0,
            require_commit_match=False,
        )
        self.assertTrue(ok)
        self.assertIsNone(reason)

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
        dummy_pip = (
            self.inst / "backend" / ".venv" / ("Scripts" if os.name == "nt" else "bin") / ("pip.exe" if os.name == "nt" else "pip")
        )
        dummy_pip.parent.mkdir(parents=True, exist_ok=True)
        dummy_pip.write_text("# dummy pip", encoding="utf-8")

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            ok, err = self.updater.ensure_dependencies(staged)
            self.assertTrue(ok)
            self.assertTrue(mock_run.called)
            kwargs = mock_run.call_args[1]
            self.assertEqual(Path(kwargs.get("cwd")).resolve(), (self.inst / "backend").resolve())

    def test_ensure_dependencies_fails_when_pip_missing(self):
        """Minor 5 驗證：backend/.venv pip 缺失時拒絕退回 sys.executable，直接回傳錯誤。"""
        (self.inst / "backend").mkdir(parents=True, exist_ok=True)
        (self.backup / "backend").mkdir(parents=True, exist_ok=True)
        (self.inst / "backend" / "requirements.txt").write_text("pytest==8.0.0\n", encoding="utf-8")
        (self.backup / "backend" / "requirements.txt").write_text("pytest==8.0.0\n", encoding="utf-8")

        staged = self.base / "staged"
        staged_be = staged / "backend"
        staged_be.mkdir(parents=True, exist_ok=True)
        (staged_be / "requirements.txt").write_text("pytest==8.1.0\n", encoding="utf-8")

        ok, err = self.updater.ensure_dependencies(staged)
        self.assertFalse(ok)
        self.assertIn("未找到 backend/.venv pip", err or "")

    def test_pre_switch_check_r7_fails_when_venv_missing(self):
        """Minor 5 驗證：pre_switch_check_r7 找不到 venv 直譯器時拒絕退回 sys.executable。"""
        staged = self.base / "staged_check"
        (staged / "backend").mkdir(parents=True, exist_ok=True)
        ok, err = pre_switch_check_r7(staged, install_dir=self.inst)
        self.assertFalse(ok)
        self.assertIn("未找到 backend/.venv 虛擬環境直譯器", err or "")

    def test_pre_switch_check_r7_succeeds_with_venv(self):
        """Minor 5 驗證：pre_switch_check_r7 找到 venv 直譯器時執行檢查。"""
        dummy_py = (
            self.inst / "backend" / ".venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
        )
        dummy_py.parent.mkdir(parents=True, exist_ok=True)
        dummy_py.write_text("# dummy python", encoding="utf-8")

        staged = self.base / "staged_check_ok"
        (staged / "backend").mkdir(parents=True, exist_ok=True)
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            ok, err = pre_switch_check_r7(staged, install_dir=self.inst)
            self.assertTrue(ok)
            self.assertIsNone(err)

    def test_r7_run_cycle_recovers_interrupted_restarting_failure(self):
        """Must-fix 1 驗證：R7 run_cycle 遇 restarting / pending_boot_check 且健康檢查失敗時，執行回退並寫入 failed.json。"""
        bad_commit = "b" * 40
        good_commit = "a" * 40
        (self.inst / "backend").mkdir(parents=True, exist_ok=True)
        (self.inst / "backend" / "server.py").write_text("v2_bad", encoding="utf-8")
        (self.backup / "backend").mkdir(parents=True, exist_ok=True)
        (self.backup / "backend" / "server.py").write_text("v1_good", encoding="utf-8")
        (self.backup / "backup_manifest.json").write_text(json.dumps({"files": ["backend/server.py"]}), encoding="utf-8")

        write_update_state(self.state, {
            "status": "restarting",
            "pending_boot_check": True,
            "target_commit": bad_commit,
            "previous_commit": good_commit,
        })

        with mock.patch.object(self.updater, "run_health_check", return_value=(False, "500 Internal Error")), \
             mock.patch.object(self.updater, "restart_after_rollback", return_value=True):
            rc = self.updater.run_cycle(force=True)
            self.assertEqual(rc, 0)

        # 斷言已回退
        self.assertEqual((self.inst / "backend" / "server.py").read_text(encoding="utf-8"), "v1_good")
        st = load_update_state(self.state)
        self.assertEqual(st.get("status"), "rolled_back")
        self.assertFalse(st.get("pending_boot_check"))
        failed = load_failed_json(self.state)
        self.assertTrue(any(f.get("commit") == bad_commit for f in failed))

    def test_r7_run_cycle_recovers_interrupted_restarting_success(self):
        """Must-fix 1 驗證：R7 run_cycle 遇 restarting / pending_boot_check 且健康檢查成功時，標記為 success。"""
        target_commit = "c" * 40
        write_update_state(self.state, {
            "status": "restarting",
            "pending_boot_check": True,
            "target_commit": target_commit,
            "previous_commit": "a" * 40,
        })

        with mock.patch.object(self.updater, "run_health_check", return_value=(True, None)):
            rc = self.updater.run_cycle(force=True)
            self.assertEqual(rc, 0)

        st = load_update_state(self.state)
        self.assertEqual(st.get("status"), "success")
        self.assertEqual(st.get("current_commit"), target_commit)
        self.assertFalse(st.get("pending_boot_check"))
        failed = load_failed_json(self.state)
        self.assertFalse(any(f.get("commit") == target_commit for f in failed))

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

    def test_frontend_only_commit_does_not_restart_backend_and_passes_health_check(self):
        """Must-fix 2 驗證：當 commit 僅更動前端或文件時，R7 不重啟後端且健康檢查不因 commit 舊版而失敗。"""
        (self.inst / "backend").mkdir()
        (self.inst / "backend" / "server.py").write_text("server_v1", encoding="utf-8")
        (self.inst / "RELEASE.json").write_text(json.dumps({
            "commit": "old_commit_00000000000000000000000000000",
            "short": "old_commit_0",
        }), encoding="utf-8")
        write_manifest(self.inst, calculate_manifest(self.inst))

        new_commit = "9999999999999999999999999999999999999999"
        tar_bytes = self._make_tarball({
            "backend/server.py": "server_v1",
            "frontend/app.py": "print('frontend update')",
            "README.md": "updated docs",
        })

        running_health_resp = (
            {"ok": True, "commit": "old_commit_00000000000000000000000000000"},
            {"x-commit": "old_commit_00000000000000000000000000000"},
        )

        with mock.patch("shared.autoupdate.get_remote_commit", return_value=new_commit), \
             mock.patch.object(self.updater, "pre_switch_check", return_value=(True, None)), \
             mock.patch("backend.autoupdate.check_http_health_r7", return_value=running_health_resp), \
             mock.patch("backend.autoupdate.stop_backend_process") as mock_stop_proc, \
             mock.patch.object(self.updater, "restart_affected", wraps=self.updater.restart_affected) as spy_restart, \
             mock.patch("urllib.request.urlopen", return_value=io.BytesIO(tar_bytes)):

            res = self.updater.run_cycle(force=True)
            self.assertEqual(res, 0, "僅改前端之更新週期應成功返回 0")

        # 斷言：
        # 1. 8090 後端程序絕未被殺死
        mock_stop_proc.assert_not_called()
        # 2. restart_affected 絕未執行
        spy_restart.assert_not_called()
        # 3. RELEASE.json 更新為新 commit
        rel_info = json.loads((self.inst / "RELEASE.json").read_text(encoding="utf-8"))
        self.assertEqual(rel_info.get("commit"), new_commit)
        # 4. failed.json 沒有記錄該 commit
        failed = load_failed_json(self.state)
        self.assertFalse(any(f.get("commit") == new_commit for f in failed))

    def test_requirements_change_or_module_deletion_requires_commit_match(self):
        """驗證當 backend/requirements.txt 變更或後端檔案刪除時，run_health_check 仍然要求 commit 一致。"""
        (self.inst / "backend").mkdir()
        (self.inst / "backend" / "server.py").write_text("server_v1", encoding="utf-8")
        (self.inst / "backend" / "requirements.txt").write_text("pytest==8.0.0\n", encoding="utf-8")
        (self.inst / "RELEASE.json").write_text(json.dumps({
            "commit": "old_commit_00000000000000000000000000000",
            "short": "old_commit_0",
        }), encoding="utf-8")
        write_manifest(self.inst, calculate_manifest(self.inst))

        new_commit = "9999999999999999999999999999999999999999"
        tar_bytes = self._make_tarball({
            "backend/server.py": "server_v1",
            "backend/requirements.txt": "pytest==8.0.0\nrequests>=2.0.0\n",
        })

        # 後端若未成功重啟回傳新 commit，則健康檢查因 commit 不符必須失敗並觸發回退
        running_health_resp = (
            {"ok": True, "commit": "old_commit_00000000000000000000000000000"},
            {"x-commit": "old_commit_00000000000000000000000000000"},
        )

        with mock.patch("shared.autoupdate.get_remote_commit", return_value=new_commit), \
             mock.patch.object(self.updater, "pre_switch_check", return_value=(True, None)), \
             mock.patch.object(self.updater, "ensure_dependencies", return_value=(True, None)), \
             mock.patch.object(self.updater, "restart_affected", return_value=True), \
             mock.patch.object(self.updater, "restart_after_rollback", return_value=True), \
             mock.patch("backend.autoupdate.check_http_health_r7", return_value=running_health_resp), \
             mock.patch("urllib.request.urlopen", return_value=io.BytesIO(tar_bytes)):

            self.updater.health_timeout = 0.5
            res = self.updater.run_cycle(force=True)
            self.assertEqual(res, 1, "requirements 變更若 commit 不吻合應失敗回退")

        # failed.json 應記錄失敗
        failed = load_failed_json(self.state)
        self.assertTrue(any(f.get("commit") == new_commit for f in failed))

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


class TestR7RestartAfterRollbackAndPersistence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.base = Path(self.temp_dir)
        self.inst = self.base / "inst"
        self.backup = self.base / "backup"
        self.state = self.base / "state"
        self.inst.mkdir()
        self.backup.mkdir()
        self.state.mkdir()

        self.updater = AutoUpdaterR7(
            install_dir=self.inst,
            backup_dir=self.backup,
            state_dir=self.state,
            repo_url="https://github.com/laiyuehta/desktop-pet",
            branch="feat/autoupdate-pi",
            require_ci=False,
            dry_run=False,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_restart_after_rollback_skips_when_last_affected_empty(self):
        """若 last_affected 為空清單（僅更動前端/非後端），回退後應略過重啟後端。"""
        self.updater.last_affected = []
        with mock.patch("backend.autoupdate.stop_backend_process") as mock_stop:
            res = self.updater.restart_after_rollback()
            self.assertTrue(res)
            mock_stop.assert_not_called()

    def test_restart_after_rollback_skips_when_persisted_affected_has_no_backend(self):
        """若實例之 last_affected 為 None，但 update_state.json 持久化記錄了受影響服務且不含後端，應略過重啟。"""
        self.updater.last_affected = None
        write_update_state(self.state, {"status": "in_progress", "affected": []})
        with mock.patch("backend.autoupdate.stop_backend_process") as mock_stop:
            res = self.updater.restart_after_rollback()
            self.assertTrue(res)
            mock_stop.assert_not_called()
            self.assertEqual(self.updater.last_affected, [])

    def test_restart_after_rollback_restarts_when_last_affected_contains_backend(self):
        """若 last_affected 包含 desktop-pet-backend，回退後必須結束後端以供重啟。"""
        self.updater.last_affected = ["desktop-pet-backend"]
        with mock.patch("backend.autoupdate.stop_backend_process") as mock_stop:
            res = self.updater.restart_after_rollback()
            self.assertTrue(res)
            mock_stop.assert_called_once_with(self.updater.backend_port, supervise=True)

    def test_restart_after_rollback_restarts_when_persisted_affected_contains_backend(self):
        """若實例 last_affected 為 None，但持久化狀態中包含後端，回退後必須重啟後端。"""
        self.updater.last_affected = None
        write_update_state(self.state, {"status": "in_progress", "affected": ["desktop-pet-backend"]})
        with mock.patch("backend.autoupdate.stop_backend_process") as mock_stop:
            res = self.updater.restart_after_rollback()
            self.assertTrue(res)
            mock_stop.assert_called_once_with(self.updater.backend_port, supervise=True)

    def test_restart_after_rollback_defaults_to_restart_when_no_state(self):
        """若無任何受影響狀態記錄（None），為確保系統可靠性，預設應執行後端重啟。"""
        self.updater.last_affected = None
        with mock.patch("backend.autoupdate.stop_backend_process") as mock_stop:
            res = self.updater.restart_after_rollback()
            self.assertTrue(res)
            mock_stop.assert_called_once_with(self.updater.backend_port, supervise=True)

    def test_interrupted_run_cycle_recovery_skips_restart_if_affected_persisted_empty(self):
        """模擬 run_cycle 啟動時偵測到前次更新中途中斷（status=in_progress 且 affected=[]），回退復原時不應干擾運行中之後端。"""
        target_commit = "1111111111111111111111111111111111111111"
        (self.backup / "backup_manifest.json").write_text(json.dumps({"app.py": "hash"}), encoding="utf-8")
        (self.backup / "app.py").write_text("old_app", encoding="utf-8")
        write_update_state(self.state, {
            "status": "in_progress",
            "target_commit": target_commit,
            "incoming_files": ["app.py"],
            "affected": [],
        })

        fresh_updater = AutoUpdaterR7(
            install_dir=self.inst,
            backup_dir=self.backup,
            state_dir=self.state,
            repo_url="https://github.com/laiyuehta/desktop-pet",
            branch="feat/autoupdate-pi",
            require_ci=False,
            dry_run=False,
        )

        with mock.patch("backend.autoupdate.stop_backend_process") as mock_stop:
            code = fresh_updater.run_cycle(force=True)
            self.assertEqual(code, 0)
            mock_stop.assert_not_called()

        st = load_update_state(self.state)
        self.assertEqual(st.get("status"), "rolled_back")
        failed = load_failed_json(self.state)
        self.assertTrue(any(f.get("commit") == target_commit for f in failed))

    def test_interrupted_boot_check_recovery_skips_restart_if_affected_persisted_empty(self):
        """模擬開機自檢 boot_check 偵測到前次更新中途中斷，若受影響服務不包含後端，復原時應略過重啟。"""
        target_commit = "2222222222222222222222222222222222222222"
        (self.backup / "backup_manifest.json").write_text(json.dumps({"app.py": "hash"}), encoding="utf-8")
        (self.backup / "app.py").write_text("old_app", encoding="utf-8")
        write_update_state(self.state, {
            "status": "in_progress",
            "target_commit": target_commit,
            "incoming_files": ["app.py"],
            "affected": [],
        })

        fresh_updater = AutoUpdaterR7(
            install_dir=self.inst,
            backup_dir=self.backup,
            state_dir=self.state,
            repo_url="https://github.com/laiyuehta/desktop-pet",
            branch="feat/autoupdate-pi",
            require_ci=False,
            dry_run=False,
        )

        with mock.patch("backend.autoupdate.stop_backend_process") as mock_stop:
            recovered, reason = fresh_updater.boot_check()
            self.assertTrue(recovered)
            mock_stop.assert_not_called()

    def test_r7_rollback_returns_failure_when_restart_fails(self):
        """驗證 R7 端回退時若 restart_after_rollback 失敗，rollback() 應回傳 False 並記錄 rollback_failed。"""
        self.backup.mkdir(parents=True, exist_ok=True)
        (self.backup / "app.py").write_text("v1_original", encoding="utf-8")
        (self.backup / "backup_manifest.json").write_text(json.dumps({
            "files": ["app.py"],
            "incoming_files": ["app.py"],
        }), encoding="utf-8")

        with mock.patch.object(self.updater, "restart_after_rollback", return_value=False):
            ok = self.updater.rollback()
            self.assertFalse(ok)

        st = load_update_state(self.state)
        self.assertEqual(st.get("status"), "rollback_failed")
        self.assertEqual(st.get("reason"), "restart_after_rollback_failed")

    def test_restart_after_rollback_returns_false_when_stop_backend_process_fails(self):
        """若結束後端程序失敗，restart_after_rollback 應回傳 False。"""
        self.updater.last_affected = ["desktop-pet-backend"]
        with mock.patch("backend.autoupdate.stop_backend_process", return_value=False) as mock_stop:
            res = self.updater.restart_after_rollback()
            self.assertFalse(res)
            mock_stop.assert_called_once_with(self.updater.backend_port, supervise=True)

    def test_restart_affected_returns_false_when_stop_backend_process_fails(self):
        """若重啟受影響服務時結束後端程序失敗，restart_affected 應回傳 False。"""
        with mock.patch("backend.autoupdate.stop_backend_process", return_value=False) as mock_stop:
            res = self.updater.restart_affected(["desktop-pet-backend"])
            self.assertFalse(res)
            mock_stop.assert_called_once_with(self.updater.backend_port, supervise=False)


if __name__ == "__main__":
    unittest.main()
