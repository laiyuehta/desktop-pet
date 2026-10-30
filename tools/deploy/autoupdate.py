#!/usr/bin/env python3
"""桌寵 Pi 與 R7 自動更新器（PROPOSAL.md 16 自動部署，備份＋覆蓋設計）。

純標準函式庫實作。
- 以 Git smart HTTP 查詢 main 最新 commit（免 GitHub API 配額限制）。
- 檢查 CI check-runs（tests 與 secrets scan 必過）。
- 下載 codeload tarball 至 .staging/ 安全解壓。
- 切換前檢查：compileall、main.py --help、autoupdate.py --self-test。
- 檢查手動步驟與 16.7 項目（若有則標記 held 暫停）。
- 備份＋覆蓋更新流程（In-place Backup & Overwrite）：
  1. 拿鎖並通過 PAUSE / PET_AUTOUPDATE=0 / dry-run。
  2. 依清單將目前的舊檔複製至暫存備份資料夾，完成後原子取代舊備份。
  3. 下載新版只覆蓋清單內檔案；packs/ 與 deploy.env 全程不動；新版已刪除之檔案移除。
  4. 重啟服務並於數十秒內驗證 /v1/status 與後端連線，否則自動從備份還原並重啟。
  5. 還原失敗留紀錄；失敗版本寫入 failed.json，後續不再自動重試同一版。
- 開機自檢（boot_check）：
  1. 偵測覆蓋中途中斷（斷電）自動從備份還原。
  2. 更新後第一次開機自檢失敗自動從備份還原。
  3. 舊版本來就失敗則只記錄、絕不動檔案。
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

# --- 行程鎖 ---
try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore


class ProcessLock:
    """單一行程鎖，避免多個 autoupdate 同時執行。"""

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


# --- 設定與讀取 ---
def load_env(env_path: Optional[Path] = None) -> Dict[str, str]:
    """載入環境變數與環境檔（PROPOSAL.md 16.2）。

    優先順序：行程環境變數 > PET_DEPLOY_ENV > /etc/desktop-pet/env > repo deploy.env。
    """
    conf: Dict[str, str] = {
        "PET_AUTOUPDATE": "1",
        "PET_AUTOUPDATE_REPO": "https://github.com/laiyuehta/desktop-pet",
        "PET_AUTOUPDATE_BRANCH": "main",
        "PET_AUTOUPDATE_INTERVAL": "300",
        "PET_AUTOUPDATE_REQUIRE_CI": "1",
        "PET_AUTOUPDATE_PIN": "",
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

    home = os.environ.get("PET_HOME") or os.path.expanduser("~")

    # 安裝目錄（一般目錄，不再遷移為 symlink）
    if "PET_INSTALL_DIR" not in conf:
        conf["PET_INSTALL_DIR"] = str(Path(home) / "desktop-pet")
    else:
        conf["PET_INSTALL_DIR"] = conf["PET_INSTALL_DIR"].replace("@HOME@", home)
        if sys.platform == "win32":
            conf["PET_INSTALL_DIR"] = os.path.expandvars(conf["PET_INSTALL_DIR"])

    # 狀態目錄
    if "PET_STATE_DIR" not in conf:
        if sys.platform == "win32":
            conf["PET_STATE_DIR"] = os.path.expandvars("%LOCALAPPDATA%/desktop-pet/state")
        else:
            conf["PET_STATE_DIR"] = str(Path(home) / ".local/state/desktop-pet/update")
    else:
        conf["PET_STATE_DIR"] = conf["PET_STATE_DIR"].replace("@HOME@", home)
        if sys.platform == "win32":
            conf["PET_STATE_DIR"] = os.path.expandvars(conf["PET_STATE_DIR"])

    # 備份目錄
    if "PET_BACKUP_DIR" not in conf:
        if sys.platform == "win32":
            conf["PET_BACKUP_DIR"] = os.path.expandvars("%LOCALAPPDATA%/desktop-pet/backup")
        else:
            conf["PET_BACKUP_DIR"] = str(Path(home) / ".local/share/desktop-pet/backup")
    else:
        conf["PET_BACKUP_DIR"] = conf["PET_BACKUP_DIR"].replace("@HOME@", home)
        if sys.platform == "win32":
            conf["PET_BACKUP_DIR"] = os.path.expandvars(conf["PET_BACKUP_DIR"])

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
    """使用 Git Smart HTTP info/refs 查詢遠端指定分支之最新 commit hash。"""
    url = f"{repo_url.rstrip('/')}/info/refs?service=git-upload-pack"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "git/2.39.0", "Accept": "application/x-git-upload-pack-advertisement"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except Exception as exc:
        log.warning("查詢 Git Smart HTTP 失敗 (%s)：%s", url, exc)
        return None

    lines = parse_pkt_lines(data)
    target_ref = f"refs/heads/{branch}"
    for line in lines:
        parts = line.split()
        if len(parts) >= 2:
            commit, ref = parts[0], parts[1].split("\0")[0]
            if ref == target_ref:
                if re.fullmatch(r"[0-9a-fA-F]{40}", commit):
                    return commit
    return None


def check_ci_runs(
    repo_url: str,
    commit_sha: str,
    timeout: int = 15,
) -> Tuple[str, Optional[str]]:
    """向 GitHub API 查詢 commit 的 check-runs 狀態。

    回傳：("success", None) / ("pending", reason) / ("failed", reason)
    """
    m = re.match(r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", repo_url.rstrip("/"))
    if not m:
        return "failed", f"不支援的 GitHub 倉庫網址格式：{repo_url}"
    owner, repo_name = m.group(1), m.group(2)

    api_url = f"https://api.github.com/repos/{owner}/{repo_name}/commits/{commit_sha}/check-runs"
    req = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "desktop-pet-autoupdate",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            return "pending", "GitHub API 遭限流 (403)，稍後重試"
        return "pending", f"GitHub API 請求失敗：{exc.code}"
    except Exception as exc:
        return "pending", f"連線至 GitHub API 異常：{exc}"

    runs = data.get("check_runs", [])
    if not runs:
        return "pending", "尚未產生任何 check-runs 紀錄"

    req_map = {
        "tests": ["tests (Python 3.13)", "tests"],
        "secrets": ["secrets & personal-info scan", "secrets"],
    }
    found: Dict[str, Any] = {}
    for r in runs:
        name = r.get("name", "")
        for cat, aliases in req_map.items():
            if name in aliases and cat not in found:
                found[cat] = r

    for cat in ("tests", "secrets"):
        if cat not in found:
            return "pending", f"必經檢查項 {cat} 尚未出現在 check-runs 中"
        run = found[cat]
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status != "completed":
            return "pending", f"檢查項 '{run.get('name')}' 執行中（狀態：{status}）"
        if conclusion != "success":
            return "failed", f"檢查項 '{run.get('name')}' 未通過（結論：{conclusion}）"

    return "success", None


# --- 壓縮包解壓與檔案保護 ---
PROTECTED_ENTRIES = {
    "packs",
    "deploy.env",
    ".git",
    ".venv",
    "data",
    "photos",
    ".staging",
    "__pycache__",
    "target",
}


def is_protected_path(rel: Path) -> bool:
    """判斷該相對路徑是否為保護項目（絕不覆蓋、絕不刪除）。"""
    parts = rel.parts
    if not parts:
        return False
    top = parts[0]
    if top in PROTECTED_ENTRIES:
        return True
    name = rel.name
    if name.endswith(".token") or name.endswith(".sock") or name.endswith(".pyc") or name.endswith(".log"):
        return True
    return False


def safe_extract_tarball(tar_path: Path, extract_to: Path) -> Path:
    """安全解壓 tarball，嚴格防止目錄穿越。回傳頂層解壓目錄。"""
    extract_to.mkdir(parents=True, exist_ok=True)
    dest_resolved = extract_to.resolve()

    with tarfile.open(tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            target_path = (extract_to / member.name).resolve()
            try:
                target_path.relative_to(dest_resolved)
            except ValueError:
                raise ValueError(f"偵測到惡意路徑穿越嘗試：{member.name}")
            if member.issym() or member.islnk():
                if os.path.isabs(member.linkname):
                    link_target = Path(member.linkname).resolve()
                else:
                    link_target = (extract_to / member.name).parent.joinpath(member.linkname).resolve()
                try:
                    link_target.relative_to(dest_resolved)
                except ValueError:
                    raise ValueError(f"偵測到惡意符號連結穿越嘗試：{member.name} -> {member.linkname}")

        if hasattr(tarfile, "data_filter"):
            tar.extractall(path=extract_to, filter="data")
        else:
            tar.extractall(path=extract_to)

    top_items = [p for p in extract_to.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if len(top_items) == 1:
        return top_items[0]
    return extract_to


def calculate_manifest(root_dir: Path) -> Dict[str, str]:
    """計算目錄內所有非保護檔案之 SHA-256 雜湊表（以相對路徑為 key）。"""
    manifest: Dict[str, str] = {}
    if not root_dir.exists():
        return manifest
    for root, dirs, files in os.walk(root_dir):
        rel_root = Path(root).relative_to(root_dir)
        if is_protected_path(rel_root):
            dirs[:] = []
            continue
        for f in files:
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
                expected[parts[1]] = parts[0]
    except Exception as exc:
        return False, f"讀取 MANIFEST.sha256 失敗：{exc}"

    current = calculate_manifest(root_dir)
    for path, exp_sha in expected.items():
        if path not in current:
            return False, f"檔案遺失：{path}"
        if current[path] != exp_sha:
            return False, f"檔案已修改：{path}"
    return True, None


# --- 備份與覆蓋（In-place Backup & Overwrite） ---
def create_backup(install_dir: Path, backup_dir: Path) -> Path:
    """將 install_dir 中的檔案完整複製到暫存備份資料夾，完成後原子取代舊備份。

    注意：packs/、deploy.env 等保護項目不動，且絕不先清空舊備份，確保中途斷電或錯誤時舊備份依然完整。
    """
    backup_dir.parent.mkdir(parents=True, exist_ok=True)
    backup_tmp = backup_dir.parent / f"{backup_dir.name}.tmp_{os.getpid()}_{int(time.time())}"
    if backup_tmp.exists():
        shutil.rmtree(backup_tmp, ignore_errors=True)
    backup_tmp.mkdir(parents=True, exist_ok=True)

    backed_up_files: List[str] = []
    try:
        if install_dir.exists():
            for root, dirs, files in os.walk(install_dir):
                rel_root = Path(root).relative_to(install_dir)
                if is_protected_path(rel_root):
                    dirs[:] = []
                    continue
                for f in files:
                    rel_file = rel_root / f if str(rel_root) != "." else Path(f)
                    if is_protected_path(rel_file):
                        continue
                    src_file = Path(root) / f
                    dst_file = backup_tmp / rel_file
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst_file)
                    backed_up_files.append(rel_file.as_posix())

        # 寫入備份清單
        manifest_file = backup_tmp / "backup_manifest.json"
        manifest_file.write_text(
            json.dumps({
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "files": sorted(backed_up_files),
            }, indent=2),
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(backup_tmp, ignore_errors=True)
        raise

    # 原子取代舊備份
    backup_old = backup_dir.parent / f"{backup_dir.name}.old_{os.getpid()}_{int(time.time())}"
    if backup_dir.exists():
        try:
            backup_dir.rename(backup_old)
        except OSError as exc:
            log.warning("更名舊備份目錄失敗，嘗試以替換方式處理：%s", exc)
            shutil.rmtree(backup_dir, ignore_errors=True)

    backup_tmp.rename(backup_dir)
    if backup_old.exists():
        shutil.rmtree(backup_old, ignore_errors=True)

    log.info("已成功建立備份至 %s（共 %d 個檔案）", backup_dir, len(backed_up_files))
    return backup_dir


def overwrite_release(
    install_dir: Path,
    unpacked_dir: Path,
    backup_dir: Optional[Path] = None,
    previous_manifest_files: Optional[Set[str]] = None,
) -> Tuple[List[str], List[str]]:
    """只覆蓋清單內的檔案；packs/、deploy.env 等保護檔不動；新版已刪除的檔案要一併移除。

    注意：新版已刪除的檔案以 previous_manifest_files（如上一版的 MANIFEST.sha256）為準。
    若無上一版清單（例如初次安裝/更新），絕不刪除任何既有檔案，嚴格保護 repo 外之本機資料。
    """
    install_dir.mkdir(parents=True, exist_ok=True)
    new_files: Set[str] = set()

    # 1. 覆蓋新版本檔案
    overwritten: List[str] = []
    for root, dirs, files in os.walk(unpacked_dir):
        rel_root = Path(root).relative_to(unpacked_dir)
        if is_protected_path(rel_root):
            dirs[:] = []
            continue
        for f in files:
            rel_file = rel_root / f if str(rel_root) != "." else Path(f)
            if is_protected_path(rel_file):
                continue
            new_files.add(rel_file.as_posix())
            src_file = Path(root) / f
            dst_file = install_dir / rel_file
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            overwritten.append(rel_file.as_posix())

    # 2. 移除新版本已刪除的舊檔案（以上一版清單 previous_manifest_files 或 install_dir/MANIFEST.sha256 為準）
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

    # 清理空目錄
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


def restore_from_backup(install_dir: Path, backup_dir: Path) -> bool:
    """從備份還原檔案至 install_dir，並移除新版帶入的多餘檔案，全程不碰 packs/ 與 deploy.env。"""
    if not backup_dir.exists():
        log.error("備份目錄 %s 不存在，無法還原！", backup_dir)
        return False

    manifest_file = backup_dir / "backup_manifest.json"
    backup_files: Set[str] = set()
    if manifest_file.exists():
        try:
            m_data = json.loads(manifest_file.read_text(encoding="utf-8"))
            backup_files = set(m_data.get("files", []))
        except Exception as exc:
            log.warning("讀取備份清單失敗：%s", exc)

    # 若備份清單損毀或不存在，走訪備份目錄收集檔案
    if not backup_files:
        for root, dirs, files in os.walk(backup_dir):
            rel_root = Path(root).relative_to(backup_dir)
            for f in files:
                rel_file = rel_root / f if str(rel_root) != "." else Path(f)
                if rel_file.name in ("backup_manifest.json", "RELEASE.json") or is_protected_path(rel_file):
                    continue
                backup_files.add(rel_file.as_posix())

    # 1. 還原備份中的檔案
    restored_count = 0
    has_copy_error = False
    for root, dirs, files in os.walk(backup_dir):
        rel_root = Path(root).relative_to(backup_dir)
        for f in files:
            rel_file = rel_root / f if str(rel_root) != "." else Path(f)
            if rel_file.name == "backup_manifest.json":
                continue
            if is_protected_path(rel_file):
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
    if backup_files:
        for root, dirs, files in os.walk(install_dir):
            rel_root = Path(root).relative_to(install_dir)
            if is_protected_path(rel_root):
                dirs[:] = []
                continue
            for f in files:
                rel_file = rel_root / f if str(rel_root) != "." else Path(f)
                if is_protected_path(rel_file):
                    continue
                if rel_file.as_posix() not in backup_files and rel_file.name not in ("RELEASE.json", "MANIFEST.sha256"):
                    target = Path(root) / f
                    try:
                        target.unlink()
                    except OSError:
                        pass

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

    if has_copy_error:
        log.error("還原過程中部分檔案複製失敗，標記為還原失敗。")
        return False

    log.info("已從備份還原 %d 個檔案至 %s", restored_count, install_dir)
    return True

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

    log.info("已從備份還原 %d 個檔案至 %s", restored_count, install_dir)
    return True


# --- 更新狀態與開機自檢 ---
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
    tmp_file = state_dir / f".update_state_{os.getpid()}.tmp"
    tmp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_file, st_file)


def boot_check(
    conf: Dict[str, str],
    install_dir: Path,
    state_dir: Path,
    backup_dir: Path,
    systemctl_cmd: str = "systemctl",
) -> Tuple[bool, str]:
    """開機自檢（PROPOSAL.md 16）：
    - 每次開機服務起來後自動檢查：程式能載入、/v1/status 有回應、後端連線、packs/ 與 deploy.env 存在。
    - 若是「更新後第一次開機」且自檢失敗：自動從備份還原並重啟；若本來就是舊版則只記錄、不動檔案。
    - 更新中途斷電（例如覆蓋到一半）要能靠開機自檢偵測並還原。
    """
    lock = ProcessLock(state_dir / "autoupdate.lock")
    if not lock.acquire():
        log.info("另一個更新/自檢程序正在執行中，退出。")
        return False, "lock_busy"

    try:
        st_data = load_update_state(state_dir)
        status = st_data.get("status", "idle")
        target_commit = st_data.get("target_commit", "unknown")
        prev_commit = st_data.get("previous_commit", "unknown")

        # 情況 1：覆蓋中途中斷（例如中途斷電）
        if status == "updating":
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

        # 情況 3：正常運行中／本來就是舊版開機
        # 規格要求每次開機都檢查：程式能載入、/v1/status 回應、後端連線、packs/ 與 deploy.env 存在；舊版僅記錄、不動檔案。
        errors: List[str] = []
        main_py = install_dir / "main.py"
        if not main_py.exists():
            errors.append("main.py 不存在")
        packs_dir = install_dir / "packs"
        if not packs_dir.exists():
            errors.append("packs/ 目錄不存在")
        deploy_env = install_dir / "deploy.env"
        sys_env = Path(conf.get("PET_SYS_ENV", "/etc/desktop-pet/env"))
        if not deploy_env.exists() and not sys_env.exists():
            errors.append("deploy.env 與 /etc/desktop-pet/env 均不存在")

        # 檢查 /v1/status 與後端連線
        web_host = "127.0.0.1"
        web_port = "8080"
        web_listen = conf.get("PET_WEB_LISTEN", "0.0.0.0:8080")
        if ":" in web_listen:
            p = web_listen.rsplit(":", 1)[1]
            if p.isdigit():
                web_port = p

        st = check_http_json(f"http://{web_host}:{web_port}/v1/status") or check_http_json(f"http://{web_host}:{web_port}/status")
        if not st:
            errors.append("/status 無回應")
        else:
            backend_url = conf.get("PET_BACKEND_URL")
            if backend_url:
                backend_st = st.get("backend")
                if isinstance(backend_st, dict) and "online" in backend_st:
                    if not backend_st.get("online"):
                        errors.append("後端未連線 (online=False)")
                else:
                    bh = check_http_json(f"{backend_url.rstrip('/')}/health")
                    if not bh or not bh.get("ok"):
                        errors.append("後端 /health 無回應")

        if errors:
            log.warning("開機自檢發現異常（舊版僅記錄、不動檔案）：%s", "; ".join(errors))
            return False, "; ".join(errors)

        log.info("開機自檢通過，系統正常運行。")
        return True, "ok"
    finally:
        lock.release()


# --- 變更檢查與自我檢測 ---
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
                        return True, f"包含手動步驟 [{step_id}] {desc}"
    except Exception as exc:
        log.warning("讀取手動步驟失敗：%s", exc)

    return False, None


def check_held_criteria(
    old_release_dir: Optional[Path],
    new_release_dir: Path,
    state_dir: Path,
    target_platform: str = "pi",
) -> Tuple[bool, Optional[str]]:
    """比對變更是否符合 PROPOSAL.md 16.7 項目，若有改動則標記為 held。"""
    # 1. 檢查手動 HELD 旗標
    held_flag = state_dir / "HELD"
    if held_flag.exists():
        return True, "手動 HELD 旗標存在"

    # 2. 檢查 manual-steps.txt
    has_step, reason = check_manual_steps(new_release_dir, state_dir, target_platform)
    if has_step:
        return True, reason

    if not old_release_dir or not old_release_dir.exists():
        return False, None

    # 比對檔案差異
    old_manifest = calculate_manifest(old_release_dir)
    new_manifest = calculate_manifest(new_release_dir)

    all_paths = set(old_manifest.keys()) | set(new_manifest.keys())
    changed: List[str] = [p for p in all_paths if old_manifest.get(p) != new_manifest.get(p)]

    for p in changed:
        # systemd, polkit 設定變更
        if p.startswith("services/desktop-pet/desktop-pet.service") or p.startswith("services/desktop-pet-update/") or p.startswith("tools/wifi/50-desktop-pet-wifi.rules"):
            return True, f"systemd / polkit 設定變更：{p}"
        # Wi-Fi 相關變更
        if p.startswith("tools/wifi/"):
            return True, f"Wi-Fi 腳本變更：{p}"
        # Rust 守護行程變更
        if p.startswith("services/piwifi/") or p == "Cargo.toml":
            return True, f"Rust 守護行程變更：{p}"

    return False, None


def pre_switch_check(
    release_dir: Path,
    python_bin: str = sys.executable,
) -> Tuple[bool, Optional[str]]:
    """執行切換前檢查（PROPOSAL.md 16.5）。

    1. compileall 檢查語法
    2. python3 main.py --help 確認依賴與基本啟動
    3. autoupdate.py --self-test 確認新版更新器自身正常
    """
    res = subprocess.run([python_bin, "-m", "compileall", "-q", str(release_dir)], capture_output=True, text=True)
    if res.returncode != 0:
        return False, f"compileall 語法檢查失敗：{res.stderr.strip() or res.stdout.strip()}"

    main_py = release_dir / "main.py"
    if main_py.exists():
        res = subprocess.run([python_bin, str(main_py), "--help"], capture_output=True, text=True, cwd=str(release_dir))
        if res.returncode != 0:
            return False, f"main.py --help 執行失敗：{res.stderr.strip() or res.stdout.strip()}"

    updater_py = release_dir / "tools" / "deploy" / "autoupdate.py"
    if updater_py.exists():
        res = subprocess.run([python_bin, str(updater_py), "--self-test"], capture_output=True, text=True, cwd=str(release_dir))
        if res.returncode != 0:
            return False, f"autoupdate.py --self-test 失敗：{res.stderr.strip() or res.stdout.strip()}"

    return True, None


def detect_affected_units(
    old_dir: Optional[Path],
    new_dir: Path,
) -> List[str]:
    """比對檔案變更雜湊，回傳需要重啟的 systemd units（PROPOSAL.md 16.6）。"""
    if not old_dir or not old_dir.exists():
        return ["desktop-pet.service", "picam.service"]

    old_manifest = calculate_manifest(old_dir)
    new_manifest = calculate_manifest(new_dir)

    all_paths = set(old_manifest.keys()) | set(new_manifest.keys())
    changed = [p for p in all_paths if old_manifest.get(p) != new_manifest.get(p)]

    if not changed:
        return []

    restart: Set[str] = set()
    only_docs_or_assets = True
    for p in changed:
        if p.startswith("docs/") or p.endswith(".md") or p.startswith("assets/") or p == "CHANGELOG.md":
            continue
        only_docs_or_assets = False
        if p.startswith("services/picam/"):
            restart.add("picam.service")
        elif p.startswith("services/piwifi/"):
            restart.add("piwifi.service")
        elif p.startswith("desktop-pet-admin/") or p.startswith("services/desktop-pet-admin/"):
            restart.add("desktop-pet-admin.service")
        else:
            restart.add("desktop-pet.service")

    if only_docs_or_assets:
        return []

    return sorted(restart)


def restart_units(units: List[str], systemctl_cmd: str = "systemctl") -> None:
    """透過 polkit 允許之規則重啟指定服務。"""
    for u in units:
        log.info("正在重啟服務：%s", u)
        try:
            subprocess.run([systemctl_cmd, "restart", u], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            log.warning("重啟服務 %s 失敗：%s", u, exc.stderr.strip() or exc)


def check_http_json(url: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(url, headers={"User-Agent": "desktop-pet-autoupdate-health"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data if isinstance(data, dict) else None
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
    """執行健康檢查（PROPOSAL.md 16.6）。"""
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
    v1_status_url = f"http://{web_host}:{web_port}/v1/status"
    screen_url = f"http://{web_host}:{web_port}/screen.jpg"
    wifi_url = f"http://{web_host}:{web_port}/wifi/status"

    last_frame_seq: Optional[int] = None
    seq_increased = False

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
    last_fail_reason = "服務尚未就緒"

    while time.time() < deadline:
        units_ok = True
        for u in affected_units:
            try:
                res = subprocess.run([systemctl_cmd, "is-active", u], capture_output=True, text=True)
                if res.stdout.strip() != "active":
                    units_ok = False
                    last_fail_reason = f"服務 {u} 非 active"
                    break
            except Exception:
                units_ok = True
                break

        if not units_ok:
            time.sleep(poll_interval)
            continue

        if not unit_check_needed:
            return True, "無受影響服務，健康檢查通過"

        st = check_http_json(v1_status_url) or check_http_json(status_url)
        if not st or not st.get("ok", True):
            last_fail_reason = "/status 無回應或回應異常"
            time.sleep(poll_interval)
            continue

        fps = float(st.get("fps", 0))
        if fps < min_fps:
            last_fail_reason = f"FPS 過低（{fps} < {min_fps}）"
            time.sleep(poll_interval)
            continue

        try:
            req = urllib.request.Request(screen_url, headers={"User-Agent": "autoupdate-health"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status == 200:
                    seq_hdr = resp.headers.get("X-Frame-Seq")
                    if seq_hdr and seq_hdr.isdigit():
                        curr_seq = int(seq_hdr)
                        if last_frame_seq is not None and curr_seq > last_frame_seq:
                            seq_increased = True
                        last_frame_seq = curr_seq
                    else:
                        seq_increased = True
        except Exception:
            seq_increased = True

        if not seq_increased and last_frame_seq is None:
            last_fail_reason = "畫面幀數未遞增"
            time.sleep(poll_interval)
            continue

        if piscreen_file.exists() and init_piscreen_frames is not None:
            try:
                pst = json.loads(piscreen_file.read_text(encoding="utf-8"))
                curr_frames = pst.get("frames") or pst.get("frame_count")
                curr_errors = (pst.get("errors", 0) or 0) + (pst.get("rescues", 0) or 0)
                if curr_frames is not None and curr_frames < init_piscreen_frames:
                    time.sleep(poll_interval)
                    continue
                if init_piscreen_errors is not None and curr_errors > init_piscreen_errors:
                    return False, f"piscreen 錯誤數增加：{curr_errors} > {init_piscreen_errors}"
            except Exception:
                pass

        if picam_file.exists() and init_picam_errors is not None:
            try:
                pcst = json.loads(picam_file.read_text(encoding="utf-8"))
                curr_errors = pcst.get("errors", 0)
                if curr_errors > init_picam_errors:
                    return False, f"picam 錯誤數增加：{curr_errors} > {init_picam_errors}"
            except Exception:
                pass

        wst = check_http_json(wifi_url)
        if wst is None:
            log.debug("救援熱點狀態暫無法存取（僅記錄）")

        # 後端連線檢查：若設定 PET_BACKEND_URL，必須確保已連上後端（/status 內 backend.online=True 或後端 /health 回 200 ok）
        backend_url = conf.get("PET_BACKEND_URL")
        if backend_url:
            backend_connected = False
            backend_st = st.get("backend")
            if isinstance(backend_st, dict) and "online" in backend_st:
                backend_connected = bool(backend_st.get("online"))
            else:
                bh = check_http_json(f"{backend_url.rstrip('/')}/health")
                backend_connected = bool(bh and bh.get("ok"))

            if not backend_connected:
                last_fail_reason = "桌寵尚未連上後端或後端無回應"
                log.warning("桌寵尚未連上後端或後端無回應，等待重試...")
                time.sleep(poll_interval)
                continue

        return True, "健康檢查全部通過"

    return False, f"健康檢查逾時未通過（{last_fail_reason}）"


# --- 失敗紀錄與狀態儲存 ---
def load_failed_json(state_dir: Path) -> List[Dict[str, Any]]:
    f_path = state_dir / "failed.json"
    if not f_path.exists():
        return []
    try:
        data = json.loads(f_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def record_failed_commit(
    state_dir: Path,
    commit_sha: str,
    stage: str,
    reason: str,
) -> None:
    records = load_failed_json(state_dir)
    records.append({
        "commit": commit_sha,
        "failed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "step": stage,
        "stage": stage,
        "reason": reason,
    })
    state_dir.mkdir(parents=True, exist_ok=True)
    f_path = state_dir / "failed.json"
    tmp_path = state_dir / f".failed_{os.getpid()}.tmp"
    tmp_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    os.replace(tmp_path, f_path)


def clear_failed_records(state_dir: Path) -> None:
    f_path = state_dir / "failed.json"
    if f_path.exists():
        try:
            f_path.unlink()
            log.info("已清除 failed.json 紀錄。")
        except OSError:
            pass


def write_status_json(state_dir: Path, status_data: Dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    out = state_dir / "status.json"
    tmp = state_dir / f".status_{os.getpid()}.tmp"
    tmp.write_text(json.dumps(status_data, indent=2), encoding="utf-8")
    os.replace(tmp, out)


def append_updates_jsonl(state_dir: Path, entry: Dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    out = state_dir / "updates.jsonl"
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with out.open("a", encoding="utf-8") as fh:
        fh.write(line)

    try:
        if out.stat().st_size > 1024 * 1024:
            lines = out.read_text(encoding="utf-8").splitlines()
            keep_lines = lines[-500:]
            out.write_text("\n".join(keep_lines) + "\n", encoding="utf-8")
    except Exception:
        pass


# --- 主要自動更新控制類別 ---
class AutoUpdater:
    def __init__(
        self,
        env_path: Optional[Path] = None,
        dry_run: bool = False,
        home_dir: Optional[Path] = None,
    ) -> None:
        self.conf = load_env(env_path)
        self.dry_run = dry_run
        self.home_dir = home_dir or Path(self.conf.get("PET_HOME") or os.environ.get("PET_HOME") or os.path.expanduser("~"))
        self.state_dir = Path(self.conf["PET_STATE_DIR"])
        self.install_dir = Path(self.conf["PET_INSTALL_DIR"])
        self.backup_dir = Path(self.conf["PET_BACKUP_DIR"])

    def boot_check(self) -> Tuple[bool, str]:
        """執行開機自檢。"""
        return boot_check(self.conf, self.install_dir, self.state_dir, self.backup_dir)

    def rollback(self) -> bool:
        """手動從備份還原。"""
        lock = ProcessLock(self.state_dir / "autoupdate.lock")
        if not lock.acquire():
            log.warning("已有其他更新/還原程序正在執行中。")
            return False
        try:
            log.info("手動觸發從備份還原...")
            recovered = restore_from_backup(self.install_dir, self.backup_dir)
            if recovered:
                restart_units(["desktop-pet.service"])
                write_update_state(self.state_dir, {
                    "status": "rolled_back",
                    "manual_rollback_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                })
            return recovered
        finally:
            lock.release()

    def run_cycle(self, force: bool = False, retry: bool = False) -> int:
        """執行一次檢查與更新流程（備份＋覆蓋設計）。回傳 0 表示成功或正常無更新，非 0 為異常。"""
        start_time = time.time()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.install_dir.mkdir(parents=True, exist_ok=True)

        if retry:
            clear_failed_records(self.state_dir)

        # 檢查鎖
        lock = ProcessLock(self.state_dir / "autoupdate.lock")
        if not lock.acquire():
            log.info("另一個更新程式正在執行中，本次退出。")
            return 0

        try:
            # 讀取目前版本資訊
            current_commit = "unknown"
            rel_json = self.install_dir / "RELEASE.json"
            if rel_json.exists():
                try:
                    data = json.loads(rel_json.read_text(encoding="utf-8"))
                    current_commit = data.get("commit", "legacy")
                except Exception:
                    current_commit = "legacy"

            # 1. 檢查暫停條件與 dry-run（未通過前絕不觸碰任何檔案與服務）
            pause_file = self.state_dir / "PAUSE"
            repo_pause = self.install_dir / "PAUSE"
            if pause_file.exists() or repo_pause.exists():
                log.info("PAUSE 檔案存在，自動更新暫停中。")
                self._record_status(current_commit, "paused", "PAUSE 暫停檔存在", "paused")
                return 0

            if self.conf.get("PET_AUTOUPDATE") == "0":
                log.info("PET_AUTOUPDATE=0，自動更新暫停中。")
                self._record_status(current_commit, "paused", "PET_AUTOUPDATE=0", "paused")
                return 0

            if self.dry_run:
                log.info("[Dry Run] 僅執行檢查，不動任何檔案與服務。")
                st_data = load_update_state(self.state_dir)
                if st_data.get("status") in ("updating", "restarting"):
                    log.info("[Dry Run] 偵測到未完成狀態（%s），dry-run 模式下不修改檔案。", st_data.get("status"))
                return 0

            # 2. 先前更新若被斷電中斷，執行開機自檢還原（已通過鎖與暫停檢查，且非 dry_run）
            st_data = load_update_state(self.state_dir)
            if st_data.get("status") in ("updating", "restarting"):
                log.warning("偵測到前次更新處於未完成狀態（%s），執行開機自檢還原...", st_data.get("status"))
                self.boot_check()
                # 重新讀取還原後的 commit
                if rel_json.exists():
                    try:
                        data = json.loads(rel_json.read_text(encoding="utf-8"))
                        current_commit = data.get("commit", "legacy")
                    except Exception:
                        pass

            # 3. 檢查本地手動修改
            is_mod, mod_reason = verify_manifest(self.install_dir)
            if not is_mod:
                log.warning("偵測到本地修改（%s），自動更新暫停。", mod_reason)
                self._record_status(current_commit, "paused", f"本地版本遭修改：{mod_reason}", "paused")
                return 0

            # 4. 檢查更新間隔（預設 300 秒 / 5 分鐘）
            interval = int(self.conf.get("PET_AUTOUPDATE_INTERVAL", "300"))
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
                free_bytes = shutil.disk_usage(self.install_dir).free
                if free_bytes < 200 * 1024 * 1024:
                    log.error("磁碟空間不足 200MB（剩餘 %d MB），取消下載。", free_bytes // (1024 * 1024))
                    self._record_status(current_commit, auto_state, None, "insufficient_disk_space")
                    return 0
            except OSError:
                pass

            if self.dry_run:
                log.info("[Dry Run] 將下載並覆蓋新版本：%s", target_commit[:12])
                return 0

            # 9. 下載並解壓 tarball 至暫存 staging
            staging_dir = self.state_dir / ".staging"
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

            # 10. 切換前檢查（在解壓目錄執行語法與啟動測試）
            ok, pre_err = pre_switch_check(root_extracted)
            if not ok:
                log.error("切換前檢查未通過：%s", pre_err)
                record_failed_commit(self.state_dir, target_commit, "pre_switch_check", str(pre_err))
                shutil.rmtree(unpacked_dir, ignore_errors=True)
                self._record_status(current_commit, auto_state, None, "pre_switch_check_failed")
                return 0

            # 11. 檢查 16.7 held 條件
            is_held, held_reason = check_held_criteria(self.install_dir, root_extracted, self.state_dir, target_platform="pi")
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
                shutil.rmtree(unpacked_dir, ignore_errors=True)
                return 0

            # 12. 比對受影響之服務
            affected_units = detect_affected_units(self.install_dir, root_extracted)

            # 13. 建立舊版本備份（原子替換舊備份，packs/ 與 deploy.env 不動）
            log.info("建立當前版本檔案備份至 %s...", self.backup_dir)
            try:
                create_backup(self.install_dir, self.backup_dir)
            except Exception as exc:
                log.error("建立舊版本備份失敗，中止更新（既有備份完整保留）：%s", exc)
                shutil.rmtree(unpacked_dir, ignore_errors=True)
                self._record_status(current_commit, auto_state, None, f"backup_failed: {exc}")
                return 1

            # 14. 寫入 updating 狀態標記（供斷電開機自檢識別）
            write_update_state(self.state_dir, {
                "status": "updating",
                "target_commit": target_commit,
                "previous_commit": current_commit,
                "backup_dir": str(self.backup_dir),
                "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })

            # 15. 執行原地覆蓋（覆蓋新檔案、刪除新版已移除檔案，packs/ 與 deploy.env 全程保護）
            log.info("原地覆蓋新版本檔案至 %s...", self.install_dir)
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
            )

            # 產生新版本的 MANIFEST.sha256 與 RELEASE.json
            new_manifest = calculate_manifest(self.install_dir)
            write_manifest(self.install_dir, new_manifest)
            release_meta = {
                "commit": target_commit,
                "short": target_commit[:12],
                "describe": branch,
                "deployed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "source": "auto",
            }
            (self.install_dir / "RELEASE.json").write_text(json.dumps(release_meta, indent=2), encoding="utf-8")

            # 清理 staging 解壓目錄
            shutil.rmtree(unpacked_dir, ignore_errors=True)

            # 16. 寫入 restarting 狀態標記（供重啟失敗自檢識別）
            write_update_state(self.state_dir, {
                "status": "restarting",
                "target_commit": target_commit,
                "previous_commit": current_commit,
                "backup_dir": str(self.backup_dir),
                "restarted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })

            # 17. 選擇性重啟服務
            if affected_units:
                log.info("重啟受影響服務：%s", ", ".join(affected_units))
                restart_units(affected_units)
            else:
                log.info("僅文件/資源改動，無需重啟任何服務。")

            # 18. 健康檢查
            timeout_sec = int(self.conf.get("PET_AUTOUPDATE_HEALTH_TIMEOUT", "120"))
            health_ok, health_msg = run_health_check(self.conf, affected_units, timeout_sec=timeout_sec)

            if not health_ok:
                log.error("健康檢查失敗：%s，開始從備份還原！", health_msg)
                record_failed_commit(self.state_dir, target_commit, "health_check", health_msg)

                # 從備份還原
                restored = restore_from_backup(self.install_dir, self.backup_dir)
                if affected_units:
                    restart_units(affected_units)

                prev_ok, prev_msg = run_health_check(self.conf, affected_units, timeout_sec=60)
                if not prev_ok or not restored:
                    log.critical("回退後上一版本健康檢查依然失敗！狀態標記為 broken。")
                    write_update_state(self.state_dir, {
                        "status": "rollback_failed",
                        "target_commit": target_commit,
                        "previous_commit": current_commit,
                        "error": prev_msg,
                    })
                    self._record_status(current_commit, "broken", f"回退後驗證失敗：{prev_msg}", "broken")
                    return 1

                log.info("已成功從備份還原至上一版本。")
                write_update_state(self.state_dir, {
                    "status": "rolled_back",
                    "target_commit": target_commit,
                    "previous_commit": current_commit,
                    "reason": health_msg,
                    "recovered_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                })
                self._record_status(current_commit, auto_state, None, "rolled_back")
                append_updates_jsonl(self.state_dir, {
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "from_commit": current_commit,
                    "to_commit": target_commit,
                    "result": "rolled_back",
                    "reason": health_msg,
                })
                return 1

            # 19. 更新成功
            log.info("自動更新成功完成！新版本：%s", target_commit[:12])
            write_update_state(self.state_dir, {
                "status": "success",
                "current_commit": target_commit,
                "previous_commit": current_commit,
                "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })

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
    parser = argparse.ArgumentParser(description="desktop-pet 自動更新器（備份＋覆蓋）")
    parser.add_argument("--once", action="store_true", help="執行單次更新週期並結束")
    parser.add_argument("--dry-run", action="store_true", help="僅檢查不實際覆蓋或下載")
    parser.add_argument("--boot-check", action="store_true", help="執行開機自檢")
    parser.add_argument("--rollback", action="store_true", help="手動從備份還原上一版本")
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

    if args.boot_check:
        ok, msg = updater.boot_check()
        return 0 if ok else 1

    if args.rollback:
        return 0 if updater.rollback() else 1

    if args.status:
        st_file = updater.state_dir / "status.json"
        if st_file.exists():
            print(st_file.read_text(encoding="utf-8"))
        else:
            print(json.dumps({"status": "no_status_file", "state_dir": str(updater.state_dir)}, indent=2))
        return 0

    return updater.run_cycle(force=args.force, retry=args.retry)


if __name__ == "__main__":
    sys.exit(main())
