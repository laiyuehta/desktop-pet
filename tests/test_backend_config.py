"""後端設定：config.example.json 是預設，本機 config.json（不進 repo）疊在上面，且載入時絕不寫檔。"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import config as bc  # noqa: E402


def test_example_has_empty_token():
    cfg = json.loads((ROOT / "backend" / "config.example.json").read_text(encoding="utf-8"))
    assert cfg["server"]["api_token"] == ""


def test_local_config_overrides_and_is_untouched(monkeypatch, tmp_path):
    local = tmp_path / "config.json"
    local.write_text(json.dumps({"server": {"api_token": "tok-local"}}), encoding="utf-8")
    before = (local.read_bytes(), os.stat(local).st_mtime_ns)
    monkeypatch.setattr(bc, "LOCAL_PATH", str(local))
    monkeypatch.delenv("PET_BACKEND_NO_LOCAL_CONFIG", raising=False)
    cfg = bc.load_config()
    assert cfg["server"]["api_token"] == "tok-local"
    assert cfg["server"]["port"] == 8090            # 其他欄位來自 example
    assert (local.read_bytes(), os.stat(local).st_mtime_ns) == before


def test_missing_local_config_is_fine(monkeypatch, tmp_path):
    monkeypatch.setattr(bc, "LOCAL_PATH", str(tmp_path / "nope.json"))
    monkeypatch.delenv("PET_BACKEND_NO_LOCAL_CONFIG", raising=False)
    assert bc.load_config()["server"]["api_token"] == ""
    assert not (tmp_path / "nope.json").exists()


def test_gitignore_covers_local_config():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert "backend/config.json" in gi and "backend-token" in gi and ".env" in gi
