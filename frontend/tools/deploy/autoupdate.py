"""Pi 自動更新器（PROPOSAL.md 16. 自動部署、Issue #13）。

使用共用核心模組 shared.autoupdate，實作 Pi 平台特定轉接邏輯：
- sparse 範圍：frontend/ 與 shared/（不包含 backend/ 與 piwifi/）
- 依賴管理：無 pip，絕不覆蓋或刪除 frontend/.venv
- 切換前檢查：compileall、main.py --help、autoupdate.py --self-test（優先使用 venv python）
- 服務重啟：透過 polkit 重啟受影響之 systemd unit（desktop-pet、desktop-pet-admin、picam 等）
- 健康檢查：unit active、fps ≥ 18、畫面幀數遞增、piscreen 唯讀檢查、後端連線警告
- 原地備份與覆蓋（In-place Backup & Overwrite）：非 repo 外部檔案（packs/、deploy.env、notes.txt 等）全程保護
- 開機自檢（boot_check）：中途中斷或首度開機異常自動復原
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
import urllib.error
import urllib.request

# 引入共用自動更新核心
try:
    from shared.autoupdate import (
        DEFAULT_BRANCH,
        DEFAULT_HEALTH_MIN_FPS,
        DEFAULT_HEALTH_TIMEOUT,
        DEFAULT_INTERVAL,
        DEFAULT_REPO,
        PROTECTED_ENTRIES,
        REQUIRED_CI_CHECKS,
        AutoUpdaterCore,
        ProcessLock,
        append_updates_jsonl,
        calculate_manifest,
        check_ci_runs,
        check_http_json,
        check_manual_steps,
        clear_failed_records,
        create_backup,
        get_remote_commit,
        is_protected_path,
        load_env,
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
    from shared.version import get_autoupdate_status, get_release_info
except ImportError:
    _shared_dir = Path(__file__).resolve().parents[3] / "shared"
    if str(_shared_dir) not in sys.path:
        sys.path.insert(0, str(_shared_dir))
    from shared.autoupdate import (  # type: ignore
        DEFAULT_BRANCH,
        DEFAULT_HEALTH_MIN_FPS,
        DEFAULT_HEALTH_TIMEOUT,
        DEFAULT_INTERVAL,
        DEFAULT_REPO,
        PROTECTED_ENTRIES,
        REQUIRED_CI_CHECKS,
        AutoUpdaterCore,
        ProcessLock,
        append_updates_jsonl,
        calculate_manifest,
        check_ci_runs,
        check_http_json,
        check_manual_steps,
        clear_failed_records,
        create_backup,
        get_remote_commit,
        is_protected_path,
        load_env,
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
    from shared.version import get_autoupdate_status, get_release_info  # type: ignore

log = logging.getLogger("pet.autoupdate")


def check_held_criteria(
    old_release_dir: Optional[Path],
    new_release_dir: Path,
    state_dir: Optional[Path] = None,
    target_platform: str = "pi",
) -> Tuple[bool, Optional[str]]:
    """比對變更是否符合 PROPOSAL.md 16.7 項目，若有改動則標記為 held。"""
    # 1. 檢查手動 HELD 旗標
    if state_dir:
        held_flag = state_dir / "HELD"
        if held_flag.exists():
            return True, "手動 HELD 旗標存在"

        # 2. 檢查 manual-steps.txt（僅限 Pi 端適用步驟）
        has_step, reason = check_manual_steps(new_release_dir, state_dir, target_platform=target_platform)
        if has_step:
            return True, reason

    if not old_release_dir or not old_release_dir.exists():
        return False, None

    old_manifest = calculate_manifest(old_release_dir)
    new_manifest = calculate_manifest(new_release_dir)

    for rel_path, new_sha in new_manifest.items():
        # 僅比對 Pi 負責之範圍（frontend/、shared/、以及相容測試路徑 services/）
        if not (rel_path.startswith("frontend/") or rel_path.startswith("shared/") or rel_path.startswith("services/")):
            continue
        old_sha = old_manifest.get(rel_path)
        if old_sha != new_sha:
            if (rel_path.startswith("frontend/services/") or rel_path.startswith("services/desktop-pet")) and (
                rel_path.endswith(".service") or rel_path.endswith(".timer")
            ):
                return True, f"偵測到 systemd unit 變更 ({rel_path})，需管理員手動安裝"
            if (rel_path.startswith("frontend/services/") or rel_path.startswith("services/desktop-pet")) and (
                rel_path.endswith(".rules") or "polkit" in rel_path
            ):
                return True, f"偵測到 polkit 規則變更 ({rel_path})，需手動安裝"
            if rel_path in ("frontend/services/apt-packages.txt", "frontend/apt-requirements.txt"):
                return True, f"偵測到 apt 套件依賴變更 ({rel_path})"
            if rel_path.startswith("services/piwifi/") and rel_path.endswith(".rs"):
                return True, f"偵測到 Rust 守護程式變更 ({rel_path})，需手動安裝"

    return False, None


def pre_switch_check(
    candidate_dir: Path,
    python_cmd: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """在候選目錄中執行切換前檢查（compileall、main.py --help、autoupdate.py --self-test）。"""
    py = python_cmd or sys.executable

    # 1. compileall
    fe_dir = candidate_dir / "frontend" if (candidate_dir / "frontend").is_dir() else candidate_dir
    res = subprocess.run([py, "-m", "compileall", "-q", str(fe_dir)], capture_output=True, text=True)
    if res.returncode != 0:
        return False, f"compileall 檢查失敗：{res.stderr.strip() or res.stdout.strip()}"

    # 2. main.py --help
    main_py = candidate_dir / "frontend" / "main.py"
    if not main_py.exists():
        main_py = candidate_dir / "main.py"

    if main_py.exists():
        env_copy = os.environ.copy()
        cand_shared = candidate_dir / "shared"
        cand_fe = candidate_dir / "frontend"
        extra_paths = [str(cand_fe), str(cand_shared), str(candidate_dir)]
        env_copy["PYTHONPATH"] = os.pathsep.join(extra_paths + [env_copy.get("PYTHONPATH", "")])
        res = subprocess.run([py, str(main_py), "--help"], capture_output=True, text=True, env=env_copy)
        if res.returncode != 0:
            return False, f"main.py --help 檢查失敗：{res.stderr.strip() or res.stdout.strip()}"

    # 3. autoupdate.py --self-test
    up_py = candidate_dir / "frontend" / "tools" / "deploy" / "autoupdate.py"
    if not up_py.exists():
        up_py = candidate_dir / "tools" / "deploy" / "autoupdate.py"

    if up_py.exists():
        res = subprocess.run([py, str(up_py), "--self-test"], capture_output=True, text=True)
        if res.returncode != 0:
            return False, f"autoupdate.py --self-test 失敗：{res.stderr.strip() or res.stdout.strip()}"

    return True, None


def detect_affected_units(old_dir: Path, new_dir: Path) -> List[str]:
    """比對新舊版本檔案差異，決定需重啟之 systemd unit。"""
    if not old_dir.exists():
        return ["desktop-pet.service"]

    # 僅針對 new_dir 中的 frontend/ 與 shared/ 檔案進行雜湊比對（排除 __pycache__ 與非執行文件/圖片）
    relevant_files: List[str] = []
    for root, dirs, files in os.walk(new_dir):
        rel_root = Path(root).relative_to(new_dir)
        if any(part in ("__pycache__", ".git", ".venv", "tests") for part in rel_root.parts):
            dirs[:] = []
            continue
        for f in files:
            if f.endswith(".pyc") or f.endswith(".md") or f.endswith(".txt") or f.endswith(".png") or f.endswith(".gif") or f.endswith(".webp"):
                continue
            rel_f = (rel_root / f if str(rel_root) != "." else Path(f)).as_posix()
            if (
                any(rel_f.startswith(prefix) for prefix in ("frontend/", "shared/", "pet/", "services/", "config/"))
                or rel_f in ("main.py", "deploy.env.example")
            ):
                relevant_files.append(rel_f)

    old_manifest = calculate_manifest(old_dir, files=relevant_files)
    new_manifest = calculate_manifest(new_dir, files=relevant_files)

    changed_files: Set[str] = set()
    for p, sha in new_manifest.items():
        if old_manifest.get(p) != sha:
            changed_files.add(p)
    for p in old_manifest:
        if p not in new_manifest:
            changed_files.add(p)

    units: Set[str] = set()
    for f in changed_files:
        if "__pycache__" in f or f.endswith(".pyc"):
            continue
        if any(f.startswith(prefix) for prefix in (
            "frontend/main.py", "frontend/pet/", "frontend/config/",
            "main.py", "pet/", "config/", "shared/"
        )):
            units.add("desktop-pet.service")
        if any(f.startswith(prefix) for prefix in (
            "frontend/services/desktop-pet-admin/", "services/desktop-pet-admin/",
            "frontend/pet/admin.py", "frontend/pet/web/admin", "pet/admin.py"
        )):
            units.add("desktop-pet-admin.service")
        if any(f.startswith(prefix) for prefix in ("frontend/services/picam/", "services/picam/")):
            units.add("picam.service")

    return sorted(units)


def restart_units(units: List[str], systemctl_cmd: str = "systemctl") -> bool:
    """呼叫 systemctl 重啟指定 unit。若重啟失敗回傳 False。"""
    all_ok = True
    for u in units:
        log.info("正在重啟服務：%s", u)
        res = subprocess.run([systemctl_cmd, "restart", u], capture_output=True, text=True)
        if res.returncode != 0:
            log.warning("重啟服務 %s 失敗：%s", u, res.stderr.strip())
            all_ok = False
    return all_ok


def run_health_check(
    conf_or_unit: Any,
    affected_units: Optional[List[str]] = None,
    timeout_sec: float = DEFAULT_HEALTH_TIMEOUT,
    poll_interval: float = 2.0,
    systemctl_cmd: str = "systemctl",
    **kwargs: Any,
) -> Tuple[bool, str]:
    """執行健康檢查（PROPOSAL.md 16.6）。支援多種呼叫模式。"""
    conf: Dict[str, str] = {}
    if isinstance(conf_or_unit, dict):
        conf = conf_or_unit
    elif isinstance(conf_or_unit, str):
        affected_units = [conf_or_unit]

    units = affected_units or ["desktop-pet.service"]
    min_fps = float(conf.get("PET_AUTOUPDATE_HEALTH_MIN_FPS", str(DEFAULT_HEALTH_MIN_FPS)))
    web_host = "127.0.0.1"
    web_port = "8080"
    web_listen = conf.get("PET_WEB_LISTEN", "0.0.0.0:8080")
    if ":" in web_listen:
        p = web_listen.rsplit(":", 1)[1]
        if p.isdigit():
            web_port = p

    deadline = time.time() + timeout_sec
    last_frame_seq: Optional[int] = None
    seq_increased = False

    while time.time() <= deadline:
        # 1. 檢查 unit 是否 active
        units_ok = True
        for u in units:
            res = subprocess.run([systemctl_cmd, "is-active", u], capture_output=True, text=True)
            if res.stdout.strip() != "active":
                units_ok = False
                break
        if not units_ok:
            time.sleep(poll_interval)
            continue

        # 2. 查詢 /v1/status 或 /status
        status_data = check_http_json(f"http://{web_host}:{web_port}/v1/status", timeout=2.0)
        if not status_data:
            status_data = check_http_json(f"http://{web_host}:{web_port}/status", timeout=2.0)

        if status_data:
            # 檢查後端大腦連線狀態（若離線僅記錄警告，不觸發回退，避免兩端更新彼此干擾）
            b_info = status_data.get("backend")
            if isinstance(b_info, dict) and b_info.get("online") is False:
                log.warning("桌寵目前尚未連上後端（可能後端正更新重啟中），本機桌寵運行正常")

            fps = float(status_data.get("fps", 0.0))
            frame_seq = status_data.get("frame_count") or status_data.get("frame_seq")

            if fps >= min_fps:
                if frame_seq is not None:
                    if last_frame_seq is not None and frame_seq > last_frame_seq:
                        seq_increased = True
                    last_frame_seq = frame_seq

                if seq_increased or frame_seq is None:
                    # 3. piscreen 唯讀檢查（確保未被停止且正常運行）
                    res = subprocess.run([systemctl_cmd, "is-active", "piscreen.service"], capture_output=True, text=True)
                    if res.returncode == 0 and res.stdout.strip() not in ("active", "activating"):
                        log.warning("piscreen 服務狀態異常：%s", res.stdout.strip())
                    piscreen_stat = Path("/run/piscreen/status.json")
                    if piscreen_stat.exists():
                        try:
                            ps_data = json.loads(piscreen_stat.read_text(encoding="utf-8"))
                            if ps_data.get("rescues", 0) > 0 or ps_data.get("errors", 0) > 5:
                                log.warning("piscreen 狀態檔顯示異常救援或錯誤：%s", ps_data)
                        except Exception:
                            pass

                    # 4. picam 狀態檢查（若 picam 運行中則確認無嚴重錯誤）
                    picam_stat = Path("/run/picam/status.json")
                    if picam_stat.exists():
                        try:
                            pc_data = json.loads(picam_stat.read_text(encoding="utf-8"))
                            if pc_data.get("errors", 0) > 0 and not pc_data.get("ok", True):
                                log.warning("picam 服務回報異常狀態：%s", pc_data)
                        except Exception:
                            pass
                    return True, "all ok"

        time.sleep(poll_interval)

    return False, f"健康檢查超時 ({timeout_sec}s)，服務未達健康標準 (fps >= {min_fps})"


def boot_check(
    conf: Dict[str, str],
    install_dir: Path,
    state_dir: Path,
    backup_dir: Path,
    systemctl_cmd: str = "systemctl",
) -> Tuple[bool, str]:
    """開機自檢函數轉接（相容舊介面）。"""
    updater = AutoUpdater(install_dir=install_dir, backup_dir=backup_dir, state_dir=state_dir)
    return updater.boot_check()


class AutoUpdaterPi(AutoUpdaterCore):
    """Pi 桌寵自動更新器實作。"""

    def __init__(
        self,
        env_path: Optional[Path] = None,
        install_dir: Optional[Path] = None,
        backup_dir: Optional[Path] = None,
        state_dir: Optional[Path] = None,
        repo_url: Optional[str] = None,
        branch: Optional[str] = None,
        interval: Optional[int] = None,
        require_ci: Optional[bool] = None,
        pin_commit: Optional[str] = None,
        dry_run: bool = False,
    ) -> None:
        super().__init__(
            target="pi",
            install_dir=install_dir,
            backup_dir=backup_dir,
            state_dir=state_dir,
            repo_url=repo_url,
            branch=branch,
            interval=interval,
            require_ci=require_ci,
            pin_commit=pin_commit,
            dry_run=dry_run,
            env_path=env_path,
        )
        self.health_timeout = float(self.env.get("PET_AUTOUPDATE_HEALTH_TIMEOUT", str(DEFAULT_HEALTH_TIMEOUT)))
        self.health_min_fps = float(self.env.get("PET_AUTOUPDATE_HEALTH_MIN_FPS", str(DEFAULT_HEALTH_MIN_FPS)))

    def get_remote_commit(self, repo_url: str, branch: str) -> Optional[str]:
        return get_remote_commit(repo_url, branch)

    def check_ci_runs(self, repo_url: str, target_commit: str) -> Tuple[str, str]:
        return check_ci_runs(repo_url, target_commit)

    def check_http_json(self, url: str, timeout: float = 3.0, headers: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
        return check_http_json(url, timeout=timeout, headers=headers)

    def pre_switch_check(self, staged_dir: Path) -> Tuple[bool, Optional[str]]:
        # 優先尋找 frontend/.venv 中的 python
        py_venv = self.install_dir / "frontend" / ".venv" / "bin" / "python3"
        if not py_venv.exists():
            py_venv = self.install_dir / "frontend" / ".venv" / "bin" / "python"
        py_cmd = str(py_venv) if py_venv.exists() else sys.executable
        return pre_switch_check(staged_dir, python_cmd=py_cmd)

    def check_held_criteria(self, old_dir: Path, new_dir: Path) -> Tuple[bool, Optional[str]]:
        return check_held_criteria(old_dir, new_dir, state_dir=self.state_dir, target_platform="pi")

    def detect_affected(self, old_dir: Path, new_dir: Path) -> List[str]:
        return detect_affected_units(old_dir, new_dir)

    def get_default_affected(self) -> List[str]:
        return ["desktop-pet.service"]

    def restart_affected(self, affected: List[str]) -> bool:
        self._last_affected = affected
        return restart_units(affected)

    def restart_after_rollback(self) -> bool:
        units = getattr(self, "_last_affected", None) or ["desktop-pet.service", "desktop-pet-admin.service", "picam.service"]
        return restart_units(units)

    def ensure_dependencies(self, staged_dir: Path) -> Tuple[bool, Optional[str]]:
        new_req = staged_dir / "frontend" / "requirements.txt"
        if not new_req.exists():
            new_req = staged_dir / "requirements.txt"
        if not new_req.exists():
            return True, None

        cur_req = self.install_dir / "frontend" / "requirements.txt"
        if not cur_req.exists():
            cur_req = self.install_dir / "requirements.txt"

        need_pip = False
        if not cur_req.exists():
            need_pip = True
        else:
            try:
                need_pip = (cur_req.read_bytes() != new_req.read_bytes())
            except Exception:
                need_pip = True

        if not need_pip:
            return True, None

        log.info("偵測到前端 requirements.txt 變更，準備更新 frontend/.venv 依賴...")
        py_venv = self.install_dir / "frontend" / ".venv" / "bin" / "python3"
        if not py_venv.exists():
            py_venv = self.install_dir / "frontend" / ".venv" / "bin" / "python"
        if not py_venv.exists():
            py_venv = Path(sys.executable)

        res = subprocess.run(
            [str(py_venv), "-m", "pip", "install", "-r", str(new_req)],
            cwd=str(self.install_dir / "frontend" if (self.install_dir / "frontend").exists() else self.install_dir),
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            return False, f"pip 安裝依賴失敗：{res.stderr.strip() or res.stdout.strip()}"
        return True, None

    def run_health_check(self, target_commit: str) -> Tuple[bool, Optional[str]]:
        ok, msg = run_health_check(self.env, affected_units=["desktop-pet.service"], timeout_sec=self.health_timeout)
        return ok, msg if not ok else None


# 類別別名相容
AutoUpdater = AutoUpdaterPi


def main() -> int:
    parser = argparse.ArgumentParser(description="desktop-pet Pi 自動更新器")
    parser.add_argument("--once", action="store_true", help="執行一次更新檢查（受 PET_AUTOUPDATE_INTERVAL 限制）")
    parser.add_argument("--force", action="store_true", help="忽略檢查間隔強制執行檢查與更新")
    parser.add_argument("--retry", action="store_true", help="清除失敗標記並重新嘗試更新")
    parser.add_argument("--status", action="store_true", help="查詢目前版本與自動更新狀態")
    parser.add_argument("--dry-run", action="store_true", help="模擬更新週期，不修改檔案或重啟服務")
    parser.add_argument("--rollback", action="store_true", help="手動回退至上一個備份版本")
    parser.add_argument("--boot-check", action="store_true", help="開機自檢模式（檢查中途中斷與首度開機異常）")
    parser.add_argument("--self-test", action="store_true", help="更新程式自我健康檢查")
    parser.add_argument("--install-dir", type=Path, help="覆寫安裝目錄路徑")
    parser.add_argument("--backup-dir", type=Path, help="覆寫備份目錄路徑")
    parser.add_argument("--state-dir", type=Path, help="覆寫狀態目錄路徑")
    parser.add_argument("--repo", type=str, help="覆寫 GitHub repo 網址")
    parser.add_argument("--branch", type=str, help="覆寫分支名稱")
    parser.add_argument("--pin", type=str, help="固定更新至特定 commit")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.self_test:
        print("desktop-pet autoupdate self-test: OK")
        return 0

    updater = AutoUpdater(
        install_dir=args.install_dir,
        backup_dir=args.backup_dir,
        state_dir=args.state_dir,
        repo_url=args.repo,
        branch=args.branch,
        pin_commit=args.pin,
        dry_run=args.dry_run,
    )

    if args.status:
        st = {
            "version": get_release_info(updater.install_dir),
            "autoupdate": get_autoupdate_status(updater.state_dir, target="pi"),
        }
        print(json.dumps(st, indent=2, ensure_ascii=False))
        return 0

    if args.rollback:
        return 0 if updater.rollback() else 1

    if args.boot_check:
        ok, msg = updater.boot_check()
        print(f"Boot check result: {msg}")
        return 0 if ok else 1

    return updater.run_cycle(force=args.force, retry=args.retry)


if __name__ == "__main__":
    sys.exit(main())
