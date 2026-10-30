"""API 金鑰管理與比對（PROPOSAL.md 「前後端連線安全：API 金鑰」）。

- 每台裝置一把金鑰，綁定 pet_ids。
- config.json 只存金鑰的 SHA-256 雜湊，不存金鑰本身。
- 金鑰由 32 bytes 隨機值經 base64url 編碼產生。
- CLI：python -m backend.keys new|list|enable|disable|revoke
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("backend.keys")


def generate_key() -> str:
    """產生 32 bytes 隨機安全的 base64url 金鑰字串。"""
    return secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    """計算金鑰的 SHA-256 雜湊（小寫十六進位字串，64 字元）。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest().lower()


def match_key(
    token: str,
    key_records: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """常數時間比對金鑰。

    比對時計算傳入 token 的 SHA-256，並以 hmac.compare_digest 與所有金鑰逐一比較。
    不提早結束迴圈，防止時間差攻擊推測金鑰位置或內容。
    回傳：(matched_record, is_enabled)。若未匹配回傳 (None, False)。
    """
    if not token or not key_records:
        return None, False

    token_hash = hash_key(token)
    matched: Optional[Dict[str, Any]] = None
    is_enabled = False

    for rec in key_records:
        rec_hash = (rec.get("sha256") or "").lower()
        if not rec_hash:
            continue
        # constant-time compare
        if hmac.compare_digest(token_hash, rec_hash):
            matched = rec
            if rec.get("enabled", True):
                is_enabled = True

    return matched, is_enabled


def save_key_file(path: str, token: str) -> str:
    """將金鑰安全寫入檔案（權限 0600），絕不印到螢幕或紀錄到日誌。"""
    abs_path = os.path.abspath(os.path.expanduser(path))
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY

    # 建立時即指定 0600（只有擁有者能讀寫）
    mode = 0o600
    fd = os.open(abs_path, flags, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token.strip() + "\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise

    try:
        os.chmod(abs_path, mode)
    except OSError:
        pass  # Windows 上 chmod 0600 可能受限，由 ACL / O_CREAT 控管

    return abs_path


def read_key_file(path: str) -> str:
    """讀取金鑰檔內容並去除首尾空白。"""
    abs_path = os.path.abspath(os.path.expanduser(path))
    with open(abs_path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def resolve_config_path(cli_path: Optional[str] = None) -> str:
    """解析目標 config.json 路徑。"""
    if cli_path:
        return os.path.abspath(os.path.expanduser(cli_path))
    env_cfg = os.environ.get("PET_BACKEND_CONFIG")
    if env_cfg:
        return os.path.abspath(os.path.expanduser(env_cfg))
    here = os.path.dirname(os.path.abspath(__file__))
    local = os.path.join(here, "config.json")
    if os.path.isfile(local):
        return local
    return local


def _load_raw_config(config_path: str) -> Dict[str, Any]:
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return data
        except Exception as exc:
            log.warning("讀取設定檔失敗 (%s)：%s", config_path, exc)
    return {}


def _save_raw_config(config_path: str, data: Dict[str, Any]) -> None:
    parent = os.path.dirname(config_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp_path, config_path)


def cmd_new(args: argparse.Namespace) -> int:
    key_id = args.id.strip()
    if not key_id:
        print("錯誤：--id 不能為空", file=sys.stderr)
        return 1

    pet_ids = [p.strip() for p in args.pet if p.strip()]
    if not pet_ids:
        print("錯誤：至少需指定一個 --pet <pet_id>", file=sys.stderr)
        return 1

    config_path = resolve_config_path(args.config)
    cfg = _load_raw_config(config_path)
    srv = cfg.setdefault("server", {})
    keys: List[Dict[str, Any]] = srv.setdefault("keys", [])

    if any(k.get("id") == key_id for k in keys):
        print(f"錯誤：金鑰 ID '{key_id}' 已存在於設定中", file=sys.stderr)
        return 1

    token = generate_key()
    sha = hash_key(token)
    now = time.time()

    out_file = args.output
    if not out_file:
        out_file = os.path.expanduser(f"~/.config/desktop-pet/keys/{key_id}.token")

    saved_path = save_key_file(out_file, token)

    new_rec = {
        "id": key_id,
        "pet_ids": pet_ids,
        "sha256": sha,
        "enabled": True,
        "created": round(now, 1),
        "note": (args.note or "").strip(),
    }
    keys.append(new_rec)
    _save_raw_config(config_path, cfg)

    print("已成功產生 API 金鑰並記錄雜湊：")
    print(f"  金鑰 ID: {key_id}")
    print(f"  綁定寵物 ID: {', '.join(pet_ids)}")
    print(f"  雜湊 (SHA-256): {sha}")
    print(f"  金鑰儲存位置: {saved_path} (權限 0600)")
    print(f"  已更新設定檔: {config_path}")
    print("  [提示] 金鑰已寫入檔案，絕未輸出至螢幕或日誌；請將該金鑰檔妥善部署至裝置。")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    cfg = _load_raw_config(config_path)
    keys: List[Dict[str, Any]] = cfg.get("server", {}).get("keys", [])

    print(f"設定檔：{config_path}")
    if not keys:
        print("目前未設定任何 API 金鑰（server.keys 為空）。")
        return 0

    print(f"共有 {len(keys)} 把金鑰：")
    print(f"{'ID':<16} {'啟用':<6} {'綁定寵物':<20} {'SHA-256 前綴':<12} {'建立時間':<20} {'備註'}")
    print("-" * 80)
    for k in keys:
        kid = str(k.get("id", ""))
        en = "是" if k.get("enabled", True) else "否"
        pets = ",".join(k.get("pet_ids", []))
        sha = str(k.get("sha256", ""))[:8]
        ts = k.get("created")
        ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if isinstance(ts, (int, float)) else "-"
        note = str(k.get("note", ""))
        print(f"{kid:<16} {en:<6} {pets:<20} {sha:<12} {ts_str:<20} {note}")
    return 0


def cmd_set_enabled(args: argparse.Namespace, enabled: bool) -> int:
    config_path = resolve_config_path(args.config)
    cfg = _load_raw_config(config_path)
    keys: List[Dict[str, Any]] = cfg.get("server", {}).get("keys", [])
    found = False
    for k in keys:
        if k.get("id") == args.id:
            k["enabled"] = enabled
            found = True
            break

    if not found:
        print(f"錯誤：找不到金鑰 ID '{args.id}'", file=sys.stderr)
        return 1

    _save_raw_config(config_path, cfg)
    status_str = "啟用" if enabled else "停用"
    print(f"已成功將金鑰 ID '{args.id}' 設定為：{status_str}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    cfg = _load_raw_config(config_path)
    keys: List[Dict[str, Any]] = cfg.get("server", {}).get("keys", [])
    new_keys = [k for k in keys if k.get("id") != args.id]

    if len(new_keys) == len(keys):
        print(f"錯誤：找不到金鑰 ID '{args.id}'", file=sys.stderr)
        return 1

    cfg.setdefault("server", {})["keys"] = new_keys
    _save_raw_config(config_path, cfg)
    print(f"已成功撤銷（刪除）金鑰 ID '{args.id}'")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m backend.keys", description="桌寵後端 API 金鑰管理工具")
    sub = p.add_subparsers(dest="subcommand", required=True)

    # new
    p_new = sub.add_parser("new", help="產生新金鑰並記錄雜湊")
    p_new.add_argument("--id", required=True, help="金鑰 ID（如 pi-seed-001）")
    p_new.add_argument("--pet", action="append", required=True, help="綁定之寵物 ID（可重複指定多個）")
    p_new.add_argument("--note", default="", help="金鑰備註（如 客廳桌寵）")
    p_new.add_argument("--output", default="", help="金鑰檔案儲存路徑（預設 ~/.config/desktop-pet/keys/<id>.token）")
    p_new.add_argument("--config", default="", help="目標 config.json 路徑")

    # list
    p_list = sub.add_parser("list", help="列出已設定的金鑰雜湊清單")
    p_list.add_argument("--config", default="", help="目標 config.json 路徑")

    # enable
    p_en = sub.add_parser("enable", help="啟用指定金鑰")
    p_en.add_argument("--id", required=True, help="金鑰 ID")
    p_en.add_argument("--config", default="", help="目標 config.json 路徑")

    # disable
    p_dis = sub.add_parser("disable", help="停用指定金鑰")
    p_dis.add_argument("--id", required=True, help="金鑰 ID")
    p_dis.add_argument("--config", default="", help="目標 config.json 路徑")

    # revoke
    p_rev = sub.add_parser("revoke", help="撤銷並刪除指定金鑰")
    p_rev.add_argument("--id", required=True, help="金鑰 ID")
    p_rev.add_argument("--config", default="", help="目標 config.json 路徑")

    args = p.parse_args(argv)

    if args.subcommand == "new":
        return cmd_new(args)
    if args.subcommand == "list":
        return cmd_list(args)
    if args.subcommand == "enable":
        return cmd_set_enabled(args, True)
    if args.subcommand == "disable":
        return cmd_set_enabled(args, False)
    if args.subcommand == "revoke":
        return cmd_revoke(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
