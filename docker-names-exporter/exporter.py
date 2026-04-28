"""Tiny Prometheus exporter that maps Docker container IDs to their
human-readable names, so PromQL queries can join cAdvisor's id-only
metrics back to readable labels.

Polls the Docker socket (read-only) every 30s, exposes a single
`docker_container_info{container_id, name, image} 1` gauge.

Listens on :9101. Stdlib only.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

DOCKER_SOCK = "/var/run/docker.sock"
POLL_INTERVAL = 30  # seconds

_metrics_lock = threading.Lock()
_latest_metrics = "# initialising\n"


def docker_get(path: str) -> bytes:
    """Talk to /var/run/docker.sock with raw HTTP/1.1."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(DOCKER_SOCK)
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n".encode())
        chunks = []
        while True:
            data = s.recv(65536)
            if not data:
                break
            chunks.append(data)
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    # Docker uses chunked transfer encoding — strip the framing if present.
    if b"Transfer-Encoding: chunked" in head:
        out = bytearray()
        while body:
            line, _, rest = body.partition(b"\r\n")
            try:
                size = int(line.strip(), 16)
            except ValueError:
                break
            if size == 0:
                break
            out.extend(rest[:size])
            body = rest[size:].lstrip(b"\r\n")
        return bytes(out)
    return body


def render_metrics() -> str:
    """Build the textfile-format /metrics output from `docker ps`."""
    try:
        body = docker_get("/v1.44/containers/json?all=false")
        containers = json.loads(body)
    except Exception as e:
        return f"# error fetching containers: {e}\n"

    lines = [
        "# HELP docker_container_info Container ID -> name mapping (1 = running).",
        "# TYPE docker_container_info gauge",
    ]
    for c in containers:
        full_id = c.get("Id", "")
        short_id = full_id[:12]
        names = c.get("Names", [])
        # Names start with a leading slash; take the first one.
        name = (names[0] if names else "").lstrip("/")
        image = c.get("Image", "")
        # Escape backslashes and double quotes per Prometheus exposition format.

        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')

        lines.append(
            f'docker_container_info{{container_id="{esc(short_id)}",'
            f'name="{esc(name)}",image="{esc(image)}"}} 1'
        )
    lines.append("")
    return "\n".join(lines)


def poll_loop():
    global _latest_metrics
    while True:
        text = render_metrics()
        with _metrics_lock:
            _latest_metrics = text
        time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        with _metrics_lock:
            body = _latest_metrics.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):
        pass


def main() -> None:
    threading.Thread(target=poll_loop, daemon=True).start()
    HTTPServer(("0.0.0.0", 9101), Handler).serve_forever()


if __name__ == "__main__":
    main()
