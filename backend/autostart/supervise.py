"""R7 後端看門程式：工作排程器在 pi-user 登入時用 pythonw.exe 執行（沒有視窗）。

- 用 CREATE_NO_WINDOW 跑 backend\\start_hidden.cmd（後端有自己隱藏的主控台，不受別的視窗 Ctrl+C／關閉影響）。
- 後端結束 → 5 秒後重啟；跑不到 2 分鐘就掛的話退避加倍，最多 60 秒。
- 防重複：127.0.0.1:18090 當鎖（第二個 supervise 直接結束）；8090 已經有人在聽就不啟動，每 15 秒看一次，等它停了才接手。
- 紀錄寫在 <repo>\\backend_supervisor.log（超過 1 MB 自動截掉舊的）。
"""
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKEND)
CMD = os.path.join(BACKEND, "start_hidden.cmd")
LOG = os.path.join(ROOT, "backend_supervisor.log")
PORT = int(os.environ.get("PET_BACKEND_PORT", "8090"))
LOCK_PORT = int(os.environ.get("PET_SUPERVISOR_LOCK_PORT", "18090"))
CREATE_NO_WINDOW = 0x08000000


def log(msg: str) -> None:
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 1 << 20:
            with open(LOG, "rb") as fh:
                tail = fh.read()[-(256 << 10):]
            with open(LOG, "wb") as fh:
                fh.write(tail)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def port_in_use(port: int = PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def take_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def run_forever(start=None, sleep=time.sleep, max_runs=None) -> None:
    start = start or (lambda: subprocess.Popen(["cmd.exe", "/c", CMD], cwd=ROOT, creationflags=CREATE_NO_WINDOW,
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL).wait())
    backoff, waiting, runs = 5.0, False, 0
    while max_runs is None or runs < max_runs:
        if port_in_use():
            if not waiting:
                log(f"{PORT} 已經有後端在跑，不重複啟動；每 15 秒檢查一次")
                waiting = True
            sleep(15)
            continue
        waiting = False
        runs += 1
        t0 = time.time()
        log("啟動後端（start_hidden.cmd）")
        rc = start()
        ran = time.time() - t0
        backoff = 5.0 if ran > 120 else min(60.0, backoff * 2 if runs > 1 else 5.0)
        log(f"後端結束（rc={rc}，跑了 {ran:.0f} 秒），{backoff:.0f} 秒後重啟")
        sleep(backoff)


def main() -> int:
    lock = take_lock()
    if lock is None:
        log("已經有 supervise.py 在跑，這個結束")
        return 0
    log(f"supervise 開始（pid {os.getpid()}）")
    try:
        run_forever()
    except Exception as exc:  # noqa: BLE001  pythonw 沒有 stderr，寫進紀錄
        log(f"supervise 出錯：{type(exc).__name__}: {exc}")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
