"""R7 自動更新器（PROPOSAL.md 16. 自動部署、Issue #14）。

繼承自 shared.autoupdate.AutoUpdaterCore，實作 R7（Windows 後端）特定邏輯：
- sparse-checkout：僅更新 backend/ 與 shared/，排除 frontend/ 與 piwifi/
- 備份覆蓋設計：原地備份覆蓋，非 repo 檔案（data/、.env、本機記錄等）全程保留
- 依賴管理：若 backend/requirements.txt 或 shared/ 變更才在 backend/ 目錄跑 pip install -r requirements.txt
- 切換前檢查：compileall、import backend; import shared 驗證、autoupdate.py --self-test
- 重啟機制：結束 8090 後端程序由 supervise.py 或工作排程自動拉起
- 健康檢查：GET /health 正常且確認伺服器真實 commit (Header X-Commit 或 body commit) 一致
- 開機自檢（boot-check）：中途中斷或首度開機異常自動回退
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple

# 引入共用自動更新核心模組
try:
    from shared.autoupdate import (
        DEFAULT_BRANCH,
        DEFAULT_HEALTH_TIMEOUT,
        DEFAULT_INTERVAL,
        DEFAULT_REPO,
        AutoUpdaterCore,
        calculate_manifest,
        check_manual_steps,
        load_env,
        load_manifest,
        load_update_state,
    )
    from shared.version import get_autoupdate_status, get_release_info
except ImportError:
    try:
        from shared.shared.autoupdate import (
            DEFAULT_BRANCH,
            DEFAULT_HEALTH_TIMEOUT,
            DEFAULT_INTERVAL,
            DEFAULT_REPO,
            AutoUpdaterCore,
            calculate_manifest,
            check_manual_steps,
            load_env,
            load_manifest,
            load_update_state,
        )
        from shared.shared.version import get_autoupdate_status, get_release_info
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
            calculate_manifest,
            check_manual_steps,
            load_env,
            load_manifest,
        )
        from shared.version import get_autoupdate_status, get_release_info  # type: ignore

log = logging.getLogger("backend.autoupdate")


def pre_switch_check_r7(
    candidate_dir: Path,
    python_cmd: Optional[str] = None,
    install_dir: Optional[Path] = None,
) -> Tuple[bool, Optional[str]]:
    """在候選目錄中執行切換前檢查（compileall、import backend/shared、autoupdate --self-test）。"""
    py = python_cmd
    if not py:
        # 優先尋找 backend/.venv 直譯器
        search_dirs = [candidate_dir]
        if install_dir:
            search_dirs.append(install_dir)
        for base_p in search_dirs:
            for cand in [
                base_p / "backend" / ".venv" / "Scripts" / "python.exe",
                base_p / "backend" / ".venv" / "bin" / "python",
                base_p / ".venv" / "Scripts" / "python.exe",
                base_p / ".venv" / "bin" / "python",
            ]:
                if cand.is_file():
                    py = str(cand)
                    break
            if py:
                break
    if not py:
        py = sys.executable

    # 1. compileall backend 與 shared
    for sub in ("backend", "shared"):
        target_sub = candidate_dir / sub
        if target_sub.is_dir():
            try:
                res = subprocess.run([py, "-m", "compileall", "-q", str(target_sub)], capture_output=True, text=True, timeout=60)
                if res.returncode != 0:
                    return False, f"compileall {sub} 檢查失敗：{res.stderr.strip() or res.stdout.strip()}"
            except Exception as exc:
                return False, f"compileall {sub} 異常或逾時：{exc}"

    # 2. import backend / shared 驗證
    env_copy = os.environ.copy()
    extra_paths = [str(candidate_dir), str(candidate_dir / "backend"), str(candidate_dir / "shared")]
    env_copy["PYTHONPATH"] = os.pathsep.join(extra_paths + [env_copy.get("PYTHONPATH", "")])

    try:
        res = subprocess.run(
            [py, "-c", "import backend; import shared"],
            capture_output=True,
            text=True,
            env=env_copy,
            timeout=30,
        )
        if res.returncode != 0:
            return False, f"import backend/shared 失敗：{res.stderr.strip() or res.stdout.strip()}"
    except Exception as exc:
        return False, f"import backend/shared 異常或逾時：{exc}"

    # 3. autoupdate.py --self-test
    up_py = candidate_dir / "backend" / "autoupdate.py"
    if not up_py.exists():
        up_py = candidate_dir / "autoupdate.py"
    if up_py.exists():
        try:
            res = subprocess.run([py, str(up_py), "--self-test"], capture_output=True, text=True, timeout=30)
            if res.returncode != 0:
                return False, f"autoupdate.py --self-test 失敗：{res.stderr.strip() or res.stdout.strip()}"
        except Exception as exc:
            return False, f"autoupdate.py --self-test 異常或逾時：{exc}"

    return True, None


def check_held_criteria_r7(
    new_release_dir: Path,
    state_dir: Path,
) -> Tuple[bool, Optional[str]]:
    """檢查 R7 是否滿足 held 條件（HELD 標記存在或有未執行之 backend 手動步驟）。"""
    held_file = state_dir / "HELD"
    if held_file.exists():
        try:
            reason = held_file.read_text(encoding="utf-8").strip() or "HELD 檔案存在"
        except Exception:
            reason = "HELD 檔案存在"
        return True, reason

    # 檢查手動步驟（僅針對 R7，絕不讀取 frontend/ 目錄）
    has_pending, reason = check_manual_steps(new_release_dir, state_dir, target_platform="r7")
    if has_pending:
        return True, reason

    return False, None


def _is_relevant_r7_file(rel_f: str) -> bool:
    """判斷指定相對路徑是否屬於受控之 R7 檔案（排除 __pycache__、非執行文字檔與文件）。"""
    parts = Path(rel_f).parts
    if any(part in ("__pycache__", ".git", ".venv", "tests") for part in parts):
        return False
    if rel_f.endswith(".pyc") or rel_f.endswith(".md"):
        return False
    # 排除非 requirements 的純文字檔（例如 notes.txt、license.txt）
    if rel_f.endswith(".txt") and "requirements" not in rel_f.lower():
        return False
    return rel_f.startswith("backend/") or rel_f.startswith("shared/")


def _scan_relevant_r7_files(base_dir: Path) -> Set[str]:
    """掃描指定目錄下屬於 backend/ 與 shared/ 的受控檔案清單（排除 __pycache__、非執行文件等）。"""
    files_found: Set[str] = set()
    if not base_dir.exists():
        return files_found
    for root, dirs, files in os.walk(base_dir):
        rel_root = Path(root).relative_to(base_dir)
        if any(part in ("__pycache__", ".git", ".venv", "tests") for part in rel_root.parts):
            dirs[:] = []
            continue
        for f in files:
            rel_f = (rel_root / f if str(rel_root) != "." else Path(f)).as_posix()
            if _is_relevant_r7_file(rel_f):
                files_found.add(rel_f)
    return files_found


def detect_affected_r7(old_dir: Path, new_dir: Path) -> List[str]:
    """比對檔案差異決定是否需重啟後端（涵蓋修改、刪除與 requirements 異動）。
    
    舊版比對集合取自 old_dir / MANIFEST.sha256（僅計算前版實際部署之檔案，排除本機未追蹤檔案）；
    若無 MANIFEST.sha256 則退回預設重啟。
    """
    if not old_dir.exists():
        return ["desktop-pet-backend"]

    manifest_file = old_dir / "MANIFEST.sha256"
    if not manifest_file.exists():
        log.info("舊版目錄缺少 MANIFEST.sha256，退回預設重啟後端")
        return ["desktop-pet-backend"]

    manifest_entries = load_manifest(manifest_file)
    if not manifest_entries:
        log.info("MANIFEST.sha256 為空或讀取失敗，退回預設重啟後端")
        return ["desktop-pet-backend"]

    old_files = {p for p in manifest_entries if _is_relevant_r7_file(p)}
    new_files = _scan_relevant_r7_files(new_dir)
    relevant_files = sorted(old_files | new_files)

    old_manifest = calculate_manifest(old_dir, files=relevant_files)
    # 確保 MANIFEST 中記錄的舊版檔案仍保留其紀錄（用於比對新版已刪除檔案）
    for p in old_files:
        if p not in old_manifest and p in manifest_entries:
            old_manifest[p] = manifest_entries[p]

    new_manifest = calculate_manifest(new_dir, files=relevant_files)

    changed_files: Set[str] = set()
    for p, sha in new_manifest.items():
        if old_manifest.get(p) != sha:
            changed_files.add(p)
    for p in old_manifest:
        if p not in new_manifest:
            changed_files.add(p)

    units: List[str] = []
    for f in changed_files:
        if f.startswith("backend/autostart/supervise.py") and "desktop-pet-supervisor" not in units:
            units.append("desktop-pet-supervisor")
        if any(f.startswith(prefix) for prefix in ("backend/", "shared/")) and "desktop-pet-backend" not in units:
            units.append("desktop-pet-backend")

    return units


def stop_backend_process(port: int = 8090, supervise: bool = False) -> None:
    """結束佔用後端通訊埠的程序（讓 supervise.py 自動重啟）。若 supervise=True 則一併重啟 supervisor 工作。"""
    if os.name == "nt":
        if supervise:
            try:
                subprocess.run(["schtasks", "/End", "/TN", "DesktopPet Backend"], capture_output=True, timeout=15)
            except Exception:
                pass
        # Windows: 用 netstat 找出 PID 並 taskkill
        try:
            out = subprocess.check_output(f'netstat -ano | findstr :{port}', shell=True, text=True, timeout=15)
            for line in out.splitlines():
                parts = line.strip().split()
                if len(parts) >= 5 and "LISTENING" in parts:
                    pid = parts[-1]
                    if pid.isdigit() and int(pid) > 0 and int(pid) != os.getpid():
                        log.info("正在結束後端程序 (PID: %s)...", pid)
                        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=15)
        except Exception:
            pass
        if supervise:
            try:
                subprocess.run(["schtasks", "/Run", "/TN", "DesktopPet Backend"], capture_output=True, timeout=15)
            except Exception:
                pass
    else:
        # POSIX (CI/Linux 環境)
        try:
            subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True, timeout=15)
        except Exception:
            pass


def check_http_health_r7(url: str, timeout: float = 3.0) -> Tuple[Optional[Dict[str, Any]], Dict[str, str]]:
    req = urllib.request.Request(url, headers={"User-Agent": "desktop-pet-autoupdate"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            data = resp.read()
            body = json.loads(data.decode("utf-8")) if data else {}
            return body, headers
    except Exception:
        return None, {}


def run_health_check_r7(
    host: str = "127.0.0.1",
    port: int = 8090,
    target_commit: Optional[str] = None,
    timeout: float = DEFAULT_HEALTH_TIMEOUT,
    install_dir: Optional[Path] = None,
    require_commit_match: bool = True,
) -> Tuple[bool, Optional[str]]:
    """向 R7 後端 /health 發送請求並檢查狀態與伺服器回傳之 commit。

    若 require_commit_match=False（例如本次更新未更動 backend/ 或 shared/，後端未重啟），
    則只要 /health 回傳 ok 即可放行，不要求伺服器 commit 與新 target_commit 一致。
    """
    start_time = time.time()
    url = f"http://{host}:{port}/health"
    last_err: Optional[str] = None

    while time.time() - start_time < timeout:
        body, headers = check_http_health_r7(url, timeout=3.0)
        if body and body.get("ok"):
            if not require_commit_match:
                return True, None
            # 從 Header X-Commit 或 body commit 取得伺服器執行的真實 commit
            server_commit = headers.get("x-commit") or body.get("commit")
            if target_commit:
                if server_commit and (server_commit == target_commit or target_commit.startswith(server_commit)):
                    return True, None
                last_err = f"後端回報 commit ({server_commit}) 與目標 commit ({target_commit[:12]}) 不符"
            else:
                return True, None
        time.sleep(1.0)

    return False, last_err or f"後端健康檢查超時 ({timeout}s)，無法連線至 {url}"


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
        if install_dir is None:
            env_inst = os.environ.get("PET_INSTALL_DIR")
            if env_inst:
                install_dir = Path(os.path.expanduser(os.path.expandvars(env_inst)))
            else:
                # 預設為執行後端之 repo 根目錄（而非 LOCALAPPDATA）
                install_dir = Path(__file__).resolve().parent.parent

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
        if getattr(self, "last_affected", None) is None:
            st = load_update_state(self.state_dir)
            self.last_affected = st.get("affected")

    def pre_switch_check(self, staged_dir: Path) -> Tuple[bool, Optional[str]]:
        return pre_switch_check_r7(staged_dir, install_dir=self.install_dir)

    def check_held_criteria(self, old_dir: Path, new_dir: Path) -> Tuple[bool, Optional[str]]:
        return check_held_criteria_r7(new_dir, self.state_dir)

    def detect_affected(self, old_dir: Path, new_dir: Path) -> List[str]:
        affected = detect_affected_r7(old_dir, new_dir)
        self.last_affected = affected
        return affected

    def get_default_affected(self) -> List[str]:
        return ["desktop-pet-backend"]

    def restart_affected(self, affected: List[str]) -> bool:
        if affected:
            log.info("重啟後端：結束現有後端程序以由 supervisor 自動重啟...")
            supervise = "desktop-pet-supervisor" in affected
            stop_backend_process(self.backend_port, supervise=supervise)
        return True

    def restart_after_rollback(self) -> bool:
        if self.last_affected is None:
            st = load_update_state(self.state_dir)
            self.last_affected = st.get("affected")
        if self.last_affected is not None and "desktop-pet-backend" not in self.last_affected:
            log.info("回退完成：本次更新未涉及後端，維持現有後端程序運行")
            return True
        log.info("回退完成，結束後端程序以供自動重啟...")
        stop_backend_process(self.backend_port, supervise=True)
        return True

    def check_normal_boot_health(self) -> Tuple[bool, str]:
        """R7 開機自檢健康檢查：確認 backend/server.py 存在並連線 /health。"""
        server_py = self.install_dir / "backend" / "server.py"
        if not server_py.exists():
            return False, "backend/server.py 不存在"
        start_t = time.time()
        url = f"http://127.0.0.1:{self.backend_port}/health"
        while time.time() - start_t < 30.0:
            body, _ = check_http_health_r7(url, timeout=2.0)
            if body and body.get("ok"):
                return True, "ok"
            time.sleep(1.0)
        return False, f"/health 無回應 ({url})"

    def run_health_check(self, target_commit: str) -> Tuple[bool, Optional[str]]:
        require_commit_match = True
        if self.last_affected is not None and "desktop-pet-backend" not in self.last_affected:
            log.info(
                "本次更新未涉及後端或共用模組 (affected=%s)，健康檢查僅驗證服務存活，略過 commit 比對",
                self.last_affected,
            )
            require_commit_match = False

        return run_health_check_r7(
            host=self.backend_host,
            port=self.backend_port,
            target_commit=target_commit,
            timeout=self.health_timeout,
            install_dir=self.install_dir,
            require_commit_match=require_commit_match,
        )

    def ensure_dependencies(self, staged_dir: Path) -> Tuple[bool, Optional[str]]:
        # 僅在 backend/requirements.txt 或 shared/ 變更時，在 backend/ 目錄跑 pip install
        req_staged = staged_dir / "backend" / "requirements.txt"
        req_backup = self.backup_dir / "backend" / "requirements.txt"

        shared_staged = staged_dir / "shared"
        shared_backup = self.backup_dir / "shared"

        need_pip = False
        if req_staged.exists():
            if not req_backup.exists():
                need_pip = True
            else:
                try:
                    if req_staged.read_bytes() != req_backup.read_bytes():
                        need_pip = True
                except OSError:
                    need_pip = True

        if not need_pip and shared_staged.is_dir():
            if not shared_backup.is_dir():
                need_pip = True
            else:
                from shared.autoupdate import calculate_manifest
                m_staged = calculate_manifest(shared_staged)
                m_backup = calculate_manifest(shared_backup)
                if m_staged != m_backup:
                    need_pip = True

        if not need_pip:
            return True, None

        # 尋找現有 backend/.venv 的 pip
        pip_candidates = [
            self.install_dir / "backend" / ".venv" / "Scripts" / "pip.exe",
            self.install_dir / "backend" / ".venv" / "bin" / "pip",
            self.install_dir / ".venv" / "Scripts" / "pip.exe",
            self.install_dir / ".venv" / "bin" / "pip",
        ]
        pip_bin = next((p for p in pip_candidates if p.is_file()), None)
        if not pip_bin:
            log.warning("未找到 backend/.venv pip，使用目前直譯器之 pip")
            pip_cmd = [sys.executable, "-m", "pip"]
        else:
            pip_cmd = [str(pip_bin)]

        be_dir = self.install_dir / "backend"
        req_file = be_dir / "requirements.txt"
        if not req_file.exists():
            return True, None

        log.info("偵測到 requirements.txt 或 shared/ 變更，在 %s 執行 pip install ...", be_dir)
        try:
            res = subprocess.run(
                pip_cmd + ["install", "-r", "requirements.txt"],
                cwd=str(be_dir),
                capture_output=True,
                text=True,
                timeout=300,
            )
            if res.returncode != 0:
                err_msg = res.stderr.strip() or res.stdout.strip()
                log.error("pip 安裝依賴失敗：%s", err_msg)
                return False, f"pip install 失敗：{err_msg}"
        except Exception as exc:
            return False, f"執行 pip install 異常：{exc}"

        return True, None


def main() -> int:
    parser = argparse.ArgumentParser(description="desktop-pet R7 後端自動更新器")
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

    if args.status:
        st = {
            "version": get_release_info(updater.install_dir),
            "autoupdate": get_autoupdate_status(updater.state_dir, target="r7"),
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
