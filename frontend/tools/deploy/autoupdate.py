"""Pi 自動更新器（PROPOSAL.md 16. 自動部署、Issue #13）。

使用共用核心模組 shared.autoupdate，實作 Pi 平台特定邏輯：
- sparse 範圍：frontend/ 與 shared/（不包含 backend/ 與 piwifi/）
- 依賴管理：若 frontend/requirements.txt 或 shared/ 有變更才重跑 pip，絕不刪除或覆蓋 frontend/.venv
- 切換前檢查：compileall、main.py --help、autoupdate.py --self-test
- 服務重啟：透過 polkit 重啟受影響之 systemd unit（desktop-pet、picam 等）
- 健康檢查：unit active、fps ≥ 18、畫面幀數遞增、piscreen 唯讀檢查、後端連線檢查
- 原地備份與覆蓋（In-place Backup & Overwrite）：非 repo 外部檔案與資料夾（packs/、deploy.env 等）全程保護
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
        ProcessLock,
        append_updates_jsonl,
        calculate_manifest,
        check_ci_runs,
        check_http_json,
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
        ProcessLock,
        append_updates_jsonl,
        calculate_manifest,
        check_ci_runs,
        check_http_json,
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

log = logging.getLogger("pet.autoupdate")


def check_manual_steps(
    new_release_dir: Path,
    state_dir: Path,
    target_platform: str = "pi",
) -> Tuple[bool, Optional[str]]:
    """檢查 tools/deploy/manual-steps.txt 是否有尚未套用的手動步驟（PROPOSAL.md 16.7、16.13）。"""
    steps_file = new_release_dir / "frontend" / "tools" / "deploy" / "manual-steps.txt"
    if not steps_file.exists():
        steps_file = new_release_dir / "tools" / "deploy" / "manual-steps.txt"
    if not steps_file.exists():
        return False, None

    applied_file = state_dir / "applied-steps.txt"
    applied_ids: Set[str] = set()
    if applied_file.exists():
        try:
            for line in applied_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    applied_ids.add(line.split()[0])
        except Exception:
            pass

    pending: List[str] = []
    try:
        for line in steps_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 2)
            if len(parts) >= 2:
                step_id, step_target = parts[0], parts[1].lower()
                desc = parts[2] if len(parts) >= 3 else ""
                if step_target in (target_platform.lower(), "all") and step_id not in applied_ids:
                    pending.append(f"{step_id} ({step_target}): {desc}")
    except Exception as exc:
        log.warning("解析手動步驟清單失敗：%s", exc)

    if pending:
        return True, f"存在未執行之手動步驟：{'; '.join(pending)}"
    return False, None


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

        # 2. 檢查 manual-steps.txt
        has_step, reason = check_manual_steps(new_release_dir, state_dir, target_platform)
        if has_step:
            return True, reason

    if not old_release_dir or not old_release_dir.exists():
        return False, None

    old_manifest = calculate_manifest(old_release_dir)
    new_manifest = calculate_manifest(new_release_dir)

    for rel_path, new_sha in new_manifest.items():
        old_sha = old_manifest.get(rel_path)
        if old_sha != new_sha:
            if rel_path.endswith(".service") or rel_path.endswith(".timer"):
                return True, f"偵測到 systemd unit 變更 ({rel_path})，需管理員手動安裝"
            if "polkit" in rel_path or rel_path.endswith(".rules"):
                return True, f"偵測到 polkit 規則變更 ({rel_path})，需手動安裝"
            if "apt" in rel_path:
                return True, f"偵測到 apt 套件依賴變更 ({rel_path})"
            if "piwifi" in rel_path or rel_path.endswith(".rs"):
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

    old_manifest = calculate_manifest(old_dir)
    new_manifest = calculate_manifest(new_dir)

    changed_files: Set[str] = set()
    for p, sha in new_manifest.items():
        if old_manifest.get(p) != sha:
            changed_files.add(p)
    for p in old_manifest:
        if p not in new_manifest:
            changed_files.add(p)

    units: Set[str] = set()
    for f in changed_files:
        if any(f.startswith(prefix) for prefix in ("frontend/main.py", "frontend/pet/", "frontend/config/", "main.py", "pet/", "config/", "shared/")):
            units.add("desktop-pet.service")
        if any(f.startswith(prefix) for prefix in ("frontend/services/picam/", "services/picam/")):
            units.add("picam.service")

    return sorted(units)


def restart_units(units: List[str], systemctl_cmd: str = "systemctl") -> None:
    """呼叫 systemctl 重啟指定 unit。"""
    for u in units:
        log.info("正在重啟服務：%s", u)
        res = subprocess.run([systemctl_cmd, "restart", u], capture_output=True, text=True)
        if res.returncode != 0:
            log.warning("重啟服務 %s 失敗：%s", u, res.stderr.strip())


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
            # 檢查後端大腦連線狀態
            b_info = status_data.get("backend")
            if isinstance(b_info, dict) and b_info.get("online") is False:
                return False, "桌寵尚未連上後端（backend.online=false）"

            fps = float(status_data.get("fps", 0.0))
            frame_seq = status_data.get("frame_count") or status_data.get("frame_seq")

            if fps >= min_fps:
                if frame_seq is not None:
                    if last_frame_seq is not None and frame_seq > last_frame_seq:
                        seq_increased = True
                    last_frame_seq = frame_seq

                if seq_increased or frame_seq is None:
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
    """開機自檢（PROPOSAL.md 16）。"""
    lock = ProcessLock(state_dir / "autoupdate.lock")
    if not lock.acquire():
        log.info("另一個更新/自檢程序正在執行中，退出。")
        return False, "lock_busy"

    try:
        st_data = load_update_state(state_dir)
        status = st_data.get("status", "idle")
        target_commit = st_data.get("target_commit", "unknown")
        prev_commit = st_data.get("previous_commit", "unknown")

        # 情況 1：覆蓋中途中斷（例如斷電）
        if status in ("updating", "in_progress"):
            log.critical("開機自檢：偵測到更新中途中斷（斷電或異常終止），立即從備份還原！")
            recovered = restore_from_backup(install_dir, backup_dir)
            record_failed_commit(state_dir, target_commit, "boot_check", "更新中途中斷（斷電），開機自檢自動還原")
            write_update_state(state_dir, {
                "status": "rolled_back",
                "target_commit": target_commit,
                "previous_commit": prev_commit,
                "reason": "power_cut_recovered",
                "recovered_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
            restart_units(["desktop-pet.service"], systemctl_cmd=systemctl_cmd)
            return recovered, "power_cut_recovered" if recovered else "power_cut_recovery_failed"

        # 情況 2：更新後第一次開機
        if status == "restarting":
            log.info("開機自檢：偵測到更新後第一次開機，執行新版本驗證...")
            health_ok, health_msg = run_health_check(conf, ["desktop-pet.service"], timeout_sec=60, systemctl_cmd=systemctl_cmd)
            if health_ok:
                log.info("開機自檢：更新後第一次開機驗證成功！")
                write_update_state(state_dir, {
                    "status": "success",
                    "current_commit": target_commit,
                    "previous_commit": prev_commit,
                    "verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                })
                return True, "first_boot_success"
            else:
                log.error("開機自檢：更新後第一次開機驗證失敗（%s），立即從備份還原！", health_msg)
                recovered = restore_from_backup(install_dir, backup_dir)
                record_failed_commit(state_dir, target_commit, "boot_check", f"更新後第一次開機失敗：{health_msg}")
                write_update_state(state_dir, {
                    "status": "rolled_back",
                    "target_commit": target_commit,
                    "previous_commit": prev_commit,
                    "reason": health_msg,
                    "recovered_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                })
                restart_units(["desktop-pet.service"], systemctl_cmd=systemctl_cmd)
                return False, f"first_boot_failed_rolled_back: {health_msg}"

        # 情況 3：正常運行中開機檢查（不動檔案）
        main_py = install_dir / "frontend" / "main.py"
        if not main_py.exists():
            main_py = install_dir / "main.py"
        if not main_py.exists():
            return False, "main.py 不存在"

        # 檢查 /v1/status 或 /status
        st = check_http_json("http://127.0.0.1:8080/v1/status", timeout=2.0)
        if not st:
            st = check_http_json("http://127.0.0.1:8080/status", timeout=2.0)
        if not st:
            return False, "/status 無回應"

        return True, "ok"

    finally:
        lock.release()


class AutoUpdater:
    """Pi 桌寵自動更新器。"""

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
        self.dry_run = dry_run
        self.conf = load_env(env_path, target="pi")
        self.env = self.conf

        if install_dir:
            self.install_dir = install_dir.resolve()
        elif self.conf.get("PET_INSTALL_DIR"):
            self.install_dir = Path(self.conf["PET_INSTALL_DIR"]).resolve()
        else:
            self.install_dir = (Path.home() / "desktop-pet").resolve()

        if backup_dir:
            self.backup_dir = backup_dir.resolve()
        elif self.conf.get("PET_BACKUP_DIR"):
            self.backup_dir = Path(self.conf["PET_BACKUP_DIR"]).resolve()
        else:
            self.backup_dir = (Path.home() / ".local" / "share" / "desktop-pet" / "backup").resolve()

        if state_dir:
            self.state_dir = state_dir.resolve()
        elif self.conf.get("PET_STATE_DIR"):
            self.state_dir = Path(self.conf["PET_STATE_DIR"]).resolve()
        else:
            self.state_dir = (Path.home() / ".local" / "state" / "desktop-pet" / "update").resolve()

        self.repo_url = repo_url or self.conf.get("PET_AUTOUPDATE_REPO", DEFAULT_REPO)
        self.branch = branch or self.conf.get("PET_AUTOUPDATE_BRANCH", DEFAULT_BRANCH)
        self.interval = int(interval or self.conf.get("PET_AUTOUPDATE_INTERVAL", str(DEFAULT_INTERVAL)))
        self.require_ci = bool(require_ci if require_ci is not None else int(self.conf.get("PET_AUTOUPDATE_REQUIRE_CI", "1")))
        self.pin_commit = pin_commit or self.conf.get("PET_AUTOUPDATE_PIN")

        self.health_timeout = float(self.conf.get("PET_AUTOUPDATE_HEALTH_TIMEOUT", str(DEFAULT_HEALTH_TIMEOUT)))
        self.health_min_fps = float(self.conf.get("PET_AUTOUPDATE_HEALTH_MIN_FPS", str(DEFAULT_HEALTH_MIN_FPS)))

        self.lock = ProcessLock(self.state_dir / "autoupdate.lock")
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def boot_check(self) -> Tuple[bool, str]:
        return boot_check(self.conf, self.install_dir, self.state_dir, self.backup_dir)

    def rollback(self) -> bool:
        if not self.lock.acquire():
            return False
        try:
            log.info("手動執行從備份還原...")
            ok = restore_from_backup(self.install_dir, self.backup_dir)
            if ok:
                restart_units(["desktop-pet.service"])
                write_update_state(self.state_dir, {
                    "status": "rolled_back",
                    "manual_rollback_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                })
            return ok
        finally:
            self.lock.release()

    def run_cycle(self, force: bool = False, retry: bool = False) -> int:
        now_ts = time.time()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.install_dir.mkdir(parents=True, exist_ok=True)

        if retry:
            clear_failed_records(self.state_dir)

        # 1. 行程鎖
        if not self.lock.acquire():
            log.info("另一個更新程式正在執行中，本次退出。")
            return 0

        try:
            current_commit = "unknown"
            rel_json = self.install_dir / "RELEASE.json"
            if rel_json.exists():
                try:
                    data = json.loads(rel_json.read_text(encoding="utf-8"))
                    current_commit = data.get("commit", "legacy")
                except Exception:
                    current_commit = "legacy"

            # 2. 暫停與 dry-run 檢查（未通過前絕不動檔案與服務）
            pause_file = self.state_dir / "PAUSE"
            repo_pause = self.install_dir / "PAUSE"
            if pause_file.exists() or repo_pause.exists():
                log.info("PAUSE 檔案存在，自動更新暫停中。")
                self._record_status(current_commit, "paused", "PAUSE 暫停檔存在", "paused")
                return 0

            if self.conf.get("PET_AUTOUPDATE") == "0" or os.environ.get("PET_AUTOUPDATE") == "0":
                log.info("PET_AUTOUPDATE=0，自動更新暫停中。")
                self._record_status(current_commit, "paused", "PET_AUTOUPDATE=0", "paused")
                return 0

            if self.dry_run:
                log.info("[Dry Run] 僅執行檢查，不動任何檔案與服務。")
                st_data = load_update_state(self.state_dir)
                if st_data.get("status") in ("updating", "restarting"):
                    log.info("[Dry Run] 偵測到未完成狀態（%s），dry-run 模式下不修改檔案。", st_data.get("status"))
                return 0

            # 3. 檢查間隔
            status_file = self.state_dir / "status.json"
            if not force and status_file.is_file():
                try:
                    s_data = json.loads(status_file.read_text(encoding="utf-8"))
                    last_check = s_data.get("last_checked_at")
                    if last_check:
                        last_ts = datetime.datetime.fromisoformat(last_check).timestamp()
                        if now_ts - last_ts < self.interval:
                            log.debug("距離上次檢查尚未滿 %d 秒，略過本次檢查", self.interval)
                            return 0
                except Exception:
                    pass

            # 4. 驗證現有檔案是否被修改
            manifest_ok, mod_reason = verify_manifest(self.install_dir)
            if not manifest_ok:
                log.warning("偵測到本地檔案遭修改，自動暫停更新：%s", mod_reason)
                (self.state_dir / "PAUSE").write_text(f"本地檔案遭手動修改: {mod_reason}\n", encoding="utf-8")
                self._record_status(current_commit, "paused", f"本地檔案遭手動修改: {mod_reason}", "paused")
                return 0

            # 5. 決定目標 commit
            target_commit = self.pin_commit
            if target_commit:
                log.info("使用固定版本 (PIN)：%s", target_commit)
            else:
                target_commit = get_remote_commit(self.repo_url, self.branch)
                if not target_commit:
                    err_msg = f"無法從 {self.repo_url} 取得 {self.branch} 之 commit"
                    log.warning(err_msg)
                    self._record_status(current_commit, "on", None, err_msg)
                    return 1

            if current_commit == target_commit and not force:
                log.info("目前已是最新版本 (%s)，無需更新", target_commit[:12])
                self._record_status(current_commit, "on", None, f"已是最新 ({target_commit[:12]})")
                return 0

            # 6. 檢查是否在 failed.json 中
            if not retry:
                failed_records = load_failed_json(self.state_dir)
                for f_entry in failed_records:
                    if f_entry.get("commit") == target_commit:
                        log.info("目標 commit %s 曾部署失敗，依規則略過", target_commit[:12])
                        self._record_status(current_commit, "broken", None, "commit_failed_previously")
                        return 0

            # 7. CI 狀態確認
            if self.require_ci:
                ci_status, ci_reason = check_ci_runs(self.repo_url, target_commit)
                if ci_status != "success":
                    log.warning("目標 commit %s CI 檢查未通過 (%s)：%s", target_commit[:12], ci_status, ci_reason)
                    self._record_status(current_commit, "on", None, f"CI 未通過: {ci_reason}")
                    return 0

            # 8. 下載 tarball 並解壓到暫存區
            staging_dir = self.install_dir / ".staging"
            shutil.rmtree(staging_dir, ignore_errors=True)
            staging_dir.mkdir(parents=True, exist_ok=True)
            tar_path = staging_dir / f"{target_commit}.tar.gz"

            tar_url = f"https://codeload.github.com/{self.repo_url.rstrip('/').split('/')[-2]}/{self.repo_url.rstrip('/').split('/')[-1]}/tar.gz/{target_commit}"
            log.info("正在自 %s 下載程式碼...", tar_url)
            try:
                req = urllib.request.Request(tar_url, headers={"User-Agent": "desktop-pet-autoupdate"})
                with urllib.request.urlopen(req, timeout=60) as resp, tar_path.open("wb") as out:
                    shutil.copyfileobj(resp, out)
            except Exception as exc:
                log.error("下載 tarball 失敗：%s", exc)
                self._record_status(current_commit, "on", None, f"下載失敗: {exc}")
                return 1

            extract_dest = staging_dir / "unpacked"
            try:
                root_extracted = safe_extract_tarball(tar_path, extract_dest)
            except Exception as exc:
                log.error("解壓縮失敗：%s", exc)
                self._record_status(current_commit, "on", None, f"解壓失敗: {exc}")
                return 1

            # 9. 檢查 held 條件
            is_held, held_reason = check_held_criteria(self.install_dir, root_extracted, self.state_dir, target_platform="pi")
            if is_held:
                log.warning("更新包含系統層級變更，標記為 held 並停止自動切換：%s", held_reason)
                self._record_status(current_commit, "held", held_reason, f"需手動安裝: {held_reason}")
                return 0

            # 10. 切換前自我檢查
            check_ok, check_err = pre_switch_check(root_extracted)
            if not check_ok:
                log.error("切換前自我檢查未通過：%s", check_err)
                record_failed_commit(self.state_dir, target_commit, "pre_switch_check", f"切換前檢查失敗: {check_err}")
                self._record_status(current_commit, "on", None, f"切換前檢查失敗: {check_err}")
                return 1

            # 11. 偵測哪些服務受影響
            affected = detect_affected_units(self.install_dir, root_extracted)
            log.info("受本次更新影響需重啟之服務：%s", affected if affected else "無（純文件/資源異動）")

            # 12. 建立現有版本備份
            log.info("正在建立現有版本備份至 %s ...", self.backup_dir)
            create_backup(self.install_dir, self.backup_dir)

            # 標記狀態為 updating
            write_update_state(self.state_dir, {
                "status": "updating",
                "target_commit": target_commit,
                "previous_commit": current_commit,
                "started_at": now_iso,
            })

            # 13. 讀取上一版 MANIFEST，執行覆蓋更新
            prev_manifest_files: Set[str] = set()
            m_file = self.install_dir / "MANIFEST.sha256"
            if m_file.exists():
                try:
                    for line in m_file.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line and not line.startswith("#"):
                            parts = line.split(None, 1)
                            if len(parts) == 2:
                                prev_manifest_files.add(parts[1].strip())
                except Exception as exc:
                    log.warning("讀取現有 MANIFEST.sha256 失敗：%s", exc)

            overwritten, deleted = overwrite_release(
                self.install_dir,
                root_extracted,
                self.backup_dir,
                previous_manifest_files=prev_manifest_files,
                sparse_subdirs={"frontend", "shared"},
            )

            # 產生新版本 MANIFEST.sha256（嚴格僅記錄新版覆蓋之檔案）
            new_manifest = calculate_manifest(self.install_dir, files=overwritten)
            write_manifest(self.install_dir, new_manifest)

            release_meta = {
                "commit": target_commit,
                "short": target_commit[:12],
                "describe": self.branch,
                "deployed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "source": "auto",
            }
            (self.install_dir / "RELEASE.json").write_text(json.dumps(release_meta, indent=2), encoding="utf-8")
            shutil.rmtree(staging_dir, ignore_errors=True)

            # 14. 重啟受影響服務
            if affected:
                log.info("正在重啟受影響服務：%s", affected)
                restart_units(affected)

            # 標記狀態為 restarting
            write_update_state(self.state_dir, {
                "status": "restarting",
                "target_commit": target_commit,
                "previous_commit": current_commit,
            })

            # 15. 執行切換後健康檢查
            log.info("正在執行切換後健康檢查（目標 commit: %s）...", target_commit[:12])
            health_ok, health_msg = run_health_check(self.conf, affected or ["desktop-pet.service"], timeout_sec=self.health_timeout)
            if not health_ok:
                log.error("健康檢查失敗：%s，啟動自動回退流程...", health_msg)
                record_failed_commit(self.state_dir, target_commit, "health_check", f"健康檢查失敗: {health_msg}")
                restore_from_backup(self.install_dir, self.backup_dir)
                restart_units(["desktop-pet.service"])
                write_update_state(self.state_dir, {
                    "status": "rolled_back",
                    "target_commit": target_commit,
                    "previous_commit": current_commit,
                    "reason": health_msg,
                    "recovered_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                })
                self._record_status(current_commit, "broken", None, f"更新失敗已回退: {health_msg}")
                return 1

            # 16. 成功完成
            clear_failed_records(self.state_dir)
            write_update_state(self.state_dir, {
                "status": "success",
                "current_commit": target_commit,
                "previous_commit": current_commit,
                "verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
            self._record_status(target_commit, "on", None, f"更新成功至 {target_commit[:12]}")
            append_updates_jsonl(self.state_dir, {
                "timestamp": now_iso,
                "target": "pi",
                "from_commit": current_commit,
                "to_commit": target_commit,
                "affected_services": affected,
                "status": "success",
            })
            log.info("自動更新成功完成！目前版本：%s", target_commit[:12])
            return 0

        finally:
            self.lock.release()

    def _record_status(
        self,
        current_commit: str,
        status: str,
        pause_reason: Optional[str],
        check_result: Optional[str],
    ) -> None:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        st_file = self.state_dir / "status.json"
        data: Dict[str, Any] = {}
        if st_file.is_file():
            try:
                data = json.loads(st_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        data["status"] = status
        data["last_checked_at"] = now_iso
        data["last_check_result"] = check_result
        if pause_reason is not None:
            data["pause_reason"] = pause_reason
        elif status == "on":
            data["pause_reason"] = None

        data["commit"] = current_commit
        data["short"] = current_commit[:12]
        write_status_json(self.state_dir, data)


def main() -> int:
    parser = argparse.ArgumentParser(description="desktop-pet Pi 自動更新器")
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

    if args.rollback:
        return 0 if updater.rollback() else 1

    if args.boot_check:
        ok, msg = updater.boot_check()
        print(f"Boot check result: {msg}")
        return 0 if ok else 1

    return updater.run_cycle(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
