"""The real service with DeepSeek replaced by a local fake that streams one scripted answer: for a
real-Obsidian walk that needs an answer no real model can be relied on to give (a citation that
fails its check), slowly enough to look at the screen while it streams. Serves a copy of the data
as live_rig.py does: same environment, stop that lane's backend first and start this on its port.

  python tests/e2e/fake_deepseek.py SERVICE_PORT "answer text"
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CHUNK = 6
PAUSE_S = 0.15
FAKE_PORT = 18999


def fake(answer: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("content-length", 0)))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()

            def send(delta: dict, finish: str | None = None, usage: dict | None = None) -> None:
                frame = {"id": "fake", "object": "chat.completion.chunk", "created": 0, "model": "fake",
                         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                if usage is not None:
                    frame["usage"] = usage
                self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
                self.wfile.flush()

            send({"role": "assistant", "content": ""})
            for i in range(0, len(answer), CHUNK):
                send({"content": answer[i:i + CHUNK]})
                time.sleep(PAUSE_S)
            send({}, "stop", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    return ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), Handler)


def main() -> None:
    port, answer = sys.argv[1], sys.argv[2]
    server = fake(answer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    from evowiki.llm import providers
    providers.DEEPSEEK_BASE_URL = f"http://127.0.0.1:{FAKE_PORT}"
    providers.DEEPSEEK_BETA_BASE_URL = f"http://127.0.0.1:{FAKE_PORT}"
    from evowiki.service.cli import main as service
    raise SystemExit(service(["serve", "--port", port]))


if __name__ == "__main__":
    main()
