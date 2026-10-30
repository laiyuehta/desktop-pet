"""測試用的假 LLM 伺服器（OpenAI 相容 /v1/chat/completions、/v1/models），只用標準函式庫。"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLLM:
    """mode：
    valid   回 {"say":"你好呀～","emotion":"happy"}
    think   回 <think>…</think> + JSON
    invalid 回不是 JSON 的文字
    slow    睡 delay 秒再回 valid
    error   HTTP 500
    seq     依序回 replies 清單裡的內容（用完重複最後一個）
    """

    def __init__(self, mode="valid", delay=0.0, replies=None):
        self.mode, self.delay, self.replies = mode, delay, list(replies or [])
        self.calls = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                try:
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass   # 客戶端已逾時離開

            def do_GET(self):
                if self.path.endswith("/models"):
                    self._send(200, {"data": [{"id": "fake-model"}]})
                else:
                    self._send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n))
                fake.calls.append(body)
                m = fake.mode
                if m == "slow":
                    time.sleep(fake.delay)
                    m = "valid"
                if m == "error":
                    self._send(500, {"error": "boom"})
                    return
                if m == "seq":
                    idx = min(len(fake.calls) - 1, len(fake.replies) - 1)
                    content = fake.replies[idx]
                elif m == "think":
                    content = '<think>使用者在打招呼，我要可愛地回應。</think>\n{"say":"嗨嗨～","emotion":"happy","intent":"nod"}'
                elif m == "invalid":
                    content = "我是種子，你好！（沒有 JSON）"
                else:
                    content = '{"say":"你好呀～","emotion":"happy"}'
                self._send(200, {"choices": [{"message": {"role": "assistant", "content": content}}]})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def free_port():
    """取得一個目前沒人聽的埠（模擬 LLM 沒開）。"""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p
