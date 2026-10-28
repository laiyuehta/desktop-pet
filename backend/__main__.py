"""python -m backend [--config 路徑] [--host 0.0.0.0] [--port 8090]"""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__
from .config import load_config
from .server import Brain, make_server


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="桌寵後端（大腦）")
    p.add_argument("--config", default=None, help="額外設定檔（預設 backend/config.example.json ＋ 本機 backend/config.json）")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:   # Windows 主控台用 UTF-8 輸出中文
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    host = args.host or cfg["server"]["host"]
    port = args.port or int(cfg["server"]["port"])
    brain = Brain(cfg)
    httpd = make_server(brain, host, port)
    log = logging.getLogger("backend")
    loc = cfg["llm"]["local"]
    log.info("桌寵後端 v%s 啟動：http://%s:%d/health", __version__, host, port)
    log.info("本機 LLM：%s（模型 %s，逾時 %ss）；存檔資料夾：%s", loc["base_url"], loc["model"],
             loc["timeout_s"], cfg["data_dir"])
    if not cfg["server"].get("api_token"):
        log.info("未設定 api_token：只建議在家裡內網使用")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，結束")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
