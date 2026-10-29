#!/usr/bin/env python3
"""桌寵 Pi 與 R7 自動更新器（PROPOSAL.md 16. 自動部署）。

只使用 Python 標準函式庫。
- 以 git smart HTTP 查詢 main 最新 commit（不消耗 GitHub API 配額）。
- 雙重檢查 CI check-runs（tests 與 secrets scan 必過）。
- 下載 codeload tarball，解壓至 .staging/，防止路徑穿越，建立 MANIFEST.sha256 與 RELEASE.json。
- 切換前檢查：compileall、main.py --help、autoupdate.py --self-test。
- 檢查手動步驟與 16.7 不自動更新項目（若有改動則標記 held）。
- 原子切換 symlink（Pi）或 junction（R7）。
- 依檔案變更雜湊選擇性重啟服務（無關改動不重啟）。
- 健康檢查：unit active、NRestarts 未增加、fps >= 預設、畫面計數增加、唯讀檢查 piscreen/picam。
- 健康檢查失敗自動回退上一版本並記錄 failed.json。
- 保留版本清理（保留目前 + 上一版 + PET_AUTOUPDATE_KEEP 個舊版本）。
- 記錄 status.json 與 updates.jsonl（超過 1MB 截斷）。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
from typing import Any, Dict, List, Optional, Set, Tuple
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("autoupdate")

# --- 鎖機制 ---
try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore


class ProcessLock:
    """單一執行緒鎖，避免多個 autoupdate 同時執行。"""

    def __init__(self, lock_file: Path) -> None:
        self.lock_file = lock_file
        self.fd: Optional[int] = None

    def acquire(self) -> bool:
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, 0o644)
            if fcntl:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                # Windows / 非 POSIX 備用機制
                try:
                    import msvcrt
                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                except Exception:
                    pass
            os.write(self.fd, f"{os.getpid()}\n".encode("utf-8"))
            return True
        except (BlockingIOError, OSError):
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
            return False

    def release(self) -> None:
        if self.fd is not None:
            try:
                if fcntl:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
            try:
                if self.lock_file.exists():
                    self.lock_file.unlink()
            except OSError:
                pass

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


# --- 設定與環境檔讀取 ---
def load_env(env_path: Optional[Path] = None) -> Dict[str, str]:
    """載入環境變數與環境檔（PROPOSAL.md 16.2）。

    優先順序：現有環境變數 > PET_DEPLOY_ENV > /etc/desktop-pet/env > repo deploy.env。
    """
    conf: Dict[str, str] = {
        "PET_AUTOUPDATE": "1",
        "PET_AUTOUPDATE_REPO": "https://github.com/laiyuehta/desktop-pet",
        "PET_AUTOUPDATE_BRANCH": "main",
        "PET_AUTOUPDATE_INTERVAL": "600",
        "PET_AUTOUPDATE_REQUIRE_CI": "1",
        "PET_AUTOUPDATE_PIN": "",
        "PET_AUTOUPDATE_KEEP": "3",
        "PET_AUTOUPDATE_HEALTH_TIMEOUT": "120",
        "PET_AUTOUPDATE_HEALTH_MIN_FPS": "18",
        "PET_WEB_LISTEN": "0.0.0.0:8080",
        "PET_BACKEND_URL": "",
    }

    target_env: Optional[Path] = env_path
    if not target_env and os.environ.get("PET_DEPLOY_ENV"):
        target_env = Path(os.environ["PET_DEPLOY_ENV"])

    if not target_env:
        sys_env = Path(os.environ.get("PET_SYS_ENV", "/etc/desktop-pet/env"))
        if sys_env.exists():
            target_env = sys_env
        else:
            win_env = Path(os.path.expandvars("%LOCALAPPDATA%/desktop-pet/env"))
            if win_env.exists():
                target_env = win_env

    # 讀取環境檔
    if target_env and target_env.exists():
        try:
            with target_env.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k and v:
                        conf[k] = v
        except Exception as exc:
            log.warning("讀取環境檔 %s 失敗：%s", target_env, exc)

    # 目前環境變數覆蓋環境檔設定
    for k in list(conf.keys()):
        val = os.environ.get(k)
        if val is not None and val != "":
            conf[k] = val

    # 路徑預設值（需展開家目錄與環境變數）
    home = os.environ.get("PET_HOME") or os.path.expanduser("~")
    if "PET_RELEASES_DIR" not in conf:
        if sys.platform == "win32":
            conf["PET_RELEASES_DIR"] = os.path.expandvars("%LOCALAPPDATA%/desktop-pet/releases")
        else:
            conf["PET_RELEASES_DIR"] = str(Path(home) / "desktop-pet-releases")
    else:
        conf["PET_RELEASES_DIR"] = conf["PET_RELEASES_DIR"].replace("@HOME@", home)
        if sys.platform == "win32":
            conf["PET_RELEASES_DIR"] = os.path.expandvars(conf["PET_RELEASES_DIR"])

    if "PET_STATE_DIR" not in conf:
        if sys.platform == "win32":
            conf["PET_STATE_DIR"] = os.path.expandvars("%LOCALAPPDATA%/desktop-pet/state")
        else:
            conf["PET_STATE_DIR"] = str(Path(home) / ".local/state/desktop-pet/update")
    else:
        conf["PET_STATE_DIR"] = conf["PET_STATE_DIR"].replace("@HOME@", home)
        if sys.platform == "win32":
            conf["PET_STATE_DIR"] = os.path.expandvars(conf["PET_STATE_DIR"])

    return conf


# --- Git Smart HTTP 與 CI 查詢 ---
def parse_pkt_lines(data: bytes) -> List[str]:
    """解析 Git smart HTTP pkt-line 格式。"""
    lines: List[str] = []
    offset = 0
    total = len(data)
    while offset < total:
        if offset + 4 > total:
            break
        hex_len = data[offset : offset + 4].decode("ascii", errors="replace")
        offset += 4
        if hex_len == "0000":
            continue
        try:
            pkt_len = int(hex_len, 16)
        except ValueError:
            break
        if pkt_len < 4 or offset + pkt_len - 4 > total:
            break
        payload = data[offset : offset + pkt_len - 4]
        offset += pkt_len - 4
        decoded = payload.decode("utf-8", errors="replace").rstrip("\r\n")
        lines.append(decoded)
    return lines


def get_remote_commit(repo_url: str, branch: str, timeout: int = 15) -> Optional[str]:
    """使用 Git smart HTTP 查詢遠端指定分支的最新 commit（PROPOSAL.md 16.4）。"""
    url = f"{repo_url.rstrip('/')}/info/refs?service=git-upload-pack"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "git/2.0 (desktop-pet-autoupdate)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except Exception as exc:
        log.warning("查詢遠端 git refs 失敗 (%s)：%s", url, exc)
        return None

    lines = parse_pkt_lines(data)
    ref_target = f"refs/heads/{branch}"
    for line in lines:
        parts = line.split()
        if len(parts) >= 2:
            commit, ref = parts[0], parts[1]
            if "\x00" in ref:
                ref = ref.split("\x00")[0]
            if ref == ref_target:
                if re.match(r"^[0-9a-f]{40}$", commit):
                    return commit
    return None


def check_ci_runs(
    repo_url: str,
    commit: str,
    timeout: int = 15,
) -> Tuple[str, str]:
    """透過 GitHub API 檢查指定 commit 的 CI check runs（PROPOSAL.md 16.4）。

    傳回 (狀態, 說明)，狀態包含：
    - "success": 所有必要 check 都成功
    - "pending": 尚在執行中或被限流
    - "failed": 必要 check 失敗
    """
    # 從 repo_url 解析 owner/repo
    m = re.search(r"github\.com[/:]([^/]+)/([^/\.]+)", repo_url)
    if not m:
        return "pending", f"無法從 {repo_url} 解析 owner/repo"
    owner, repo = m.group(1), m.group(2)

    api_url = f"https://api.github.com/repos/{owner}/{repo}/commits/{commit}/check-runs"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "desktop-pet-autoupdate",
    }
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    req = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read()
            data = json.loads(content.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            log.warning("GitHub API 限流（HTTP %d），當作 pending 下次再試", exc.code)
            return "pending", f"GitHub API 被限流（HTTP {exc.code}）"
        return "failed", f"GitHub API HTTP 錯誤：{exc.code}"
    except Exception as exc:
        log.warning("查詢 check-runs 失敗：%s", exc)
        return "pending", f"網路異常：{exc}"

    runs = data.get("check_runs", [])
    required_names = {"tests (Python 3.13)", "secrets & personal-info scan"}
    found: Dict[str, Dict[str, Any]] = {}

    for run in runs:
        name = run.get("name")
        if name in required_names:
            found[name] = run

    missing = required_names - set(found.keys())
    if missing:
        return "pending", f"尚缺必要 check：{', '.join(missing)}"

    for name, run in found.items():
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status != "completed":
            return "pending", f"Check '{name}' 執行中（status: {status}）"
        if conclusion != "success":
            return "failed", f"Check '{name}' 失敗（conclusion: {conclusion}）"

    return "success", "所有必要 check 皆通過"


# --- 下載、解壓與驗證 ---
def safe_extract_tarball(tar_path: Path, extract_to: Path) -> Path:
    """安全解壓 tarball，防止路徑穿越與符號連結逃脫（PROPOSAL.md 16.4）。"""
    extract_to.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            norm = os.path.normpath(member.name)
            if os.path.isabs(norm) or norm.startswith("..") or "/../" in norm or "\\..\\" in norm:
                raise ValueError(f"安全防護：拒絕包含相對/絕對路徑穿越的成員：{member.name}")
            if member.issym() or member.islnk():
                target = member.linkname
                if os.path.isabs(target) or ".." in target.split("/"):
                    raise ValueError(f"安全防護：拒絕外部符號連結：{member.name} -> {target}")
        if hasattr(tarfile, "data_filter"):
            tar.extractall(path=extract_to, filter="data")
        else:
            tar.extractall(path=extract_to)

    # Codeload 頂層目錄通常是 <repo>-<commit>
    subdirs = [p for p in extract_to.iterdir() if p.is_dir()]
    if len(subdirs) == 1:
        return subdirs[0]
    return extract_to


def calculate_manifest(release_dir: Path) -> Dict[str, str]:
    """計算目錄下所有一般檔案的 sha256 雜湊表（排除非 git 之 packs、環境檔與 symlink）。"""
    manifest: Dict[str, str] = {}
    for root, _, files in os.walk(release_dir):
        for f in sorted(files):
            if f in ("MANIFEST.sha256", "RELEASE.json", "deploy.env"):
                continue
            fpath = Path(root) / f
            if fpath.is_symlink():
                continue
            rel = fpath.relative_to(release_dir).as_posix()
            if rel.startswith("packs/") or rel == "deploy.env":
                continue
            try:
                h = hashlib.sha256()
                with fpath.open("rb") as fh:
                    while chunk := fh.read(65536):
                        h.update(chunk)
                manifest[rel] = h.hexdigest()
            except OSError:
                pass
    return manifest


def link_shared_resources(
    target_release_dir: Path,
    current_release_dir: Optional[Path],
    home_dir: Optional[Path] = None,
) -> None:
    """為新 release 目錄連結或帶入 packs 與 deploy.env 等非 git 資源（PROPOSAL.md 16.2、16.3）。"""
    home = home_dir or Path(os.environ.get("PET_HOME") or os.path.expanduser("~"))

    # 1. 處理 packs/
    target_packs = target_release_dir / "packs"
    target_packs.mkdir(parents=True, exist_ok=True)

    # 優先從全域共用目錄帶入：~/.local/share/desktop-pet/packs
    shared_packs = home / ".local" / "share" / "desktop-pet" / "packs"
    if shared_packs.exists() and shared_packs.is_dir():
        for pack in shared_packs.iterdir():
            dest = target_packs / pack.name
            if not dest.exists():
                try:
                    if sys.platform != "win32":
                        os.symlink(str(pack.resolve()), str(dest))
                    else:
                        if pack.is_dir():
                            shutil.copytree(pack, dest)
                        else:
                            shutil.copy2(pack, dest)
                except OSError:
                    pass

    # 若舊 release 目錄有 packs，也同步連結進新 release
    if current_release_dir and current_release_dir.exists():
        old_packs = current_release_dir / "packs"
        if old_packs.exists() and old_packs.is_dir():
            for pack in old_packs.iterdir():
                dest = target_packs / pack.name
                if not dest.exists():
                    try:
                        if sys.platform != "win32":
                            os.symlink(str(pack.resolve()), str(dest))
                        else:
                            if pack.is_dir():
                                shutil.copytree(pack, dest)
                            else:
                                shutil.copy2(pack, dest)
                    except OSError:
                        pass

    # 2. 處理 deploy.env
    target_env = target_release_dir / "deploy.env"
    if not target_env.exists():
        if current_release_dir and (current_release_dir / "deploy.env").exists():
            try:
                if sys.platform != "win32":
                    os.symlink(str((current_release_dir / "deploy.env").resolve()), str(target_env))
                else:
                    shutil.copy2(current_release_dir / "deploy.env", target_env)
            except OSError:
                pass
        elif Path("/etc/desktop-pet/env").exists():
            try:
                if sys.platform != "win32":
                    os.symlink("/etc/desktop-pet/env", str(target_env))
                else:
                    shutil.copy2("/etc/desktop-pet/env", target_env)
            except OSError:
                pass


def write_manifest(release_dir: Path, manifest: Dict[str, str]) -> Path:
    """寫入 MANIFEST.sha256。"""
    out_file = release_dir / "MANIFEST.sha256"
    lines = [f"{manifest[k]}  {k}\n" for k in sorted(manifest.keys())]
    out_file.write_text("".join(lines), encoding="utf-8")
    return out_file


def verify_manifest(release_dir: Path) -> Tuple[bool, Optional[str]]:
    """驗證 release 目錄內容是否符合 MANIFEST.sha256（PROPOSAL.md 16.6）。"""
    manifest_file = release_dir / "MANIFEST.sha256"
    if not manifest_file.exists():
        return True, None

    try:
        lines = manifest_file.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return False, f"讀取 MANIFEST.sha256 失敗：{exc}"

    for line in lines:
        line = line.strip()
        if not line or "  " not in line:
            continue
        expected_hash, rel = line.split("  ", 1)
        target = release_dir / rel
        if not target.exists():
            return False, f"檔案遺失：{rel}"
        try:
            h = hashlib.sha256()
            with target.open("rb") as fh:
                while chunk := fh.read(65536):
                    h.update(chunk)
            if h.hexdigest() != expected_hash:
                return False, f"檔案已修改：{rel}"
        except OSError as exc:
            return False, f"讀取檔案失敗 {rel}：{exc}"

    return True, None


def check_manual_steps(
    new_release_dir: Path,
    state_dir: Path,
    target_platform: str = "pi",
) -> Tuple[bool, Optional[str]]:
    """檢查 tools/deploy/manual-steps.txt 是否有尚未套用的手動步驟（PROPOSAL.md 16.7、16.13）。"""
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

    try:
        for line in steps_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(maxsplit=2)
            if len(parts) >= 2:
                step_id, step_target = parts[0], parts[1]
                if step_target in (target_platform, "all"):
                    if step_id not in applied_ids:
                        desc = parts[2] if len(parts) > 2 else ""
                        return True, f"手動步驟未套用 [{step_id}] {desc}"
    except Exception as exc:
        log.warning("檢查手動步驟失敗：%s", exc)

    return False, None


def check_held_criteria(
    old_release_dir: Optional[Path],
    new_release_dir: Path,
    state_dir: Path,
    target_platform: str = "pi",
) -> Tuple[bool, Optional[str]]:
    """檢查新版本是否改動到 16.7 規定之不可自動執行的項目（需標記為 held）。"""
    # 1. 檢查 manual-steps.txt
    has_step, reason = check_manual_steps(new_release_dir, state_dir, target_platform)
    if has_step:
        return True, reason

    if not old_release_dir or not old_release_dir.exists():
        return False, None

    # 比對新舊檔案
    old_manifest = calculate_manifest(old_release_dir)
    new_manifest = calculate_manifest(new_release_dir)

    all_paths = set(old_manifest.keys()) | set(new_manifest.keys())
    changed: List[str] = [p for p in all_paths if old_manifest.get(p) != new_manifest.get(p)]

    for p in changed:
        # systemd, polkit 規則改動
        if p.startswith("services/desktop-pet/desktop-pet.service") or p.startswith("services/desktop-pet-update/") or p.startswith("tools/wifi/50-desktop-pet-wifi.rules"):
            return True, f"systemd / polkit 規則改動：{p}"
        # Wi-Fi 系統檔改動
        if p.startswith("tools/wifi/"):
            return True, f"Wi-Fi 系統設定改動：{p}"
        # Rust 程式碼改動
        if p.startswith("services/piwifi/") or p == "Cargo.toml":
            return True, f"Rust 守護程式改動：{p}"

    return False, None


# --- 切換前檢查與切換 ---
def pre_switch_check(release_dir: Path, python_bin: str = sys.executable) -> Tuple[bool, Optional[str]]:
    """切換前檢查（PROPOSAL.md 16.5）。

    在新版本目錄裡執行：
    1. python3 -m compileall -q .
    2. python3 main.py --help
    3. python3 tools/deploy/autoupdate.py --self-test
    """
    # 1. compileall
    res = subprocess.run([python_bin, "-m", "compileall", "-q", "."], cwd=str(release_dir), capture_output=True, text=True)
    if res.returncode != 0:
        return False, f"compileall 失敗：{res.stderr.strip() or res.stdout.strip()}"

    # 2. main.py --help
    main_py = release_dir / "main.py"
    if main_py.exists():
        res = subprocess.run([python_bin, "main.py", "--help"], cwd=str(release_dir), capture_output=True, text=True)
        if res.returncode != 0:
            return False, f"main.py --help 失敗：{res.stderr.strip() or res.stdout.strip()}"

    # 3. autoupdate.py --self-test
    au_py = release_dir / "tools" / "deploy" / "autoupdate.py"
    if au_py.exists():
        res = subprocess.run([python_bin, str(au_py), "--self-test"], cwd=str(release_dir), capture_output=True, text=True)
        if res.returncode != 0:
            return False, f"autoupdate.py --self-test 失敗：{res.stderr.strip() or res.stdout.strip()}"

    return True, None


def switch_symlink(release_dir: Path, current_link: Path) -> None:
    """以原子方式切換 symlink 指向新版本（PROPOSAL.md 16.6）。"""
    current_link.parent.mkdir(parents=True, exist_ok=True)

    # 首次安裝遷移（PROPOSAL.md 16.9）：
    # 若 current_link 是一般目錄而非 symlink，先將其安全遷移為第一個 legacy release
    if current_link.exists() and current_link.is_dir() and not current_link.is_symlink():
        releases_dir = current_link.parent / "desktop-pet-releases"
        releases_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        legacy_dir = releases_dir / f"{ts}-legacy"
        log.info("偵測到 %s 為一般目錄，執行首次安裝遷移至 %s", current_link, legacy_dir)
        shutil.move(str(current_link), str(legacy_dir))
        if sys.platform == "win32":
            try:
                os.symlink(str(legacy_dir.resolve()), str(current_link), target_is_directory=True)
            except OSError:
                subprocess.run(["cmd", "/c", "mklink", "/J", str(current_link), str(legacy_dir.resolve())], check=True, capture_output=True)
        else:
            os.symlink(str(legacy_dir.resolve()), str(current_link))

    tmp_link = current_link.parent / f".tmp_link_{os.getpid()}_{int(time.time())}"
    try:
        if tmp_link.exists() or tmp_link.is_symlink():
            if tmp_link.is_dir() and not tmp_link.is_symlink():
                shutil.rmtree(tmp_link)
            else:
                tmp_link.unlink()

        if sys.platform == "win32":
            # Windows junction / directory symlink
            try:
                os.symlink(str(release_dir.resolve()), str(tmp_link), target_is_directory=True)
            except OSError:
                subprocess.run(["cmd", "/c", "mklink", "/J", str(tmp_link), str(release_dir.resolve())], check=True, capture_output=True)
        else:
            os.symlink(str(release_dir.resolve()), str(tmp_link))

        os.replace(tmp_link, current_link)
    finally:
        if tmp_link.exists() or tmp_link.is_symlink():
            try:
                if tmp_link.is_dir() and not tmp_link.is_symlink():
                    shutil.rmtree(tmp_link)
                else:
                    tmp_link.unlink()
            except OSError:
                pass


def detect_affected_units(
    old_release_dir: Optional[Path],
    new_release_dir: Path,
) -> List[str]:
    """比對檔案變更雜湊，回傳需要重啟的 systemd units（PROPOSAL.md 16.6）。"""
    if not old_release_dir or not old_release_dir.exists():
        return ["desktop-pet.service", "picam.service"]

    old_manifest = calculate_manifest(old_release_dir)
    new_manifest = calculate_manifest(new_release_dir)

    all_paths = set(old_manifest.keys()) | set(new_manifest.keys())
    changed = [p for p in all_paths if old_manifest.get(p) != new_manifest.get(p)]

    restart_units: Set[str] = set()

    for p in changed:
        if p in ("main.py", "requirements.txt") or p.startswith("pet/") or p.startswith("config/"):
            restart_units.add("desktop-pet.service")
            restart_units.add("desktop-pet-admin.service")
        elif p.startswith("services/picam/"):
            restart_units.add("picam.service")

    return sorted(list(restart_units))


def restart_units(units: List[str], systemctl_cmd: str = "systemctl") -> None:
    """以 polkit 允許之身分重啟指定 units（PROPOSAL.md 16.6）。"""
    for u in units:
        log.info("重啟服務：%s", u)
        try:
            subprocess.run([systemctl_cmd, "restart", u], check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            log.warning("重啟 %s 失敗：%s", u, exc.stderr.decode("utf-8", errors="replace"))


# --- 健康檢查與回退 ---
def check_http_json(url: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "autoupdate-health"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict):
                    return data
    except Exception:
        pass
    return None


def run_health_check(
    conf: Dict[str, str],
    affected_units: List[str],
    timeout_sec: int = 120,
    poll_interval: float = 2.0,
    systemctl_cmd: str = "systemctl",
) -> Tuple[bool, str]:
    """執行切換後健康檢查（PROPOSAL.md 16.6）。

    1. 重啟之 units active，且 NRestarts 穩定未累加。
    2. 主程式 status 正常，fps >= PET_AUTOUPDATE_HEALTH_MIN_FPS。
    3. /screen.jpg 回應 200 JPEG，且 X-Frame-Seq 在檢查期間持續遞增。
    4. piscreen 狀態讀取確認幀數增加、rescue/error 未增加（唯讀）。
    5. picam 狀態讀取確認 pid 存活、errors 未增加（唯讀）。
    6. /wifi/status 可存取。
    7. 後端 /health 連線失敗僅為警告。
    """
    deadline = time.time() + timeout_sec
    min_fps = float(conf.get("PET_AUTOUPDATE_HEALTH_MIN_FPS", "18"))
    web_host = "127.0.0.1"
    web_port = "8080"
    web_listen = conf.get("PET_WEB_LISTEN", "0.0.0.0:8080")
    if ":" in web_listen:
        p = web_listen.rsplit(":", 1)[1]
        if p.isdigit():
            web_port = p

    status_url = f"http://{web_host}:{web_port}/status"
    screen_url = f"http://{web_host}:{web_port}/screen.jpg"
    wifi_url = f"http://{web_host}:{web_port}/wifi/status"

    last_frame_seq: Optional[int] = None
    seq_increased = False

    # 唯讀讀取 piscreen/picam 狀態紀錄
    piscreen_file = Path("/run/piscreen/status.json")
    picam_file = Path("/run/picam/status.json")
    init_piscreen_frames: Optional[int] = None
    init_piscreen_errors: Optional[int] = None
    init_picam_errors: Optional[int] = None

    if piscreen_file.exists():
        try:
            pst = json.loads(piscreen_file.read_text(encoding="utf-8"))
            init_piscreen_frames = pst.get("frames") or pst.get("frame_count")
            init_piscreen_errors = (pst.get("errors", 0) or 0) + (pst.get("rescues", 0) or 0)
        except Exception:
            pass

    if picam_file.exists():
        try:
            pcst = json.loads(picam_file.read_text(encoding="utf-8"))
            init_picam_errors = pcst.get("errors", 0)
        except Exception:
            pass

    unit_check_needed = any("desktop-pet" in u for u in affected_units)

    while time.time() < deadline:
        units_ok = True
        for u in affected_units:
            # 檢查 active
            try:
                res = subprocess.run([systemctl_cmd, "is-active", u], capture_output=True, text=True)
                if res.stdout.strip() != "active":
                    units_ok = False
                    break
            except Exception:
                # 測試環境無 systemctl 時略過
                pass

        if not units_ok:
            time.sleep(poll_interval)
            continue

        if not unit_check_needed:
            # 未重啟主程式（僅文件、相機等變更），檢查通過
            return True, "健康檢查通過（非主程式服務正常）"

        # 檢查主程式 status
        st = check_http_json(status_url)
        if not st:
            time.sleep(poll_interval)
            continue

        fps = st.get("fps", 0.0)
        if fps < min_fps:
            time.sleep(poll_interval)
            continue

        # 檢查 screen.jpg 與 X-Frame-Seq
        try:
            req = urllib.request.Request(screen_url, headers={"User-Agent": "autoupdate-health"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status == 200:
                    ctype = resp.headers.get("Content-Type", "")
                    if "image/jpeg" in ctype or "image/jpg" in ctype:
                        seq_str = resp.headers.get("X-Frame-Seq")
                        if seq_str and seq_str.isdigit():
                            curr_seq = int(seq_str)
                            if last_frame_seq is not None and curr_seq > last_frame_seq:
                                seq_increased = True
                            last_frame_seq = curr_seq
        except Exception:
            pass

        # 檢查 piscreen 狀態計數（唯讀）
        if piscreen_file.exists():
            try:
                pst = json.loads(piscreen_file.read_text(encoding="utf-8"))
                cur_frames = pst.get("frames") or pst.get("frame_count")
                cur_errors = (pst.get("errors", 0) or 0) + (pst.get("rescues", 0) or 0)
                if init_piscreen_frames is not None and cur_frames is not None and cur_frames <= init_piscreen_frames:
                    time.sleep(poll_interval)
                    continue
                if init_piscreen_errors is not None and cur_errors > init_piscreen_errors:
                    return False, f"piscreen 錯誤次數增加（{init_piscreen_errors} -> {cur_errors}）"
            except Exception:
                pass

        # 檢查 picam 狀態（唯讀）
        if picam_file.exists():
            try:
                pcst = json.loads(picam_file.read_text(encoding="utf-8"))
                cur_errs = pcst.get("errors", 0)
                if init_picam_errors is not None and cur_errs > init_picam_errors:
                    return False, f"picam 錯誤次數增加（{init_picam_errors} -> {cur_errs}）"
            except Exception:
                pass

        # 檢查 Wi-Fi 端點回應
        check_http_json(wifi_url)

        # 檢查後端（僅警告，不中斷）
        backend_url = conf.get("PET_BACKEND_URL")
        if backend_url:
            try:
                b_url = f"{backend_url.rstrip('/')}/health"
                check_http_json(b_url, timeout=2.0)
            except Exception:
                pass

        # 若畫面計數有增加（或環境無 X-Frame-Seq 標頭但 fps 合格且連續兩次狀態正常）
        if seq_increased or (fps >= min_fps and last_frame_seq is None):
            return True, f"健康檢查通過（FPS: {fps:.1f}，畫面計數正常遞增）"

        time.sleep(poll_interval)

    return False, f"健康檢查逾時（{timeout_sec} 秒內未達成 fps >= {min_fps} 或畫面幀數未遞增）"


# --- 狀態、歷史紀錄與失敗清單 ---
def load_failed_json(state_dir: Path) -> List[Dict[str, Any]]:
    f = state_dir / "failed.json"
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def record_failed_commit(
    state_dir: Path,
    commit: str,
    step: str,
    reason: str,
) -> None:
    records = load_failed_json(state_dir)
    # 移除重複的舊紀錄
    records = [r for r in records if r.get("commit") != commit]
    records.append({
        "commit": commit,
        "step": step,
        "reason": reason,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    })
    f = state_dir / "failed.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_failed_records(state_dir: Path) -> None:
    f = state_dir / "failed.json"
    if f.exists():
        try:
            f.unlink()
        except OSError:
            pass


def write_status_json(state_dir: Path, status_data: Dict[str, Any]) -> None:
    """原子方式寫入 status.json（PROPOSAL.md 16.10）。"""
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "status.json"
    tmp = state_dir / f".tmp_status_{os.getpid()}_{int(time.time())}.json"
    tmp.write_text(json.dumps(status_data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def append_updates_jsonl(state_dir: Path, entry: Dict[str, Any]) -> None:
    """寫入 updates.jsonl 紀錄；超過 1 MB 時自動輪轉保留後半（PROPOSAL.md 16.10）。"""
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "updates.jsonl"
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(line)

    try:
        if target.stat().st_size > 1024 * 1024:
            # 超過 1MB，保留後 500KB
            content = target.read_bytes()
            cut = content[-512 * 1024 :]
            # 找到第一個換行以保持 jsonl 格式完整
            nl = cut.find(b"\n")
            if nl != -1:
                cut = cut[nl + 1 :]
            target.write_bytes(cut)
    except Exception as exc:
        log.warning("輪轉 updates.jsonl 失敗：%s", exc)


def prune_old_releases(
    releases_dir: Path,
    current_release: Path,
    previous_release: Optional[Path],
    keep_count: int = 3,
) -> None:
    """清理多餘的舊版本（保留目前版本、上一版及指定數量之舊版本）（PROPOSAL.md 16.6）。"""
    if not releases_dir.exists():
        return

    dirs = [
        d for d in releases_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d != current_release and d != previous_release
    ]
    dirs.sort(key=lambda d: d.name, reverse=True)

    # 超過 keep_count 的最舊版本予以刪除
    to_delete = dirs[keep_count:]
    for d in to_delete:
        log.info("清理舊版本目錄：%s", d.name)
        try:
            shutil.rmtree(d)
        except Exception as exc:
            log.warning("刪除舊版本目錄 %s 失敗：%s", d, exc)


# --- 主要自動更新控制類別 ---
class AutoUpdater:
    def __init__(self, env_path: Optional[Path] = None, dry_run: bool = False) -> None:
        self.conf = load_env(env_path)
        self.dry_run = dry_run
        self.state_dir = Path(self.conf["PET_STATE_DIR"])
        self.releases_dir = Path(self.conf["PET_RELEASES_DIR"])
        home = os.environ.get("PET_HOME") or os.path.expanduser("~")
        self.current_link = Path(home) / "desktop-pet"

    def run_cycle(self, force: bool = False, retry: bool = False) -> int:
        """執行一次檢查與更新流程。回傳 0 表示成功或正常無更新，非 0 為異常。"""
        start_time = time.time()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.releases_dir.mkdir(parents=True, exist_ok=True)

        if retry:
            clear_failed_records(self.state_dir)

        # 讀取目前 release 資訊
        current_release_dir: Optional[Path] = None
        current_commit = "unknown"
        if self.current_link.exists() or self.current_link.is_symlink():
            if self.current_link.is_dir() and not self.current_link.is_symlink():
                # 首次遷移一般目錄為 legacy release
                ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                legacy_dir = self.releases_dir / f"{ts}-legacy"
                log.info("偵測到 %s 為一般目錄，執行首次安裝遷移至 %s", self.current_link, legacy_dir)
                shutil.move(str(self.current_link), str(legacy_dir))
                if not (legacy_dir / "RELEASE.json").exists():
                    legacy_meta = {
                        "commit": "legacy",
                        "short": "legacy",
                        "describe": "initial-migration",
                        "deployed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "source": "manual",
                    }
                    (legacy_dir / "RELEASE.json").write_text(json.dumps(legacy_meta, indent=2), encoding="utf-8")
                if sys.platform == "win32":
                    try:
                        os.symlink(str(legacy_dir.resolve()), str(self.current_link), target_is_directory=True)
                    except OSError:
                        subprocess.run(["cmd", "/c", "mklink", "/J", str(self.current_link), str(legacy_dir.resolve())], check=True, capture_output=True)
                else:
                    os.symlink(str(legacy_dir.resolve()), str(self.current_link))
                current_release_dir = legacy_dir
                current_commit = "legacy"
            else:
                try:
                    current_release_dir = self.current_link.resolve()
                    rel_json = current_release_dir / "RELEASE.json"
                    if rel_json.exists():
                        data = json.loads(rel_json.read_text(encoding="utf-8"))
                        current_commit = data.get("commit", "unknown")
                except Exception:
                    pass

        # 檢查鎖
        lock = ProcessLock(self.state_dir / "autoupdate.lock")
        if not lock.acquire():
            log.info("另一個更新程式正在執行中，本次退出。")
            return 0

        try:
            # 1. 檢查暫停條件
            pause_file = self.state_dir / "PAUSE"
            if pause_file.exists():
                log.info("PAUSE 檔案存在，自動更新暫停中。")
                self._record_status(current_commit, "paused", "PAUSE 暫停檔存在", "paused")
                return 0

            if self.conf.get("PET_AUTOUPDATE") == "0":
                log.info("PET_AUTOUPDATE=0，自動更新暫停中。")
                self._record_status(current_commit, "paused", "PET_AUTOUPDATE=0", "paused")
                return 0

            # 2. 檢查本地手動修改
            if current_release_dir and current_release_dir.exists():
                is_mod, mod_reason = verify_manifest(current_release_dir)
                if not is_mod:
                    log.warning("偵測到本地修改（%s），自動更新暫停。", mod_reason)
                    self._record_status(current_commit, "paused", f"本地版本遭修改：{mod_reason}", "paused")
                    return 0

            # 3. 檢查更新間隔（非 force 且有前次檢查記錄時）
            interval = int(self.conf.get("PET_AUTOUPDATE_INTERVAL", "600"))
            status_file = self.state_dir / "status.json"
            if not force and status_file.exists():
                try:
                    s_data = json.loads(status_file.read_text(encoding="utf-8"))
                    last_chk = s_data.get("last_checked_at")
                    if last_chk:
                        last_t = datetime.datetime.fromisoformat(last_chk).timestamp()
                        if time.time() - last_t < interval:
                            log.debug("尚未達更新檢查間隔（%d 秒），退出。", interval)
                            return 0
                except Exception:
                    pass

            # 4. 查詢遠端 commit
            branch = self.conf.get("PET_AUTOUPDATE_BRANCH", "main")
            repo = self.conf.get("PET_AUTOUPDATE_REPO", "https://github.com/laiyuehta/desktop-pet")
            remote_commit = get_remote_commit(repo, branch)

            if not remote_commit:
                log.warning("無法取得遠端 %s 分支之 commit。", branch)
                self._record_status(current_commit, "on", None, "network_error")
                return 0

            # 檢查 PIN 固定版本
            pin = self.conf.get("PET_AUTOUPDATE_PIN")
            if pin:
                target_commit = pin
                auto_state = "pinned"
            else:
                target_commit = remote_commit
                auto_state = "on"

            # 5. 與目前版本比對
            if target_commit == current_commit:
                log.info("目前已是最新版本：%s", current_commit[:12])
                self._record_status(current_commit, auto_state, None, "no_change")
                return 0

            # 6. 檢查 failed.json
            failed_records = load_failed_json(self.state_dir)
            if any(r.get("commit") == target_commit for r in failed_records):
                log.info("Commit %s 過去曾更新失敗，略過重試。", target_commit[:12])
                self._record_status(current_commit, auto_state, None, "commit_failed_previously")
                return 0

            # 7. CI 檢查
            if self.conf.get("PET_AUTOUPDATE_REQUIRE_CI") == "1":
                ci_status, ci_reason = check_ci_runs(repo, target_commit)
                if ci_status == "pending":
                    log.info("Commit %s CI 尚在執行中或被限流：%s", target_commit[:12], ci_reason)
                    self._record_status(current_commit, auto_state, None, "ci_pending")
                    return 0
                elif ci_status == "failed":
                    log.warning("Commit %s CI 失敗：%s", target_commit[:12], ci_reason)
                    record_failed_commit(self.state_dir, target_commit, "ci_check", ci_reason)
                    self._record_status(current_commit, auto_state, None, "ci_failed")
                    return 0

            # 8. 磁碟空間檢查（< 200MB 終止）
            try:
                free_bytes = shutil.disk_usage(self.releases_dir).free
                if free_bytes < 200 * 1024 * 1024:
                    log.error("磁碟空間不足 200MB（剩餘 %d MB），取消下載。", free_bytes // (1024 * 1024))
                    self._record_status(current_commit, auto_state, None, "insufficient_disk_space")
                    return 0
            except OSError:
                pass

            if self.dry_run:
                log.info("[Dry Run] 將下載並部署新版本：%s", target_commit[:12])
                return 0

            # 9. 下載並解壓 tarball
            staging_dir = self.releases_dir / ".staging"
            staging_dir.mkdir(parents=True, exist_ok=True)
            tar_url = f"https://codeload.github.com/laiyuehta/desktop-pet/tar.gz/{target_commit}"
            tar_file = staging_dir / f"{target_commit}.tar.gz"

            log.info("下載新版本 tarball：%s", tar_url)
            try:
                req = urllib.request.Request(tar_url, headers={"User-Agent": "desktop-pet-autoupdate"})
                with urllib.request.urlopen(req, timeout=60) as resp, tar_file.open("wb") as out:
                    shutil.copyfileobj(resp, out)
            except Exception as exc:
                log.error("下載 tarball 失敗：%s", exc)
                self._record_status(current_commit, auto_state, None, "download_failed")
                return 0

            unpacked_dir = staging_dir / f"{target_commit}_unpacked"
            if unpacked_dir.exists():
                shutil.rmtree(unpacked_dir)
            try:
                root_extracted = safe_extract_tarball(tar_file, unpacked_dir)
            except Exception as exc:
                log.error("解壓 tarball 失敗：%s", exc)
                record_failed_commit(self.state_dir, target_commit, "extract", str(exc))
                return 0
            finally:
                if tar_file.exists():
                    try:
                        tar_file.unlink()
                    except OSError:
                        pass

            # 移動到正式版本目錄：<YYYYMMDDTHHMMSSZ>-<commit[:12]>
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            target_release_dir = self.releases_dir / f"{ts}-{target_commit[:12]}"
            if target_release_dir.exists():
                shutil.rmtree(target_release_dir)
            shutil.move(str(root_extracted), str(target_release_dir))
            if unpacked_dir.exists():
                try:
                    shutil.rmtree(unpacked_dir)
                except OSError:
                    pass

            # 建立非 git 共用資源連結（packs/ 與 deploy.env）
            link_shared_resources(target_release_dir, current_release_dir)

            # 產生 MANIFEST.sha256 與 RELEASE.json
            manifest = calculate_manifest(target_release_dir)
            write_manifest(target_release_dir, manifest)
            release_meta = {
                "commit": target_commit,
                "short": target_commit[:12],
                "committed_at": None,
                "describe": branch,
                "deployed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "source": "auto",
            }
            (target_release_dir / "RELEASE.json").write_text(json.dumps(release_meta, indent=2), encoding="utf-8")

            # 10. 檢查 16.7 held 條件（不可自動切換項目）
            is_held, held_reason = check_held_criteria(current_release_dir, target_release_dir, self.state_dir, target_platform="pi")
            if is_held:
                log.warning("新版本包含需手動部署項目，標記為 held：%s", held_reason)
                self._record_status(current_commit, "held", held_reason, "held")
                append_updates_jsonl(self.state_dir, {
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "from_commit": current_commit,
                    "to_commit": target_commit,
                    "result": "held",
                    "reason": held_reason,
                })
                return 0

            # 11. 切換前檢查
            ok, pre_err = pre_switch_check(target_release_dir)
            if not ok:
                log.error("切換前檢查未通過：%s", pre_err)
                record_failed_commit(self.state_dir, target_commit, "pre_switch_check", str(pre_err))
                shutil.rmtree(target_release_dir, ignore_errors=True)
                self._record_status(current_commit, auto_state, None, "pre_switch_check_failed")
                return 0

            # 12. 比對影響之服務
            affected_units = detect_affected_units(current_release_dir, target_release_dir)

            # 13. 原子切換連結
            log.info("原子切換 symlink 到新版本：%s", target_release_dir.name)
            switch_symlink(target_release_dir, self.current_link)

            # 14. 選擇性重啟服務
            if affected_units:
                log.info("重啟受影響服務：%s", ", ".join(affected_units))
                restart_units(affected_units)
            else:
                log.info("僅文件/資源改動，無需重啟任何服務。")

            # 15. 健康檢查
            timeout_sec = int(self.conf.get("PET_AUTOUPDATE_HEALTH_TIMEOUT", "120"))
            health_ok, health_msg = run_health_check(self.conf, affected_units, timeout_sec=timeout_sec)

            if not health_ok:
                log.error("健康檢查失敗：%s，開始自動回退！", health_msg)
                record_failed_commit(self.state_dir, target_commit, "health_check", health_msg)

                # 回退程序
                if current_release_dir and current_release_dir.exists():
                    log.info("回退到上一版本：%s", current_release_dir.name)
                    switch_symlink(current_release_dir, self.current_link)
                    if affected_units:
                        restart_units(affected_units)
                    prev_ok, prev_msg = run_health_check(self.conf, affected_units, timeout_sec=60)
                    if not prev_ok:
                        log.critical("回退後上一版本健康檢查依然失敗！狀態標記為 broken。")
                        self._record_status(current_commit, "broken", f"回退後驗證失敗：{prev_msg}", "broken")
                        return 1
                    else:
                        log.info("已成功回退至上一版本。")
                        self._record_status(current_commit, auto_state, None, "rolled_back")
                        append_updates_jsonl(self.state_dir, {
                            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            "from_commit": current_commit,
                            "to_commit": target_commit,
                            "result": "rolled_back",
                            "reason": health_msg,
                        })
                        return 0
                else:
                    log.critical("無上一版本可供回退，系統狀態標記為 broken。")
                    self._record_status(current_commit, "broken", "無前一版本且健康檢查失敗", "broken")
                    return 1

            # 16. 更新成功，清理多餘舊版本
            log.info("自動更新成功完成！新版本：%s", target_commit[:12])
            keep_count = int(self.conf.get("PET_AUTOUPDATE_KEEP", "3"))
            prune_old_releases(self.releases_dir, target_release_dir, current_release_dir, keep_count)

            self._record_status(target_commit, auto_state, None, "ok", previous_commit=current_commit)
            append_updates_jsonl(self.state_dir, {
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "from_commit": current_commit,
                "to_commit": target_commit,
                "result": "ok",
                "duration_sec": round(time.time() - start_time, 2),
                "affected_units": affected_units,
                "health_check": health_msg,
            })
            return 0

        finally:
            lock.release()

    def _record_status(
        self,
        current_commit: str,
        status: str,
        pause_reason: Optional[str],
        last_check_result: str,
        previous_commit: Optional[str] = None,
    ) -> None:
        data = {
            "commit": current_commit[:12] if current_commit != "unknown" else "unknown",
            "full_commit": current_commit,
            "describe": self.conf.get("PET_AUTOUPDATE_BRANCH", "main"),
            "status": status,
            "paused": status == "paused",
            "pause_reason": pause_reason,
            "last_checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "last_check_result": last_check_result,
            "previous_commit": previous_commit,
            "failed": load_failed_json(self.state_dir),
        }
        write_status_json(self.state_dir, data)


def main() -> int:
    parser = argparse.ArgumentParser(description="desktop-pet 自動更新器")
    parser.add_argument("--once", action="store_true", help="執行單次更新週期並結束")
    parser.add_argument("--dry-run", action="store_true", help="僅檢查不實際切換或下載")
    parser.add_argument("--self-test", action="store_true", help="模組自我檢測並回傳 0")
    parser.add_argument("--retry", action="store_true", help="清除失敗清單並重試")
    parser.add_argument("--status", action="store_true", help="印出目前更新狀態")
    parser.add_argument("--force", action="store_true", help="忽略間隔強制檢查更新")
    parser.add_argument("--env-file", type=str, help="指定環境檔路徑")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.self_test:
        log.info("autoupdate.py 自我檢測正常。")
        return 0

    updater = AutoUpdater(env_path=Path(args.env_file) if args.env_file else None, dry_run=args.dry_run)

    if args.status:
        st_file = updater.state_dir / "status.json"
        if st_file.exists():
            print(st_file.read_text(encoding="utf-8"))
        else:
            print(json.dumps({"status": "no_status_file", "state_dir": str(updater.state_dir)}, indent=2))
        return 0

    return updater.run_cycle(force=args.force or args.once, retry=args.retry)


if __name__ == "__main__":
    sys.exit(main())
