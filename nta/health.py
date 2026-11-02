"""Tiny /healthz HTTP endpoint (stdlib) for long-running processes."""

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class HealthServer:
    """GET /healthz returns the status dict as JSON: 200 if the last success is fresher
    than stale_after_s, else 503 (including "never succeeded yet")."""

    def __init__(self, status: Callable[[], dict[str, Any]], host: str = "127.0.0.1",
                 port: int = 8765, stale_after_s: float = 60.0) -> None:
        self._status = status
        self._stale_after_s = stale_after_s
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.split("?")[0] != "/healthz":
                    self.send_error(404)
                    return
                body = outer.payload()
                data = json.dumps(body).encode()
                self.send_response(200 if body["ok"] else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:  # silence stderr access log
                pass

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True,
                                        name="healthz")

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def payload(self) -> dict[str, Any]:
        body = dict(self._status())
        since = body.get("seconds_since_last_success")
        body["stale_after_s"] = self._stale_after_s
        # A process can also report itself unhealthy (e.g. nta risk with an open incident).
        body["ok"] = since is not None and since <= self._stale_after_s and not body.get("unhealthy", False)
        return body

    def start(self) -> "HealthServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
