"""测试用的假 OpenAI 兼容翻译服务：http://127.0.0.1:8765/v1

把要翻译的句子加上「（译）」原样返回。不联网、不需要 API Key。
收到的每个请求都记录到 mock_llm_requests.log（默认在同目录，可用 --log 指定），方便检查提示词和上下文。

    python tools/mock_llm.py [--port 8765] [--log 路径]
"""

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_llm_requests.log")


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        # /v1/models：让启动检查能通过
        out = json.dumps({"data": [{"id": "mock"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        user = body["messages"][-1]["content"]
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"model": body.get("model"), "temperature": body.get("temperature"),
                                "system": body["messages"][0]["content"][:80], "user": user},
                               ensure_ascii=False) + "\n")
        text = user.split("【请翻译这一句】\n", 1)[-1]
        time.sleep(0.5)
        out = json.dumps({"choices": [{"message": {"role": "assistant", "content": f"（译）{text}"},
                                       "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--log", default=LOG, help="请求记录文件")
    args = ap.parse_args()
    LOG = os.path.abspath(args.log)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    print(f"mock LLM listening on http://127.0.0.1:{args.port}/v1  (log: {LOG})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()
