"""API 金鑰格式：32 bytes 隨機值的 base64url 字串；後端只存 SHA-256（小寫十六進位）。

金鑰檔：一個檔案只放一把金鑰（前後空白與換行會被去掉），權限 0600。
"""
import hashlib
import os
import secrets

KEY_BYTES = 32


def generate_key() -> str:
    """產生 32 bytes 隨機安全的 base64url 金鑰字串。"""
    return secrets.token_urlsafe(KEY_BYTES)


def hash_key(key: str) -> str:
    """計算金鑰的 SHA-256 雜湊（小寫十六進位字串，64 字元）。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest().lower()


def read_key_file(path: str) -> str:
    """讀取金鑰檔內容並去除首尾空白（支援 ~）。讀不到時丟 OSError。"""
    with open(os.path.abspath(os.path.expanduser(path)), "r", encoding="utf-8") as fh:
        return fh.read().strip()
