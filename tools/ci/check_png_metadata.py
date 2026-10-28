#!/usr/bin/env python3
"""檢查 PNG 有沒有夾帶生成用的文字資料（CI 的 secrets-scan 會跑）。

AI 生圖工具（ComfyUI、A1111、InvokeAI…）會把 prompt、workflow、參數寫進 PNG 的文字 chunk
（tEXt／zTXt／iTXt）。這些不該出現在公開的 repo。以下任一情況就算失敗：
- 文字 chunk 的關鍵字（不分大小寫）是 prompt、workflow、parameters 這類生成資料；
- 任何文字 chunk 的內容（解壓後）超過 --max-text 位元組（預設 1024）；
- PNG 格式壞掉（無法確認內容）。

範圍：--tree 看所有追蹤的 *.png；--diff <base>..<head> 看 PR 新增或修改的 *.png（讀 head 的版本）；
也可以直接給檔案路徑。只印檔名、chunk 種類、關鍵字與長度，不印內容。
清掉的方法：用 Pillow 重新存檔不帶 pnginfo，或 `exiftool -all= file.png`、`optipng -strip all`。
"""
import argparse
import os
import struct
import subprocess
import sys
import zlib

SIGNATURE = b"\x89PNG\r\n\x1a\n"
TEXT_CHUNKS = (b"tEXt", b"zTXt", b"iTXt")
# 生成資料常用的關鍵字（比對時轉小寫）
FORBIDDEN_KEYS = {
    "prompt", "workflow", "parameters", "negative_prompt", "negative prompt", "positive_prompt",
    "comfyui", "extra_pnginfo", "sd-metadata", "invokeai_metadata", "invokeai_graph", "dream",
    "generation_data", "generation_parameters", "sd_metadata", "novelai", "comment_prompt",
}
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAX_DECOMPRESS = 16 * 1024 * 1024


def _inflate(data: bytes) -> bytes:
    d = zlib.decompressobj()
    out = d.decompress(data, MAX_DECOMPRESS)
    return out


def text_chunks(data: bytes):
    """回傳 [(chunk 種類, 關鍵字, 文字長度)]；格式不對就丟 ValueError。"""
    if not data.startswith(SIGNATURE):
        raise ValueError("不是 PNG")
    out = []
    i = len(SIGNATURE)
    while i < len(data):
        if i + 8 > len(data):
            raise ValueError("chunk 被截斷")
        n, ctype = struct.unpack(">I4s", data[i:i + 8])
        body = data[i + 8:i + 8 + n]
        if i + 12 + n > len(data):
            raise ValueError("chunk 被截斷")
        if ctype in TEXT_CHUNKS:
            key, sep, rest = body.partition(b"\0")
            if not sep:
                raise ValueError(f"{ctype.decode()} 沒有關鍵字分隔")
            try:
                if ctype == b"tEXt":
                    text = rest
                elif ctype == b"zTXt":
                    text = _inflate(rest[1:])
                else:  # iTXt: flag, method, lang\0, translated\0, text
                    flag = rest[0] if rest else 0
                    _lang, _, rest2 = rest[2:].partition(b"\0")
                    _tr, _, text = rest2.partition(b"\0")
                    if flag:
                        text = _inflate(text)
            except (zlib.error, IndexError) as exc:
                raise ValueError(f"{ctype.decode()} 解壓失敗：{exc}") from exc
            out.append((ctype.decode(), key.decode("latin-1"), len(text)))
        i += 12 + n
        if ctype == b"IEND":
            return out
    raise ValueError("沒有 IEND（檔案不完整）")


def problems_for(name: str, data: bytes, max_text: int):
    try:
        chunks = text_chunks(data)
    except ValueError as exc:
        return [f"{name}: PNG 格式無法解析（{exc}）"]
    probs = []
    for ctype, key, size in chunks:
        k = key.strip().lower()
        if k in FORBIDDEN_KEYS:
            probs.append(f"{name}: {ctype} 關鍵字「{key}」是生成資料（{size} bytes）")
        elif size > max_text:
            probs.append(f"{name}: {ctype}「{key}」文字太長（{size} bytes > {max_text}）")
    return probs


def git(*args, binary=False):
    r = subprocess.run(["git", "-C", ROOT, *args], check=True, capture_output=True)
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="直接檢查這些檔案")
    ap.add_argument("--tree", action="store_true", help="檢查所有追蹤的 *.png")
    ap.add_argument("--diff", metavar="BASE..HEAD", help="檢查這個範圍新增或修改的 *.png（讀 HEAD 的版本）")
    ap.add_argument("--max-text", type=int, default=1024, help="文字 chunk 長度上限（bytes，預設 1024）")
    a = ap.parse_args()

    items = []  # (顯示名稱, 讀取函式)
    for f in a.files:
        items.append((f, lambda f=f: open(f, "rb").read()))
    if a.tree:
        for p in git("ls-files", "-z", "--", "*.png", "*.PNG").split("\0"):
            if p:
                items.append((p, lambda p=p: open(os.path.join(ROOT, p), "rb").read()))
    if a.diff:
        base, _, head = a.diff.partition("..")
        head = head or "HEAD"
        names = git("diff", "--name-only", "-z", "--diff-filter=AMR", f"{base}...{head}", "--", "*.png", "*.PNG").split("\0")
        for p in names:
            if p:
                items.append((f"{p}@{head[:10]}", lambda p=p: git("show", f"{head}:{p}", binary=True)))

    probs = []
    for name, read in items:
        probs += problems_for(name, read(), a.max_text)
    for p in probs:
        print(f"::error::{p}")
    print(f"檢查了 {len(items)} 個 PNG，" + ("沒有發現生成資料" if not probs else f"發現 {len(probs)} 個問題"))
    return 1 if probs else 0


if __name__ == "__main__":
    sys.exit(main())
