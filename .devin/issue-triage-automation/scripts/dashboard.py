#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Dependency-free metrics dashboard server for the issue-triage MVP.

A tiny stdlib `http.server` that reads the metrics store live (per request) via
the frozen `record.metrics()` / `record.timeseries()` helpers and serves both
the JSON API the front-end consumes and the dashboard HTML page.

Usage (from the scripts/ dir):
    python dashboard.py                 # serve at http://127.0.0.1:8765/
    python dashboard.py --demo-seed     # seed demo rows first, then serve
    python dashboard.py --port 0        # auto-pick a free port

Routes (GET only):
    /                     -> dashboard_template.html (sibling file) or placeholder
    /api/metrics          -> record.metrics() as JSON
    /api/timeseries       -> record.timeseries(bucket) as JSON (bucket=day|week)
    anything else         -> 404 JSON
"""

# Standalone CLI tooling (not part of the superset package): stdlib json is
# intended here.
# ruff: noqa: TID251
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import record

TEMPLATE_PATH = Path(__file__).resolve().parent / "dashboard_template.html"
VALID_BUCKETS = {"day", "week"}

_PLACEHOLDER_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Triage Dashboard</title></head>
<body>
<h1>Issue Triage Dashboard</h1>
<p>Template not delivered yet. The JSON API is live:</p>
<ul>
<li><a href="/api/metrics">/api/metrics</a></li>
<li><a href="/api/timeseries?bucket=day">/api/timeseries?bucket=day</a></li>
</ul>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    """Serve the dashboard page and the read-only metrics JSON API."""

    server_version = "TriageDashboard/1.0"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: object) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _serve_index(self) -> None:
        if TEMPLATE_PATH.exists():
            html = TEMPLATE_PATH.read_text(encoding="utf-8")
        else:
            html = _PLACEHOLDER_HTML
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_timeseries(self, query: dict[str, list[str]]) -> None:
        bucket = query.get("bucket", ["day"])[0]
        if bucket not in VALID_BUCKETS:
            self._send_json(
                400,
                {"error": f"invalid bucket '{bucket}'; expected one of day, week"},
            )
            return
        self._send_json(200, record.timeseries(bucket))

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler API)
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._serve_index()
        elif path == "/api/metrics":
            self._send_json(200, record.metrics())
        elif path == "/api/timeseries":
            self._serve_timeseries(parse_qs(parsed.query))
        else:
            self._send_json(404, {"error": f"not found: {path}"})

    def log_message(self, format: str, *args: object) -> None:
        """Quiet, single-line request logging."""
        print(f"[dashboard] {self.address_string()} - {format % args}")


def serve(host: str, port: int, demo_seed: bool = False) -> None:
    if demo_seed:
        from seed_metrics import seed

        summary = seed()
        print(f"Seeded demo rows: {json.dumps(summary, sort_keys=True)}")

    httpd = HTTPServer((host, port), DashboardHandler)
    bound_port = int(httpd.server_address[1])
    print(f"Serving triage dashboard at http://{host}:{bound_port}/ (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        httpd.server_close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Issue-triage metrics dashboard server.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument(
        "--port", type=int, default=8765, help="port (0 auto-picks a free port)"
    )
    ap.add_argument(
        "--demo-seed",
        action="store_true",
        help="seed representative demo rows before serving",
    )
    args = ap.parse_args()
    serve(args.host, args.port, args.demo_seed)


if __name__ == "__main__":
    main()
