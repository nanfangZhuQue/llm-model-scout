import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


@pytest.fixture
def http_server():
    requests = []
    slow_release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, body):
            content = json.dumps(body).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):
            requests.append(("GET", self.path, dict(self.headers), None))
            self.respond(200, {"data": [{"id": "good"}, {"id": "bad"}]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(("POST", self.path, dict(self.headers), body))
            model = body["model"]
            if model == "slow":
                # Remain pending until fixture cleanup, independent of scheduler timing.
                slow_release.wait()
            if model == "bad":
                self.respond(
                    404, {"error": {"code": "model_not_found", "message": "model not found"}}
                )
            elif self.path.endswith("/messages"):
                self.respond(
                    200,
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "pong"}],
                    },
                )
            else:
                self.respond(
                    200, {"choices": [{"message": {"role": "assistant", "content": "pong"}}]}
                )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        slow_release.set()
        server.shutdown()
        server.server_close()
        thread.join()
