#!/usr/bin/env python3
"""檢查 repo 有沒有個人與環境資訊（CI 的 secrets-scan 會跑）。

兩種檢查：
1. 雜湊比對：家裡 Wi‑Fi 名稱、電腦名稱、正式後端網址這類寫不成通用樣式的字串，
   只比對加鹽 SHA-256（不存原文）。雜湊從環境變數 PERSONAL_HASHES 讀
   （GitHub Actions secret；沒設就跳過這項並提醒），也可以用 --hashes-file 指定本機檔案。
   格式：每行「長度:sha256hex」，# 開頭是註解。
   產生方式：python3 tools/ci/check_personal.py --make-hash（從標準輸入讀，不會印出原文）。
2. commit 訊息：套用 .gitleaks.toml 裡 tag 有 "personal" 的規則（gitleaks 本身只看檔案內容）。

範圍：--tree 看目前所有追蹤的檔案；--history 另外看 --rev-range 的每個 commit 的新增行與訊息
（預設 --all）。有找到就結束碼 1，只印檔案／commit 與規則名稱，不印比對到的內容。
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys
import tomllib

SALT = b"desktop-pet-personal-v1:"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORD = re.compile(r"[0-9a-z]")


def digest(value: str) -> str:
    return hashlib.sha256(SALT + value.strip().lower().encode("utf-8")).hexdigest()


def load_hashes(text: str):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        n, h = line.split(":", 1)
        out.setdefault(int(n), set()).add(h.strip().lower())
    return out


def hash_hits(text: str, hashes) -> int:
    """找出跟雜湊相同、前後不是英數字的子字串（不分大小寫）。"""
    low = text.lower()
    hits = 0
    for n, wanted in hashes.items():
        for i in range(0, len(low) - n + 1):
            if i and WORD.match(low[i - 1]):
                continue
            j = i + n
            if j < len(low) and WORD.match(low[j]):
                continue
            if not WORD.match(low[i]):
                continue
            if hashlib.sha256(SALT + low[i:j].encode("utf-8")).hexdigest() in wanted:
                hits += 1
    return hits


def personal_rules():
    with open(os.path.join(ROOT, ".gitleaks.toml"), "rb") as fh:
        cfg = tomllib.load(fh)
    rules = []
    for r in cfg.get("rules", []):
        if "personal" not in r.get("tags", []):
            continue
        allow = [re.compile(a) for al in r.get("allowlists", []) for a in al.get("regexes", [])]
        rules.append((r["id"], re.compile(r["regex"]), allow))
    return rules


def rule_hits(text: str, rules):
    found = []
    for rid, rx, allow in rules:
        for m in rx.finditer(text):
            if not any(a.search(m.group(0)) for a in allow):
                found.append(rid)
    return found


def git(*args) -> str:
    return subprocess.run(["git", "-C", ROOT, *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tree", action="store_true", help="檢查目前追蹤的檔案")
    ap.add_argument("--history", action="store_true", help="檢查 commit 的新增行與訊息")
    ap.add_argument("--rev-range", default="--all", help="git log 範圍，例如 origin/main..HEAD（預設 --all）")
    ap.add_argument("--hashes-file", help="本機雜湊檔（不要放進 repo）")
    ap.add_argument("--make-hash", action="store_true", help="從標準輸入每行讀一個字串，印出「長度:雜湊」")
    a = ap.parse_args()

    if a.make_hash:
        for line in sys.stdin:
            v = line.strip().lower()
            if v:
                print(f"{len(v)}:{digest(v)}")
        return 0

    text = os.environ.get("PERSONAL_HASHES", "")
    if a.hashes_file:
        with open(a.hashes_file, encoding="utf-8") as fh:
            text += "\n" + fh.read()
    hashes = load_hashes(text)
    if not hashes:
        print("::warning::沒有設定 PERSONAL_HASHES，跳過雜湊比對（只檢查 commit 訊息的通用規則）")
    rules = personal_rules()
    problems = []

    if a.tree:
        for path in git("ls-files", "-z").split("\0"):
            if not path:
                continue
            try:
                with open(os.path.join(ROOT, path), "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue  # 二進位檔
            n = hash_hits(data.decode("utf-8", "replace"), hashes) if hashes else 0
            if n:
                problems.append(f"{path}: 雜湊相符 {n} 處")

    if a.history:
        revs = [r for r in git("rev-list", *a.rev_range.split()).split() if r]
        for rev in revs:
            msg = git("log", "-1", "--format=%B", rev)
            for rid in sorted(set(rule_hits(msg, rules))):
                problems.append(f"commit {rev[:10]} 訊息: {rid}")
            if hashes:
                n = hash_hits(msg, hashes)
                patch = git("show", "--format=", "--no-color", "--unified=0", "--no-ext-diff", rev)
                added = "\n".join(l[1:] for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++"))
                n += hash_hits(added, hashes)
                if n:
                    problems.append(f"commit {rev[:10]}: 雜湊相符 {n} 處")
        print(f"檢查了 {len(revs)} 個 commit")

    for p in problems:
        print(f"::error::{p}")
    print("沒有發現個人資訊" if not problems else f"發現 {len(problems)} 個問題（內容不印出，請看檔案或 commit）")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
