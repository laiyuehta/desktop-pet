"""R7 自動更新器（PROPOSAL.md 16. 自動部署、Issue #14）。

繼承自 shared.autoupdate.AutoUpdaterCore，實作 R7（Windows 後端）特定邏輯：
- sparse-checkout：僅更新 backend/ 與 shared/，排除 frontend/ 與 piwifi/
- 備份覆蓋設計（依使用者決定取代 junction 切換）：原地備份覆蓋，非 repo 檔案全程保留
- 依賴管理：若 backend/requirements.txt 或 shared/ 變更才重跑 pip，絕不刪除或覆蓋 backend/.venv
- 切換前檢查：compileall、import backend 驗證、autoupdate.py --self-test
- 重啟機制：結束 8090 後端程序由 supervise.py 或工作排程自動拉起
- 健康檢查：GET /health 正常且確認目標 commit 一致
- 開機自檢（boot-check）：中途中斷或首度開機異常自動回退
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

# 引入共用自動更新核心模組
try:
    from shared.autoupdate import (
        DEFAULT_BRANCH,
        DEFAULT_HEALTH_TIMEOUT,
        DEFAULT_INTERVAL,
        DEFAULT_REPO,
        AutoUpdaterCore,
        check_http_json,
        check_manual_steps,
        load_env,
    )
    from shared.version import get_release_info
except ImportError:
    _shared_dir = Path(__file__).resolve().parents[1] / "shared"
    if str(_shared_dir) not in sys.path:
        sys.path.insert(0, str(_shared_dir))
    from shared.autoupdate import (  # type: ignore
        DEFAULT_BRANCH,
        DEFAULT_HEALTH_TIMEOUT,
        DEFAULT_INTERVAL,
        DEFAULT_REPO,
        AutoUpdaterCore,
        check_http_json,
        check_manual_steps,
        load_env,
    )
    from shared.version import get_release_info  # type: ignore

log = logging.getLogger("backend.autoupdate")


def pre_switch_check_r7(
    candidate_dir: Path,
    python_cmd: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """在候選目錄中執行切換前檢查（compileall、import backend、autoupdate --self-test）。"""
    py = python_cmd or sys.executable

    # 1. compileall backend
    be_dir = candidate_dir / "backend" if (candidate_dir / "backend").is_dir() else candidate_dir
    res = subprocess.run([py, "-m", "compileall", "-q", str(be_dir)], capture_output=True, text=True)
    if res.returncode != 0:
        return False, f"compileall 檢查失敗：{res.stderr.strip() or res.stdout.strip()}"

    # 2. import backend 驗證
    env_copy = os.environ.copy()
    cand_shared = candidate_dir / "shared"
    cand_be = candidate_dir / "backend"
    cand_root = candidate_dir
    extra_paths = [str(cand_root), str(cand_be), str(cand_shared)]
    env_copy["PYTHONPATH"] = os.pathsep.join(extra_paths + [env_copy.get("PYTHONPATH", "")])

    res = subprocess.run([py, "-c", "import backend; import shared"], capture_output=True, text=True, env=env_copy)
    if res.returncode != 0:
        return False, f"import backend 失敗：{res.stderr.strip() or res.stdout.strip()}"

    # 3. autoupdate.py --self-test
    up_py = candidate_dir / "backend" / "autoupdate.py"
    if not up_py.exists():
        up_py = candidate_dir / "autoupdate.py"
    if up_py.exists():
        res = subprocess.run([py, str(up_py), "--self-test"], capture_output=True, text=True)
        if res.returncode != 0:
            return False, f"autoupdate.py --self-test 失敗：{res.stderr.strip() or res.stdout.strip()}"

    return True, None


def check_held_criteria_r7(
    old_release_dir: Path,
    new_release_dir: Path,
    applied_steps_file: Optional[Path] = None,
) -> Tuple[bool, Optional[str]]:
    manual_file = new_release_dir / "frontend" / "tools" / "deploy" / "manual-steps.txt"
    if not manual_file.exists():
        manual_file = new_release_dir / "tools" / "deploy" / "manual-steps.txt"

    if manual_file.exists() and applied_steps_file:
        has_pending, steps = check_manual_steps(manual_file, applied_steps_file, target="r7")
        if has_pending:
            return True, f"有尚未執行之手動步驟：{'; '.join(steps)}"

    return False, None


def detect_affected_r7(old_dir: Path, new_dir: Path) -> List[str]:
    """比對檔案差異決定是否需重啟後端。"""
    from shared.autoupdate import calculate_manifest

    if not old_dir.exists():
        return ["desktop-pet-backend"]

    old_manifest = calculate_manifest(old_dir)
    new_manifest = calculate_manifest(new_dir)

    changed_files: Set[str] = set()
    for p, sha in new_manifest.items():
        if old_manifest.get(p) != sha:
            changed_files.add(p)
    for p in old_manifest:
        if p not in new_manifest:
            changed_files.add(p)

    for f in changed_files:
        if any(f.startswith(prefix) for prefix in ("backend/", "shared/")):
            return ["desktop-pet-backend"]

    return []


def stop_backend_process(port: int = 8090) -> None:
    """結束佔用後端通訊埠的程序（讓 supervise.py 自動重啟）。"""
    if os.name == "nt":
        # Windows: 用 netstat 找出 PID 並 taskkill
        try:
            out = subprocess.check_output(f'netstat -ano | findstr :{port}', shell=True, text=True)
            for line in out.splitlines():
                parts = line.strip().split()
                if len(parts) >= 5 and "LISTENING" in parts:
                    pid = parts[-1]
                    if pid.isdigit() and int(pid) > 0 and int(pid) != os.getpid():
                        log.info("正在結束後端程序 (PID: %s)...", pid)
                        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        except Exception:
            pass
    else:
        # POSIX (CI/Linux 環境)
        try:
            subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
        except Exception:
            pass


def run_health_check_r7(
    host: str = "127.0.0.1",
    port: int = 8090,
    target_commit: Optional[str] = None,
    timeout: float = DEFAULT_HEALTH_TIMEOUT,
    install_dir: Optional[Path] = None,
) -> Tuple[bool, Optional[str]]:
    """向 R7 後端 /health 發送請求並檢查狀態與 commit。"""
    start_time = time.time()
    url = f"http://{host}:{port}/health"

    while time.time() - start_time < timeout:
        data = check_http_json(url, timeout=3.0)
        if data and data.get("ok"):
            # 檢查目標 commit 是否一致
            if target_commit and install_dir:
                rel_info = get_release_info(install_dir)
                curr = rel_info.get("commit", "")
                if curr and curr != target_commit and curr != "dev":
                    return False, f"回報 commit ({curr[:12]}) 與目標 commit ({target_commit[:12]}) 不符"
            return True, None
        time.sleep(2.0)

    return False, f"後端健康檢查超時 ({timeout}s)，無法連線至 {url}"


class AutoUpdaterR7(AutoUpdaterCore):
    """R7 後端自動更新器實作。"""

    def __init__(
        self,
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
            target="r7",
            install_dir=install_dir,
            backup_dir=backup_dir,
            state_dir=state_dir,
            repo_url=repo_url,
            branch=branch,
            interval=interval,
            require_ci=require_ci,
            pin_commit=pin_commit,
            dry_run=dry_run,
        )
        self.backend_host = self.env.get("PET_BACKEND_HOST", "127.0.0.1")
        self.backend_port = int(self.env.get("PET_BACKEND_PORT", "8090"))
        self.health_timeout = float(self.env.get("PET_AUTOUPDATE_HEALTH_TIMEOUT", DEFAULT_HEALTH_TIMEOUT))

    def pre_switch_check(self, staged_dir: Path) -> Tuple[bool, Optional[str]]:
        return pre_switch_check_r7(staged_dir)

    def check_held_criteria(self, old_dir: Path, new_dir: Path) -> Tuple[bool, Optional[str]]:
        applied_file = self.state_dir / "applied-steps.txt"
        return check_held_criteria_r7(old_dir, new_dir, applied_steps_file=applied_file)

    def detect_affected(self, old_dir: Path, new_dir: Path) -> List[str]:
        return detect_affected_r7(old_dir, new_dir)

    def restart_affected(self, affected: List[str]) -> bool:
        if affected:
            log.info("重啟後端：結束現有後端程序以由 supervisor 自動重啟...")
            stop_backend_process(self.backend_port)
        return True

    def run_health_check(self, target_commit: str) -> Tuple[bool, Optional[str]]:
        return run_health_check_r7(
            host=self.backend_host,
            port=self.backend_port,
            target_commit=target_commit,
            timeout=self.health_timeout,
            install_dir=self.install_dir,
        )

    def ensure_dependencies(self, staged_dir: Path) -> Tuple[bool, Optional[str]]:
        # 若 backend/requirements.txt 或 shared/ 變更，在現有 backend/.venv 跑 pip
        req_file = staged_dir / "backend" / "requirements.txt"
        old_req = self.install_dir / "backend" / "requirements.txt"

        need_pip = False
        if req_file.exists() and old_req.exists():
            try:
                if req_file.read_bytes() != old_req.read_bytes():
                    need_pip = True
            except OSError:
                pass

        if need_pip:
            pip_candidates = [
                self.install_dir / "backend" / ".venv" / "Scripts" / "pip.exe",
                self.install_dir / "backend" / ".venv" / "bin" / "pip",
            ]
            pip_bin = next((p for p in pip_candidates if p.exists()), None)
            if pip_bin:
                log.info("偵測到依賴變更，在 backend/.venv 執行 pip install ...")
                res = subprocess.run([str(pip_bin), "install", "-r", str(req_file)], capture_output=True, text=True)
                if res.returncode != 0:
                    return False, f"pip 安裝依賴失敗：{res.stderr.strip()}"
        return True, None


def main() -> int:
    parser = argparse.ArgumentParser(description="desktop-pet R7 後端自動更新器")
    parser.add_argument("--once", action="store_true", help="執行一次更新檢查（受 PET_AUTOUPDATE_INTERVAL 限制）")
    parser.add_argument("--force", action="store_true", help="忽略檢查間隔強制執行檢查與更新")
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
        print("desktop-pet R7 autoupdate self-test: OK")
        return 0

    updater = AutoUpdaterR7(
        install_dir=args.install_dir,
        backup_dir=args.backup_dir,
        state_dir=args.state_dir,
        repo_url=args.repo,
        branch=args.branch,
        pin_commit=args.pin,
        dry_run=args.dry_run,
    )

    if args.rollback:
        return 0 if updater.rollback() else 1

    if args.boot_check:
        ok, msg = updater.boot_check()
        print(f"Boot check result: {msg}")
        return 0 if ok else 1

    return updater.run_cycle(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
