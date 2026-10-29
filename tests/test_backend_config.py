"""後端設定：四層優先順序（命令列 > 環境變數／環境檔 > 本機 config.json > config.example.json），載入時絕不寫檔。"""
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
    cfg = bc.load_config(env={})
    assert cfg["server"]["api_token"] == "tok-local"
    assert cfg["server"]["port"] == 8090            # 其他欄位來自 example
    assert (local.read_bytes(), os.stat(local).st_mtime_ns) == before


def test_missing_local_config_is_fine(monkeypatch, tmp_path):
    monkeypatch.setattr(bc, "LOCAL_PATH", str(tmp_path / "nope.json"))
    monkeypatch.delenv("PET_BACKEND_NO_LOCAL_CONFIG", raising=False)
    assert bc.load_config(env={})["server"]["api_token"] == ""
    assert not (tmp_path / "nope.json").exists()


def test_gitignore_covers_local_config():
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert "backend/config.json" in gi and "backend-token" in gi and ".env" in gi


def test_four_layer_priority_and_env_file(monkeypatch, tmp_path):
    # 層 3：PET_BACKEND_CONFIG 指定本機設定檔
    ext_cfg = tmp_path / "ext_config.json"
    ext_cfg.write_text(
        json.dumps({
            "server": {"host": "127.0.0.1", "port": 8091, "api_token": "tok-from-json"},
            "llm": {"local": {"model": "json-model"}},
        }),
        encoding="utf-8",
    )
    tok_file = tmp_path / "secret-token"
    tok_file.write_text("tok-from-file\n", encoding="utf-8")
    data_dir = tmp_path / "ext_data"

    # 層 2b：環境檔（含空值欄位與 %LOCALAPPDATA% 展開）
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    env_file = tmp_path / "env"
    env_file.write_text(
        f"# 測試環境檔\n"
        f"PET_BACKEND_CONFIG={ext_cfg}\n"
        f"PET_BACKEND_HOST=0.0.0.0\n"
        f"PET_BACKEND_PORT=8092\n"
        f"PET_BACKEND_DATA_DIR=%LOCALAPPDATA%/ext_data\n"
        f"PET_BACKEND_API_TOKEN_FILE={tok_file}\n"
        f"PET_LLM_LOCAL_URL=http://127.0.0.1:8088/v1\n"
        f"PET_LLM_LOCAL_MODEL=\n"
        f"PET_CONSOLE_LISTEN=127.0.0.1:8099\n",
        encoding="utf-8",
    )

    # 環境檔覆寫 ext_config.json，空值 PET_LLM_LOCAL_MODEL= 保留 ext_config.json 的 json-model
    cfg1 = bc.load_config(env={}, env_file=str(env_file))
    assert cfg1["server"]["host"] == "0.0.0.0"
    assert cfg1["server"]["port"] == 8092
    assert cfg1["server"]["api_token"] == "tok-from-file"
    assert Path(cfg1["data_dir"]) == data_dir
    assert cfg1["llm"]["local"]["base_url"] == "http://127.0.0.1:8088/v1"
    assert cfg1["llm"]["local"]["model"] == "json-model"
    assert cfg1["console"]["listen"] == "127.0.0.1:8099"

    # 層 2a：程序環境變數優先於環境檔
    cfg2 = bc.load_config(
        env={"PET_BACKEND_PORT": "8093", "PET_LLM_LOCAL_MODEL": "proc-model"},
        env_file=str(env_file),
    )
    assert cfg2["server"]["port"] == 8093
    assert cfg2["llm"]["local"]["model"] == "proc-model"

    # 層 1：命令列 --config (path) 與 overrides 優先於環境變數
    cli_cfg = tmp_path / "cli.json"
    cli_cfg.write_text(json.dumps({"server": {"port": 8094}}), encoding="utf-8")
    cfg3 = bc.load_config(
        path=str(cli_cfg),
        overrides={"server": {"host": "192.0.2.50"}},
        env={"PET_BACKEND_PORT": "8093"},
        env_file=str(env_file),
    )
    assert cfg3["server"]["port"] == 8094
    assert cfg3["server"]["host"] == "192.0.2.50"
