"""桌寵自動更新核心模組（PROPOSAL.md 16. 自動部署）。

Pi（#13）與 R7（#14）共用的自動更新核心邏輯：
- 以 Git smart HTTP 查詢遠端 main 最新 commit（免 GitHub API 配額限制）。
- 雙重檢查 GitHub Actions CI check-runs（tests 與 secrets-scan 必過）。
- 安全下載與解壓 codeload tarball，嚴格防止目錄穿越與路徑逃逸。
- 原地「備份＋覆蓋」更新流程（In-place Backup & Overwrite）：
  1. 拿鎖並前置檢查 PAUSE / PET_AUTOUPDATE=0 / dry_run。
  2. 原子暫存備份既有檔案，中途出錯舊備份完好如初。
  3. 下載新版只覆蓋清單內檔案；資料／設定檔／動畫包全程不動；新版已刪檔案自目錄移除。
  4. MANIFEST.sha256 僅記錄新版覆蓋之檔案，絕不掃描整個目錄，徹底保護本機非 repo 檔案。
  5. 重啟受影響服務並執行健康檢查，失敗自動自備份還原並記錄至 failed.json。
  6. 開機自檢（boot_check）：偵測更新覆蓋中途中斷或首度開機異常並自動還原。
"""
from __future__ import annotations

import ctypes
import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
import urllib.error
import urllib.request
import uuid

from shared.envfile import parse_env_file

log = logging.getLogger("shared.autoupdate")

DEFAULT_REPO = "https://github.com/laiyuehta/desktop-pet"
DEFAULT_BRANCH = "main"
DEFAULT_INTERVAL = 300  # 預設檢查間隔 300 秒（5 分鐘）
DEFAULT_HEALTH_TIMEOUT = 120
DEFAULT_HEALTH_MIN_FPS = 18

REQUIRED_CI_CHECKS = (
    "tests (Python 3.13)",
    "secrets & personal-info scan",
)

# 永遠不可刪除或覆蓋的保護項目與資料夾
PROTECTED_ENTRIES = {
    ".git",
    ".staging",
    "backup",
    ".venv",
    "deploy.env",
    "config.json",
    "data",
    "logs",
    "secrets",
    "packs",
    "pending-photos",
    "node_modules",
}


class ProcessLock:
    """跨平台核心層級檔案鎖，避免多個更新程序同時執行。"""

    def __init__(self, lock_file: Path) -> None:
        self.lock_file = lock_file
        self._fh: Optional[Any] = None
        self._acquired = False

    def acquire(self) -> bool:
        if self._acquired:
            return True
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            fh = open(self.lock_file, "a+", encoding="utf-8")
            if os.name == "nt":
                import msvcrt
                try:
                    # Windows: 鎖定高位 offset 1000000，使位移 0 的 PID 仍能被安全讀取
                    fh.seek(1000000)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                except (OSError, IOError):
                    fh.close()
                    log.warning("自動更新程式已有其他程序持有檔案鎖，放棄鎖定")
                    return False
            else:
                import fcntl
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError):
                    fh.close()
                    log.warning("自動更新程式已有其他程序持有檔案鎖，放棄鎖定")
                    return False

            fh.seek(0)
            fh.truncate(0)
            fh.write(f"{os.getpid()}\n")
            fh.flush()
            self._fh = fh
            self._acquired = True
            return True
        except Exception as exc:
            log.warning("無法建立或鎖定檔案 %s：%s", self.lock_file, exc)
            return False

    def release(self) -> None:
        if not self._acquired or not self._fh:
            return
        try:
            fh = self._fh
            self._fh = None
            self._acquired = False
            if os.name == "nt":
                import msvcrt
                try:
                    fh.seek(1000000)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            fh.close()
        except Exception as exc:
            log.debug("釋放鎖定檔案失敗：%s", exc)

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


def load_env(
    env_path: Optional[Path] = None,
    target: str = "pi",
    install_dir: Optional[Path] = None,
) -> Dict[str, str]:
    """載入環境設定檔（支援 /etc/desktop-pet/env、deploy.env 或 Windows 環境檔）。"""
    candidates: List[Path] = []
    if env_path:
        candidates.append(Path(env_path))

    if install_dir:
        candidates.append(Path(install_dir) / "deploy.env")
        candidates.append(Path(install_dir) / "env")

    env_sys = os.environ.get("PET_SYS_ENV")
    if env_sys:
        candidates.append(Path(os.path.expanduser(os.path.expandvars(env_sys))))

    if target == "r7" or os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            candidates.append(Path(local_app) / "desktop-pet" / "env")
        candidates.append(Path.home() / ".local" / "share" / "desktop-pet" / "env")
    else:
        candidates.append(Path("/etc/desktop-pet/env"))

    # 根目錄備用 deploy.env
    candidates.append(Path("deploy.env"))
    for cand in candidates:
        if cand.is_file():
            try:
                return parse_env_file(
                    cand,
                    expand=lambda v: os.path.expanduser(os.path.expandvars(v)),
                )
            except Exception as exc:
                log.warning("讀取環境檔 %s 失敗：%s", cand, exc)
    return {}


def parse_pkt_lines(data: bytes) -> List[str]:
    """解析 Git smart HTTP pkt-line 協定資料。"""
    lines: List[str] = []
    idx = 0
    total = len(data)
    while idx < total:
        if idx + 4 > total:
            break
        len_hex = data[idx : idx + 4]
        try:
            pkt_len = int(len_hex, 16)
        except ValueError:
            break
        if pkt_len == 0:
            idx += 4
            continue
        if pkt_len < 4 or idx + pkt_len > total:
            break
        payload = data[idx + 4 : idx + pkt_len]
        try:
            line_str = payload.decode("utf-8", errors="replace")
            lines.append(line_str)
        except Exception:
            pass
        idx += pkt_len
    return lines


def get_remote_commit(repo_url: str, branch: str, timeout: int = 15) -> Optional[str]:
    """透過 Git smart HTTP 查詢遠端分支之最新 commit。免 GitHub API 配額。"""
    clean_repo = repo_url.rstrip("/")
    if clean_repo.endswith(".git"):
        clean_repo = clean_repo[:-4]
    url = f"{clean_repo}.git/info/refs?service=git-upload-pack"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "git/2.43.0", "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except Exception as exc:
        log.warning("查詢 Git 遠端引用失敗：%s (%s)", url, exc)
        return None

    target_ref = f"refs/heads/{branch}"
    for line in parse_pkt_lines(data):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\x00", 1)[0].split()
        if len(parts) >= 2:
            sha, ref_name = parts[0], parts[1]
            if ref_name == target_ref and len(sha) == 40:
                return sha
    return None


def check_ci_runs(
    repo_url: str,
    commit: str,
    required_checks: Tuple[str, ...] = REQUIRED_CI_CHECKS,
    timeout: int = 15,
) -> Tuple[str, Optional[str]]:
    """向 GitHub API 查詢 commit 之 check-runs 狀態，回傳 (status, msg)。

    status 包含：
    - "success": 必要檢查皆已完成且結論為 success
    - "pending": 檢查進行中、未有結果或遭 GitHub 限流
    - "failed": 必要檢查失敗或 API 連線異常
    """
    clean = repo_url.rstrip("/")
    if clean.endswith(".git"):
        clean = clean[:-4]
    parts = clean.split("/")
    if len(parts) < 2:
        return "failed", f"無法解析 repo 擁有者與名稱：{repo_url}"
    owner, repo_name = parts[-2], parts[-1]

    api_url = f"https://api.github.com/repos/{owner}/{repo_name}/commits/{commit}/check-runs"
    req = urllib.request.Request(
        api_url,
        headers={
            "User-Agent": "desktop-pet-autoupdate",
            "Accept": "application/vnd.github.v3+json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            data = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            return "pending", "GitHub API 遭限流 (HTTP 403)，稍後自動重試"
        return "failed", f"GitHub API 回傳 HTTP {exc.code}：{exc.reason}"
    except Exception as exc:
        return "failed", f"連線 GitHub API 失敗：{exc}"

    runs = data.get("check_runs", [])
    if not runs:
        return "pending", "尚未有任何 CI check-runs 結果"

    latest_by_name: Dict[str, Dict[str, Any]] = {}
    # 依 id 升冪排序，確保同名檢查後續覆蓋時取到最新的 check_run
    sorted_runs = sorted(runs, key=lambda x: x.get("id", 0))
    for r in sorted_runs:
        name = r.get("name", "")
        latest_by_name[name] = r

    for required in required_checks:
        r = latest_by_name.get(required)
        if not r:
            return "pending", f"缺少必要檢查：{required}"
        status = r.get("status")
        conclusion = r.get("conclusion")
        if status != "completed":
            return "pending", f"檢查 {required} 進行中 (status={status})"
        if conclusion != "success":
            return "failed", f"檢查 {required} 未通過 (conclusion={conclusion})"

    return "success", None


def is_protected_path(rel: Path) -> bool:
    """檢查路徑是否屬於受保護項目（絕不覆蓋、刪除或還原）。"""
    parts = rel.parts
    if not parts:
        return False
    top = parts[0]
    if top in PROTECTED_ENTRIES or top.endswith(".lock") or top.startswith("packs.moved"):
        return True
    for part in parts:
        if part in PROTECTED_ENTRIES or part.endswith(".lock") or part.startswith("packs.moved"):
            return True
    return False


def safe_extract_tarball(tar_path: Path, extract_to: Path) -> Path:
    """安全解壓縮 tarball，阻擋目錄穿越與連結逃逸攻擊。"""
    extract_to.mkdir(parents=True, exist_ok=True)
    real_dest = extract_to.resolve()

    with tarfile.open(tar_path, "r:*") as tar:
        for member in tar.getmembers():
            target_path = (extract_to / member.name).resolve()
            try:
                target_path.relative_to(real_dest)
            except ValueError:
                raise ValueError(f"安全防護：偵測到危險路徑穿越：{member.name}")

            if member.islnk() or member.issym():
                link_target = Path(member.linkname)
                if link_target.is_absolute():
                    resolved_link = link_target.resolve()
                else:
                    resolved_link = (target_path.parent / link_target).resolve()
                try:
                    resolved_link.relative_to(real_dest)
                except ValueError:
                    raise ValueError(f"安全防護：偵測到指向目標目錄外部的符號連結：{member.name} -> {member.linkname}")

        if hasattr(tarfile, "data_filter"):
            tar.extractall(path=extract_to, filter="data")
        else:
            tar.extractall(path=extract_to)

    top_items = [p for p in extract_to.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if len(top_items) == 1:
        return top_items[0]
    return extract_to


def calculate_manifest(root_dir: Path, files: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """計算指定檔案或非保護檔案之 SHA-256 雜湊表（以相對路徑為 key）。"""
    manifest: Dict[str, str] = {}
    if not root_dir.exists():
        return manifest

    if files is not None:
        for rel_str in sorted(files):
            rel_file = Path(rel_str)
            if is_protected_path(rel_file):
                continue
            if rel_file.as_posix() in ("MANIFEST.sha256", "RELEASE.json"):
                continue
            p = root_dir / rel_file
            if p.is_file():
                try:
                    h = hashlib.sha256()
                    with p.open("rb") as fh:
                        while chunk := fh.read(65536):
                            h.update(chunk)
                    manifest[rel_file.as_posix()] = h.hexdigest()
                except OSError:
                    pass
        return manifest

    for root, dirs, files_in_dir in os.walk(root_dir):
        rel_root = Path(root).relative_to(root_dir)
        if is_protected_path(rel_root):
            dirs[:] = []
            continue
        for f in files_in_dir:
            rel_file = rel_root / f if str(rel_root) != "." else Path(f)
            if is_protected_path(rel_file):
                continue
            if rel_file.as_posix() in ("MANIFEST.sha256", "RELEASE.json"):
                continue
            p = Path(root) / f
            try:
                h = hashlib.sha256()
                with p.open("rb") as fh:
                    while chunk := fh.read(65536):
                        h.update(chunk)
                manifest[rel_file.as_posix()] = h.hexdigest()
            except OSError:
                pass
    return manifest


def write_manifest(root_dir: Path, manifest: Dict[str, str]) -> Path:
    out = root_dir / "MANIFEST.sha256"
    lines = [f"{sha}  {path}" for path, sha in sorted(manifest.items())]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def verify_manifest(root_dir: Path) -> Tuple[bool, Optional[str]]:
    """比對現有檔案與 MANIFEST.sha256，若有手動修改則回傳 False 與原因。"""
    manifest_file = root_dir / "MANIFEST.sha256"
    if not manifest_file.exists():
        return True, None
    expected: Dict[str, str] = {}
    try:
        for line in manifest_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                expected[parts[1].strip()] = parts[0].strip()
    except Exception as exc:
        return False, f"讀取 MANIFEST.sha256 失敗：{exc}"

    if not expected:
        return True, None

    current = calculate_manifest(root_dir, files=expected.keys())
    for path, exp_sha in expected.items():
        curr_sha = current.get(path)
        if curr_sha is None:
            return False, f"檔案遺失：{path}"
        if curr_sha != exp_sha:
            return False, f"檔案已修改：{path} (期望 {exp_sha[:8]}, 實際 {curr_sha[:8]})"
    return True, None


def create_backup(
    install_dir: Path,
    backup_dir: Path,
    files: Optional[Iterable[str]] = None,
    incoming_files: Optional[Iterable[str]] = None,
) -> Path:
    """原子備份現有發布檔案：複製到暫存目錄後替換 backup_dir，確保備份全程完整。"""
    if not install_dir.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        return backup_dir

    backup_tmp = backup_dir.parent / f"{backup_dir.name}.tmp_{uuid.uuid4().hex[:8]}"
    backup_tmp.mkdir(parents=True, exist_ok=True)

    backed_up_files: List[str] = []
    try:
        if files is not None:
            for rel_str in sorted(files):
                rel_file = Path(rel_str)
                if is_protected_path(rel_file):
                    continue
                src = install_dir / rel_file
                if src.is_file():
                    dst = backup_tmp / rel_file
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    backed_up_files.append(rel_file.as_posix())
        else:
            m_file = install_dir / "MANIFEST.sha256"
            tracked_files: Optional[Set[str]] = None
            if m_file.exists():
                tracked_files = set()
                try:
                    for line in m_file.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line and not line.startswith("#"):
                            parts = line.split(None, 1)
                            if len(parts) == 2:
                                tracked_files.add(parts[1].strip())
                except Exception:
                    tracked_files = None

            if tracked_files is not None:
                for rel_str in sorted(tracked_files):
                    rel_file = Path(rel_str)
                    if is_protected_path(rel_file):
                        continue
                    src = install_dir / rel_file
                    if src.is_file():
                        dst = backup_tmp / rel_file
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                        backed_up_files.append(rel_file.as_posix())
            else:
                for root, dirs, file_list in os.walk(install_dir):
                    rel_root = Path(root).relative_to(install_dir)
                    if is_protected_path(rel_root):
                        dirs[:] = []
                        continue
                    for f in file_list:
                        rel_file = rel_root / f if str(rel_root) != "." else Path(f)
                        if is_protected_path(rel_file):
                            continue
                        src = Path(root) / f
                        dst = backup_tmp / rel_file
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)
                        backed_up_files.append(rel_file.as_posix())

        # 備份 RELEASE.json 與 MANIFEST.sha256
        for meta_name in ("RELEASE.json", "MANIFEST.sha256"):
            meta_src = install_dir / meta_name
            if meta_src.is_file():
                shutil.copy2(meta_src, backup_tmp / meta_name)

        # 寫入備份清單（包含 incoming_files，以供異常中斷回退時精準清理新檔案）
        meta: Dict[str, Any] = {
            "files": backed_up_files,
            "backed_up_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if incoming_files is not None:
            meta["incoming_files"] = sorted(incoming_files)

        (backup_tmp / "backup_manifest.json").write_text(
            json.dumps(meta, indent=2),
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(backup_tmp, ignore_errors=True)
        raise

    # 原子替換備份資料夾
    backup_old = backup_dir.parent / f"backup.old_{uuid.uuid4().hex[:8]}"
    if backup_dir.exists():
        backup_dir.rename(backup_old)
    backup_tmp.rename(backup_dir)
    if backup_old.exists():
        shutil.rmtree(backup_old, ignore_errors=True)

    log.info("已成功建立備份至 %s（共 %d 個檔案）", backup_dir, len(backed_up_files))
    return backup_dir


def should_include_in_sparse(rel: Path, sparse_subdirs: Optional[Set[str]]) -> bool:
    """判斷檔案是否符合 sparse 篩選條件。"""
    if not sparse_subdirs:
        return True
    parts = rel.parts
    if not parts:
        return True
    top = parts[0]
    if top in sparse_subdirs:
        return True
    if len(parts) == 1:
        return True
    if top in ("docs", "tools") and parts[1] == "ci":
        return True
    return False


def overwrite_release(
    install_dir: Path,
    unpacked_dir: Path,
    backup_dir: Optional[Path] = None,
    previous_manifest_files: Optional[Set[str]] = None,
    sparse_subdirs: Optional[Set[str]] = None,
    overwritten_collector: Optional[List[str]] = None,
) -> Tuple[List[str], List[str]]:
    """覆蓋新版檔案至 install_dir，並依 previous_manifest_files 移除新版已刪之檔案。"""
    install_dir.mkdir(parents=True, exist_ok=True)
    new_files: Set[str] = set()
    overwritten: List[str] = []

    # 1. 覆蓋新版本檔案
    for root, dirs, files in os.walk(unpacked_dir):
        rel_root = Path(root).relative_to(unpacked_dir)
        if is_protected_path(rel_root):
            dirs[:] = []
            continue
        for f in files:
            rel_file = rel_root / f if str(rel_root) != "." else Path(f)
            if is_protected_path(rel_file):
                continue
            if not should_include_in_sparse(rel_file, sparse_subdirs):
                continue
            new_files.add(rel_file.as_posix())
            src_file = Path(root) / f
            dst_file = install_dir / rel_file
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            overwritten.append(rel_file.as_posix())
            if overwritten_collector is not None:
                overwritten_collector.append(rel_file.as_posix())

    # 2. 移除新版本已刪除的舊檔案（僅依上一版清單 previous_manifest_files 為準）
    deleted: List[str] = []
    if previous_manifest_files is None:
        m_file = install_dir / "MANIFEST.sha256"
        if m_file.exists():
            previous_manifest_files = set()
            try:
                for line in m_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts = line.split(None, 1)
                        if len(parts) == 2:
                            previous_manifest_files.add(parts[1].strip())
            except Exception as exc:
                log.warning("讀取現有 MANIFEST.sha256 失敗：%s", exc)

    if previous_manifest_files:
        for old_rel in sorted(previous_manifest_files):
            if old_rel not in new_files:
                target = install_dir / old_rel
                if target.exists() and not is_protected_path(Path(old_rel)):
                    try:
                        target.unlink()
                        deleted.append(old_rel)
                    except OSError as exc:
                        log.warning("刪除新版本已移除之檔案 %s 失敗：%s", old_rel, exc)

    # 清理空目錄（不碰保護目錄）
    for root, dirs, files in os.walk(install_dir, topdown=False):
        rel_root = Path(root).relative_to(install_dir)
        if str(rel_root) != "." and not is_protected_path(rel_root):
            if not dirs and not files:
                try:
                    Path(root).rmdir()
                except OSError:
                    pass

    log.info("檔案覆蓋完成：更新 %d 個檔案，移除 %d 個舊檔案", len(overwritten), len(deleted))
    return overwritten, deleted


def restore_from_backup(
    install_dir: Path,
    backup_dir: Path,
    newly_overwritten: Optional[Iterable[str]] = None,
) -> bool:
    """從備份還原檔案至 install_dir，並移除新版帶入的多餘檔案，全程保護非 repo 本機檔案。"""
    if not backup_dir.exists():
        log.error("備份目錄 %s 不存在，無法還原！", backup_dir)
        return False

    manifest_file = backup_dir / "backup_manifest.json"
    backup_files: Set[str] = set()
    incoming_files: Set[str] = set()
    if manifest_file.exists():
        try:
            m_data = json.loads(manifest_file.read_text(encoding="utf-8"))
            backup_files = set(m_data.get("files", []))
            if "incoming_files" in m_data:
                incoming_files = set(m_data.get("incoming_files", []))
        except Exception as exc:
            log.warning("讀取備份清單失敗：%s", exc)

    if not backup_files:
        for root, dirs, files in os.walk(backup_dir):
            rel_root = Path(root).relative_to(backup_dir)
            for f in files:
                rel_file = rel_root / f if str(rel_root) != "." else Path(f)
                if rel_file.name in ("backup_manifest.json", "RELEASE.json", "MANIFEST.sha256") or is_protected_path(rel_file):
                    continue
                backup_files.add(rel_file.as_posix())

    # 1. 還原備份中的檔案
    restored_count = 0
    has_copy_error = False
    for root, dirs, files in os.walk(backup_dir):
        rel_root = Path(root).relative_to(backup_dir)
        for f in files:
            rel_file = rel_root / f if str(rel_root) != "." else Path(f)
            if rel_file.name == "backup_manifest.json" or is_protected_path(rel_file):
                continue
            src_file = Path(root) / f
            dst_file = install_dir / rel_file
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src_file, dst_file)
                restored_count += 1
            except OSError as exc:
                log.error("還原檔案 %s 失敗：%s", rel_file, exc)
                has_copy_error = True

    # 2. 清理新版加入但備份中沒有的檔案
    # 候選清單僅包含：新版預計傳入清單 (incoming_files) ∪ 當前覆蓋檔案 (newly_overwritten) ∪ 新版 MANIFEST.sha256
    # 嚴格禁止遍歷掃描整個 install_dir，徹底保護非 repo 本機檔案（如 notes.txt、piwifi/、packs.moved-* 等）。
    candidates: Set[str] = set()
    if newly_overwritten:
        candidates.update(newly_overwritten)
    if incoming_files:
        candidates.update(incoming_files)

    current_manifest = install_dir / "MANIFEST.sha256"
    if current_manifest.exists():
        try:
            for line in current_manifest.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        candidates.add(parts[1].strip())
        except Exception as exc:
            log.warning("讀取現有 MANIFEST.sha256 失敗：%s", exc)

    files_to_remove = candidates - backup_files
    for rel_str in sorted(files_to_remove):
        rel_p = Path(rel_str)
        if is_protected_path(rel_p) or rel_p.name in ("RELEASE.json", "MANIFEST.sha256"):
            continue
        target = install_dir / rel_p
        if target.is_file():
            try:
                target.unlink()
            except OSError as exc:
                log.warning("刪除新版本多餘檔案 %s 失敗：%s", rel_str, exc)

    # 清理空目錄（僅清理曾被新版本影響且已無檔案的目錄，不碰保護目錄）
    for rel_str in sorted(files_to_remove, reverse=True):
        parent_p = (install_dir / rel_str).parent
        while parent_p != install_dir and parent_p.is_dir():
            if is_protected_path(parent_p.relative_to(install_dir)):
                break
            try:
                if not any(parent_p.iterdir()):
                    parent_p.rmdir()
                else:
                    break
            except OSError:
                break
            parent_p = parent_p.parent

    # 3. 還原或移除 RELEASE.json
    if (backup_dir / "RELEASE.json").exists():
        try:
            shutil.copy2(backup_dir / "RELEASE.json", install_dir / "RELEASE.json")
        except OSError:
            pass
    elif (install_dir / "RELEASE.json").exists():
        try:
            (install_dir / "RELEASE.json").unlink()
        except OSError:
            pass

    # 4. 還原或移除 MANIFEST.sha256
    if (backup_dir / "MANIFEST.sha256").exists():
        try:
            shutil.copy2(backup_dir / "MANIFEST.sha256", install_dir / "MANIFEST.sha256")
        except OSError:
            pass
    elif (install_dir / "MANIFEST.sha256").exists():
        try:
            (install_dir / "MANIFEST.sha256").unlink()
        except OSError:
            pass

    if has_copy_error:
        log.error("還原過程中部分檔案複製失敗，標記為還原失敗。")
        return False

    log.info("已從備份還原 %d 個檔案至 %s", restored_count, install_dir)
    return True


def load_update_state(state_dir: Path) -> Dict[str, Any]:
    st_file = state_dir / "update_state.json"
    if st_file.exists():
        try:
            return json.loads(st_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"status": "idle"}


def write_update_state(state_dir: Path, data: Dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    st_file = state_dir / "update_state.json"
    st_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_failed_json(state_dir: Path) -> List[Dict[str, Any]]:
    failed_file = state_dir / "failed.json"
    if failed_file.is_file():
        try:
            data = json.loads(failed_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def record_failed_commit(
    state_dir: Path,
    commit_sha: str,
    stage: str,
    reason: str = "",
) -> None:
    """記錄失敗的 commit 至 failed.json。相容 3 或 4 個參數。"""
    state_dir.mkdir(parents=True, exist_ok=True)
    records = load_failed_json(state_dir)
    records = [r for r in records if r.get("commit") != commit_sha]
    records.append({
        "commit": commit_sha,
        "failed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "step": stage,
        "stage": stage,
        "reason": reason or stage,
    })
    records = records[-10:]
    (state_dir / "failed.json").write_text(json.dumps(records, indent=2), encoding="utf-8")


def clear_failed_records(state_dir: Path) -> None:
    failed_file = state_dir / "failed.json"
    if failed_file.is_file():
        try:
            failed_file.unlink()
        except OSError:
            pass


def write_status_json(state_dir: Path, status_data: Dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "status.json"
    target.write_text(json.dumps(status_data, indent=2), encoding="utf-8")


def append_updates_jsonl(state_dir: Path, entry: Dict[str, Any], max_bytes: int = 1_000_000) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "updates.jsonl"
    try:
        if target.exists() and target.stat().st_size > max_bytes:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            keep_lines = lines[len(lines) // 2 :]
            target.write_text("\n".join(keep_lines) + "\n", encoding="utf-8")
    except Exception:
        pass
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def check_manual_steps(
    new_release_dir: Path,
    state_dir: Path,
    target_platform: str = "pi",
) -> Tuple[bool, Optional[str]]:
    """比對手動部署清單，若有該機器適用且尚未執行的步驟則回傳 (True, 原因)。"""
    steps_file: Optional[Path] = None
    if target_platform.lower() == "r7":
        for cand in [
            new_release_dir / "backend" / "manual-steps.txt",
            new_release_dir / "backend" / "deploy" / "manual-steps.txt",
            new_release_dir / "manual-steps.txt",
        ]:
            if cand.exists():
                steps_file = cand
                break
    else:
        for cand in [
            new_release_dir / "frontend" / "tools" / "deploy" / "manual-steps.txt",
            new_release_dir / "tools" / "deploy" / "manual-steps.txt",
            new_release_dir / "manual-steps.txt",
        ]:
            if cand.exists():
                steps_file = cand
                break

    if not steps_file or not steps_file.exists():
        return False, None

    applied_file = state_dir if state_dir.is_file() else (state_dir / "applied-steps.txt")
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


def check_http_json(url: str, timeout: float = 3.0, headers: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    req_headers = {"User-Agent": "desktop-pet-autoupdate"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return json.loads(data.decode("utf-8"))
    except Exception:
        return None


class AutoUpdaterCore:
    """自動更新核心編排器（Pi 與 R7 共用）。"""

    def __init__(
        self,
        target: str,
        install_dir: Optional[Path] = None,
        backup_dir: Optional[Path] = None,
        state_dir: Optional[Path] = None,
        repo_url: Optional[str] = None,
        branch: Optional[str] = None,
        interval: Optional[int] = None,
        require_ci: Optional[bool] = None,
        pin_commit: Optional[str] = None,
        dry_run: bool = False,
        env_path: Optional[Path] = None,
    ) -> None:
        self.target = target.lower()  # "pi" 或 "r7"
        self.dry_run = dry_run
        self.env_path = env_path
        self.env = load_env(env_path, target=self.target)
        self.conf = self.env  # 相容 conf 屬性

        # 安裝目錄
        if install_dir:
            self.install_dir = install_dir.resolve()
        elif self.env.get("PET_INSTALL_DIR"):
            self.install_dir = Path(self.env["PET_INSTALL_DIR"]).resolve()
        else:
            if self.target == "r7" or os.name == "nt":
                local_app = os.environ.get("LOCALAPPDATA")
                self.install_dir = (Path(local_app) / "desktop-pet" / "repo").resolve() if local_app else (Path.home() / "desktop-pet").resolve()
            else:
                self.install_dir = (Path.home() / "desktop-pet").resolve()

        # 備份目錄
        if backup_dir:
            self.backup_dir = backup_dir.resolve()
        elif self.env.get("PET_BACKUP_DIR"):
            self.backup_dir = Path(self.env["PET_BACKUP_DIR"]).resolve()
        else:
            if self.target == "r7" or os.name == "nt":
                local_app = os.environ.get("LOCALAPPDATA")
                self.backup_dir = (Path(local_app) / "desktop-pet" / "backup").resolve() if local_app else (Path.home() / ".local" / "share" / "desktop-pet" / "backup").resolve()
            else:
                self.backup_dir = (Path.home() / ".local" / "share" / "desktop-pet" / "backup").resolve()

        # 狀態目錄
        if state_dir:
            self.state_dir = state_dir.resolve()
        elif self.env.get("PET_STATE_DIR"):
            self.state_dir = Path(self.env["PET_STATE_DIR"]).resolve()
        else:
            if self.target == "r7" or os.name == "nt":
                local_app = os.environ.get("LOCALAPPDATA")
                self.state_dir = (Path(local_app) / "desktop-pet" / "state" / "update").resolve() if local_app else (Path.home() / ".local" / "state" / "desktop-pet" / "update").resolve()
            else:
                self.state_dir = (Path.home() / ".local" / "state" / "desktop-pet" / "update").resolve()

        self.repo_url = repo_url or self.env.get("PET_AUTOUPDATE_REPO", DEFAULT_REPO)
        self.branch = branch or self.env.get("PET_AUTOUPDATE_BRANCH", DEFAULT_BRANCH)
        self.interval = int(interval or self.env.get("PET_AUTOUPDATE_INTERVAL", DEFAULT_INTERVAL))
        self.require_ci = bool(require_ci if require_ci is not None else int(self.env.get("PET_AUTOUPDATE_REQUIRE_CI", "1")))
        raw_pin = pin_commit or self.env.get("PET_AUTOUPDATE_PIN")
        if raw_pin:
            import re
            if re.match(r"^[0-9a-fA-F]{40}$", str(raw_pin).strip()):
                self.pin_commit = str(raw_pin).strip()
            else:
                log.warning("PET_AUTOUPDATE_PIN (%s) 不是合法的 40 碼十六進位 SHA，忽略 pin 設定", raw_pin)
                self.pin_commit = None
        else:
            self.pin_commit = None

        self.lock = ProcessLock(self.state_dir / "autoupdate.lock")
        self.last_affected: Optional[List[str]] = None
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def is_paused(self) -> Tuple[bool, Optional[str]]:
        pause_file = self.state_dir / "PAUSE"
        if pause_file.exists():
            try:
                txt = pause_file.read_text(encoding="utf-8").strip()
                if txt:
                    return True, f"PAUSE: {txt}"
            except Exception:
                pass
            return True, "PAUSE 暫停檔存在"
        if self.env.get("PET_AUTOUPDATE") == "0" or os.environ.get("PET_AUTOUPDATE") == "0":
            return True, "環境變數 PET_AUTOUPDATE=0"
        return False, None

    # --- 子類別擴充掛鉤 ---
    def pre_switch_check(self, staged_dir: Path) -> Tuple[bool, Optional[str]]:
        return True, None

    def check_held_criteria(self, old_dir: Path, new_dir: Path) -> Tuple[bool, Optional[str]]:
        return False, None

    def detect_affected(self, old_dir: Path, new_dir: Path) -> List[str]:
        return []

    def get_default_affected(self) -> List[str]:
        return []

    def restart_affected(self, affected: List[str]) -> bool:
        return True

    def restart_after_rollback(self) -> bool:
        """回退完成後，重新啟動該平台之服務。"""
        affected = self.get_default_affected()
        if affected:
            return self.restart_affected(affected)
        return True

    def run_health_check(self, target_commit: str) -> Tuple[bool, Optional[str]]:
        return True, None

    def ensure_dependencies(self, staged_dir: Path) -> Tuple[bool, Optional[str]]:
        return True, None

    def rollback(self, newly_overwritten: Optional[Iterable[str]] = None) -> bool:
        """手動或自動執行回退至備份版本。"""
        log.warning("正在執行回退至上一版本...")
        if newly_overwritten is None:
            try:
                st = load_update_state(self.state_dir)
                if "incoming_files" in st:
                    newly_overwritten = st["incoming_files"]
            except Exception:
                pass
        ok = restore_from_backup(self.install_dir, self.backup_dir, newly_overwritten=newly_overwritten)
        if ok:
            write_update_state(self.state_dir, {
                "status": "rolled_back",
                "last_rollback_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
            log.info("回退完成，重新啟動服務...")
            self.restart_after_rollback()
            return True
        log.error("回退失敗！請手動檢查 %s 與備份目錄 %s", self.install_dir, self.backup_dir)
        return False

    def get_remote_commit(self, repo_url: str, branch: str) -> Optional[str]:
        return get_remote_commit(repo_url, branch)

    def check_ci_runs(self, repo_url: str, target_commit: str) -> Tuple[str, str]:
        return check_ci_runs(repo_url, target_commit)

    def check_http_json(self, url: str, timeout: float = 3.0, headers: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
        return check_http_json(url, timeout=timeout, headers=headers)

    def check_normal_boot_health(self) -> Tuple[bool, str]:
        """正常開機健康檢查，支援重試容錯（至多 30 秒）。"""
        main_py = self.install_dir / "frontend" / "main.py"
        if not main_py.exists():
            main_py = self.install_dir / "main.py"
        if not main_py.exists():
            return False, "main.py 不存在"

        start_t = time.time()
        while time.time() - start_t < 30.0:
            st_data = self.check_http_json("http://127.0.0.1:8080/v1/status", timeout=2.0)
            if not st_data:
                st_data = self.check_http_json("http://127.0.0.1:8080/status", timeout=2.0)
            if st_data:
                return True, "ok"
            time.sleep(1.0)
        return False, "/status 無回應"

    def boot_check(self) -> Tuple[bool, str]:
        """開機自檢：檢查覆蓋中途中斷或更新後首度開機異常，自動復原。"""
        if not self.lock.acquire():
            log.warning("開機自檢獲取鎖定失敗，已有其他程序持有鎖")
            return False, "lock_busy"

        try:
            st = load_update_state(self.state_dir)
            status = st.get("status")

            # 1. 優先處理中途中斷：無論是否暫停，若處於 in_progress / updating 均必須先回退復原
            if status in ("in_progress", "updating"):
                log.warning("偵測到前次更新中途中斷（status=%s），執行備份還原...", status)
                if not self.backup_dir.exists():
                    return False, "前次更新中途中斷，但備份目錄不存在，無法還原！"
                recovered = self.rollback()
                record_failed_commit(self.state_dir, st.get("target_commit", "unknown"), "boot_check", "更新中途中斷（斷電），開機自檢自動還原")
                # 若中斷曾自動引發暫停，清理該暫停標記
                pause_f = self.state_dir / "PAUSE"
                if pause_f.exists():
                    try:
                        if "本地檔案遭手動修改" in pause_f.read_text(encoding="utf-8"):
                            pause_f.unlink()
                    except Exception:
                        pass
                return recovered, "power_cut_recovered" if recovered else "power_cut_recovery_failed"

            paused, reason = self.is_paused()
            if paused:
                return True, f"更新已暫停 ({reason})，略過開機自檢還原"
            if self.dry_run:
                return True, "dry-run 模式，開機自檢略過還原"

            # 2. 首度開機自檢
            if st.get("pending_boot_check") or status == "restarting":
                target_commit = st.get("target_commit", "")
                log.info("偵測到更新後首度開機自檢（target_commit: %s）", target_commit)
                health_ok, fail_reason = self.run_health_check(target_commit)
                if not health_ok:
                    log.error("首度開機健康檢查失敗：%s，啟動自動回退...", fail_reason)
                    if not self.backup_dir.exists():
                        return False, f"開機健康檢查失敗 ({fail_reason})，且備份目錄不存在無法還原"
                    record_failed_commit(self.state_dir, target_commit, "boot_check", f"開機自檢失敗: {fail_reason}")
                    self.rollback()
                    st["pending_boot_check"] = False
                    st["status"] = "rolled_back"
                    write_update_state(self.state_dir, st)
                    return False, f"first_boot_failed_rolled_back: {fail_reason}"
                st["pending_boot_check"] = False
                st["status"] = "idle"
                write_update_state(self.state_dir, st)
                return True, "first_boot_success"

            # 3. 正常開機：檢查檔案完整性與服務狀態（含重試容錯）
            return self.check_normal_boot_health()
        finally:
            self.lock.release()

    def run_cycle(self, force: bool = False, retry: bool = False) -> int:
        """執行一次自動更新生命週期。"""
        now_ts = time.time()
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        if retry:
            clear_failed_records(self.state_dir)

        if not self.lock.acquire():
            log.info("已有另一個更新程序在執行中，本次週期跳過")
            return 0

        try:
            # 1. 檢查暫停與 dry-run（注意：手動 PAUSE、PET_AUTOUPDATE=0 或 dry-run 絕不動檔案與服務）
            paused, pause_reason = self.is_paused()
            if paused and "本地檔案遭手動修改" not in (pause_reason or ""):
                log.info("自動更新已暫停：%s", pause_reason)
                self._record_status("paused", pause_reason=pause_reason, check_result=f"已暫停: {pause_reason}")
                return 0

            if self.dry_run:
                log.info("[Dry Run] 僅執行檢查，不動任何檔案與服務。")
                return 0

            # 0. 檢查前次更新是否曾中途中斷（如斷電、kill、crash 導致留在 in_progress / updating）
            st_pre = load_update_state(self.state_dir)
            if st_pre.get("status") in ("in_progress", "updating"):
                log.warning("偵測到前次更新程序異常中斷（status=%s），優先執行回退還原...", st_pre.get("status"))
                if self.backup_dir.exists():
                    self.rollback()
                record_failed_commit(self.state_dir, st_pre.get("target_commit", "unknown"), "interrupted", "更新中途中斷，run_cycle 自動復原")
                pause_f = self.state_dir / "PAUSE"
                if pause_f.exists():
                    try:
                        if "本地檔案遭手動修改" in pause_f.read_text(encoding="utf-8"):
                            pause_f.unlink()
                            log.info("已清理因中途中斷產生的自動 PAUSE 標記")
                    except Exception:
                        pass
                return 0

            # 2. 檢查間隔（給予 30 秒彈性容許 timer 週期微小誤差）
            status_file = self.state_dir / "status.json"
            if not force and status_file.is_file():
                try:
                    s_data = json.loads(status_file.read_text(encoding="utf-8"))
                    last_check = s_data.get("last_checked_at")
                    if last_check:
                        last_ts = datetime.datetime.fromisoformat(last_check).timestamp()
                        if now_ts - last_ts < max(0, self.interval - 30):
                            log.debug("距離上次檢查尚未滿 %d 秒，略過本次檢查", self.interval)
                            return 0
                except Exception:
                    pass

            # 3. 取得目前安裝版本
            current_commit: Optional[str] = None
            release_json = self.install_dir / "RELEASE.json"
            if release_json.is_file():
                try:
                    rel_meta = json.loads(release_json.read_text(encoding="utf-8"))
                    current_commit = rel_meta.get("commit")
                except Exception:
                    pass

            # 4. 驗證現有檔案是否被手動修改
            manifest_ok, mod_reason = verify_manifest(self.install_dir)
            if not manifest_ok:
                log.warning("偵測到本地檔案遭修改，自動暫停更新：%s", mod_reason)
                (self.state_dir / "PAUSE").write_text(f"本地檔案遭手動修改: {mod_reason}\n", encoding="utf-8")
                self._record_status("paused", pause_reason=f"本地檔案遭手動修改: {mod_reason}", check_result="暫停")
                append_updates_jsonl(self.state_dir, {
                    "timestamp": now_iso, "target": self.target, "current_commit": current_commit,
                    "status": "paused", "reason": f"本地檔案遭手動修改: {mod_reason}"
                })
                return 0

            # 5. 決定目標 commit
            target_commit = self.pin_commit
            if target_commit:
                log.info("使用固定版本 (PIN)：%s", target_commit)
            else:
                target_commit = self.get_remote_commit(self.repo_url, self.branch)
                if not target_commit:
                    err_msg = f"無法從 {self.repo_url} 取得 {self.branch} 之 commit"
                    log.warning(err_msg)
                    self._record_status("on", check_result=err_msg)
                    append_updates_jsonl(self.state_dir, {
                        "timestamp": now_iso, "target": self.target, "current_commit": current_commit,
                        "status": "failed", "reason": err_msg
                    })
                    return 1

            if current_commit == target_commit and not force:
                log.info("目前已是最新版本 (%s)，無需更新", target_commit[:12])
                self._record_status("on", current_commit=current_commit, check_result=f"已是最新 ({target_commit[:12]})")
                append_updates_jsonl(self.state_dir, {
                    "timestamp": now_iso, "target": self.target, "current_commit": current_commit,
                    "to_commit": target_commit, "status": "up_to_date"
                })
                return 0

            # 6. 檢查是否在 failed.json 中
            if not retry:
                failed_records = load_failed_json(self.state_dir)
                for f_entry in failed_records:
                    if f_entry.get("commit") == target_commit:
                        log.info("目標 commit %s 曾部署失敗，依規則略過", target_commit[:12])
                        self._record_status("broken", current_commit=current_commit, check_result="commit_failed_previously")
                        append_updates_jsonl(self.state_dir, {
                            "timestamp": now_iso, "target": self.target, "current_commit": current_commit,
                            "to_commit": target_commit, "status": "skipped_failed_commit"
                        })
                        return 0

            # 6.5 檢查是否處於 held 狀態（避免每輪重複下載未改變之 held commit）
            held_record_file = self.state_dir / "held_commit.txt"
            if not force and not retry and held_record_file.exists():
                try:
                    if held_record_file.read_text(encoding="utf-8").strip() == target_commit:
                        log.info("目標 commit %s 已被標記為 held，略過重複下載", target_commit[:12])
                        return 0
                except Exception:
                    pass

            # 7. CI 狀態確認
            if self.require_ci:
                ci_status, ci_reason = self.check_ci_runs(self.repo_url, target_commit)
                if ci_status != "success":
                    log.warning("目標 commit %s CI 檢查未通過 (%s)：%s", target_commit[:12], ci_status, ci_reason)
                    self._record_status("on", current_commit=current_commit, check_result=f"CI 未通過: {ci_reason}")
                    append_updates_jsonl(self.state_dir, {
                        "timestamp": now_iso, "target": self.target, "from_commit": current_commit,
                        "to_commit": target_commit, "status": "ci_failed", "reason": ci_reason
                    })
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
                self._record_status("on", current_commit=current_commit, check_result=f"下載失敗: {exc}")
                append_updates_jsonl(self.state_dir, {
                    "timestamp": now_iso, "target": self.target, "from_commit": current_commit,
                    "to_commit": target_commit, "status": "download_failed", "reason": str(exc)
                })
                return 1

            extract_dest = staging_dir / "unpacked"
            try:
                root_extracted = safe_extract_tarball(tar_path, extract_dest)
            except Exception as exc:
                log.error("解壓縮失敗：%s", exc)
                self._record_status("on", current_commit=current_commit, check_result=f"解壓失敗: {exc}")
                append_updates_jsonl(self.state_dir, {
                    "timestamp": now_iso, "target": self.target, "from_commit": current_commit,
                    "to_commit": target_commit, "status": "extract_failed", "reason": str(exc)
                })
                return 1

            # 9. 檢查 held 條件與手動步驟
            is_held, held_reason = self.check_held_criteria(self.install_dir, root_extracted)
            if is_held:
                log.warning("更新包含系統層級變更，標記為 held 並停止自動切換：%s", held_reason)
                try:
                    (self.state_dir / "held_commit.txt").write_text(target_commit, encoding="utf-8")
                except Exception:
                    pass
                self._record_status("held", current_commit=current_commit, pause_reason=held_reason, check_result=f"需手動安裝: {held_reason}")
                append_updates_jsonl(self.state_dir, {
                    "timestamp": now_iso, "target": self.target, "from_commit": current_commit,
                    "to_commit": target_commit, "status": "held", "reason": held_reason
                })
                return 0

            # 10. 切換前自我檢查
            check_ok, check_err = self.pre_switch_check(root_extracted)
            if not check_ok:
                log.error("切換前自我檢查未通過：%s", check_err)
                record_failed_commit(self.state_dir, target_commit, "pre_switch_check", f"切換前檢查失敗: {check_err}")
                self._record_status("on", current_commit=current_commit, check_result=f"切換前檢查失敗: {check_err}")
                append_updates_jsonl(self.state_dir, {
                    "timestamp": now_iso, "target": self.target, "from_commit": current_commit,
                    "to_commit": target_commit, "status": "pre_switch_failed", "reason": check_err
                })
                return 1

            # dry-run 模式：到此為止不變更正式檔案
            if self.dry_run:
                log.info("[dry-run] 模擬檢查全數通過，不修改正式檔案。目標 commit: %s", target_commit[:12])
                append_updates_jsonl(self.state_dir, {
                    "timestamp": now_iso, "target": self.target, "from_commit": current_commit,
                    "to_commit": target_commit, "status": "dry_run_success"
                })
                shutil.rmtree(staging_dir, ignore_errors=True)
                return 0

            # 11. 偵測哪些服務受影響（比對前先清理 staging 中 compileall 產生的 __pycache__）
            for pycache in root_extracted.rglob("__pycache__"):
                shutil.rmtree(pycache, ignore_errors=True)
            affected = self.detect_affected(self.install_dir, root_extracted)
            self.last_affected = affected
            log.info("受本次更新影響需重啟之服務：%s", affected if affected else "無（純文件/資源異動）")

            # 12. 建立現有版本備份（原子暫存保護，僅備份新版本對應之 sparse 範圍檔案）
            log.info("正在建立現有版本備份至 %s ...", self.backup_dir)
            sparse_subdirs: Optional[Set[str]] = None
            if self.target == "pi":
                sparse_subdirs = {"frontend", "shared"}
            elif self.target == "r7":
                sparse_subdirs = {"backend", "shared"}

            candidate_backup_files: Set[str] = set()
            incoming_release_files: Set[str] = set()
            m_file = self.install_dir / "MANIFEST.sha256"
            if m_file.exists():
                try:
                    for line in m_file.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line and not line.startswith("#"):
                            parts = line.split(None, 1)
                            if len(parts) == 2:
                                candidate_backup_files.add(parts[1].strip())
                except Exception:
                    pass

            for root, dirs, f_list in os.walk(root_extracted):
                rel_root = Path(root).relative_to(root_extracted)
                if is_protected_path(rel_root):
                    dirs[:] = []
                    continue
                for f in f_list:
                    rel_f = rel_root / f if str(rel_root) != "." else Path(f)
                    if is_protected_path(rel_f):
                        continue
                    if should_include_in_sparse(rel_f, sparse_subdirs):
                        candidate_backup_files.add(rel_f.as_posix())
                        incoming_release_files.add(rel_f.as_posix())

            try:
                create_backup(
                    self.install_dir,
                    self.backup_dir,
                    files=candidate_backup_files,
                    incoming_files=incoming_release_files,
                )
            except Exception as exc:
                log.error("建立備份失敗：%s", exc)
                self._record_status("broken", current_commit=current_commit, check_result=f"建立備份失敗: {exc}")
                append_updates_jsonl(self.state_dir, {
                    "timestamp": now_iso, "target": self.target, "from_commit": current_commit,
                    "to_commit": target_commit, "status": "backup_failed", "reason": str(exc)
                })
                shutil.rmtree(staging_dir, ignore_errors=True)
                return 1

            # 標記狀態為 in_progress，並記錄 incoming_files 供中途中斷回退時精準清理
            write_update_state(self.state_dir, {
                "status": "in_progress",
                "target_commit": target_commit,
                "started_at": now_iso,
                "incoming_files": sorted(incoming_release_files),
            })

            overwritten_files: List[str] = []
            try:
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

                # 清理 staging 中可能殘留的 __pycache__，避免被複製進安裝目錄
                for pycache in root_extracted.rglob("__pycache__"):
                    shutil.rmtree(pycache, ignore_errors=True)

                overwritten, deleted = overwrite_release(
                    self.install_dir,
                    root_extracted,
                    self.backup_dir,
                    previous_manifest_files=prev_manifest_files,
                    sparse_subdirs=sparse_subdirs,
                    overwritten_collector=overwritten_files,
                )
                overwritten_files = overwritten

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

                # 14. 依賴安裝與更新（在備份後執行，若失敗可及時回退）
                dep_ok, dep_err = self.ensure_dependencies(root_extracted)
                if not dep_ok:
                    raise RuntimeError(f"依賴更新失敗: {dep_err}")

                # 清理 staging 解壓目錄
                shutil.rmtree(staging_dir, ignore_errors=True)

                # 15. 重啟受影響服務
                if affected:
                    log.info("正在重啟受影響服務：%s", affected)
                    restart_ok = self.restart_affected(affected)
                    if not restart_ok:
                        raise RuntimeError(f"重啟受影響服務失敗: {affected}")

                # 標記狀態為 pending_boot_check
                write_update_state(self.state_dir, {
                    "status": "in_progress",
                    "pending_boot_check": True,
                    "target_commit": target_commit,
                })

                # 16. 執行切換後健康檢查
                log.info("正在執行切換後健康檢查（目標 commit: %s）...", target_commit[:12])
                health_ok, health_err = self.run_health_check(target_commit)
                if not health_ok:
                    raise RuntimeError(f"健康檢查失敗: {health_err}")

            except Exception as exc:
                log.exception("更新執行過程發生異常，立即啟動自動回退：%s", exc)
                record_failed_commit(self.state_dir, target_commit, "update_process", str(exc))
                self.rollback(newly_overwritten=overwritten_files)
                self._record_status("broken", current_commit=current_commit, check_result=f"更新失敗已回退: {exc}")
                append_updates_jsonl(self.state_dir, {
                    "timestamp": now_iso,
                    "target": self.target,
                    "from_commit": current_commit,
                    "to_commit": target_commit,
                    "affected_services": affected,
                    "status": "rolled_back",
                    "error": str(exc),
                })
                shutil.rmtree(staging_dir, ignore_errors=True)
                return 1

            # 17. 成功完成
            clear_failed_records(self.state_dir)
            try:
                (self.state_dir / "held_commit.txt").unlink()
            except OSError:
                pass
            write_update_state(self.state_dir, {
                "status": "success",
                "pending_boot_check": False,
                "current_commit": target_commit,
            })
            self._record_status(
                "on",
                current_commit=target_commit,
                previous_commit=current_commit,
                check_result=f"更新成功至 {target_commit[:12]}",
            )
            append_updates_jsonl(self.state_dir, {
                "timestamp": now_iso,
                "target": self.target,
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
        status: str,
        current_commit: Optional[str] = None,
        previous_commit: Optional[str] = None,
        pause_reason: Optional[str] = None,
        check_result: Optional[str] = None,
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

        if current_commit:
            data["commit"] = current_commit
            data["short"] = current_commit[:12]
        if previous_commit:
            data["previous_commit"] = previous_commit
        write_status_json(self.state_dir, data)
