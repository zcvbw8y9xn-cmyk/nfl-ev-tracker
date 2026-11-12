"""Local HTTP API and static UI server for the NFL +EV Tracker macOS app."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import threading
from typing import Any, Dict, Optional

from src.pipeline import (
    get_api_key_status,
    load_accounts,
    load_cached_payload,
    load_local_env,
    run_pipeline,
    save_accounts,
    save_api_keys,
    verify_sleeper_username,
)
from src.sheets_export import export_to_google_sheets

UI_INDEX_PATH = Path(__file__).resolve().parent / "ui" / "index.html"


class AppState:
    """Thread-safe container holding the active EV payload and scrape status."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.payload: Optional[Dict[str, Any]] = load_cached_payload()
        self.refreshing: bool = False
        self.step: int = 0
        self.total: int = 11
        self.step_title: str = "Ready"
        self.step_detail: str = ""
        self.error: Optional[str] = None

    def status_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "refreshing": self.refreshing,
                "step": self.step,
                "total": self.total,
                "step_title": self.step_title,
                "step_detail": self.step_detail,
                "error": self.error,
                "has_data": self.payload is not None,
                "fetched_at": self.payload.get("fetched_at") if self.payload else None,
                "fetched_at_display": self.payload.get("fetched_at_display") if self.payload else None,
            }

    def data_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "status": {
                    "refreshing": self.refreshing,
                    "step": self.step,
                    "total": self.total,
                    "step_title": self.step_title,
                    "step_detail": self.step_detail,
                    "error": self.error,
                    "has_data": self.payload is not None,
                    "fetched_at": self.payload.get("fetched_at") if self.payload else None,
                    "fetched_at_display": self.payload.get("fetched_at_display") if self.payload else None,
                },
                "payload": self.payload,
            }

    def start_refresh(self) -> bool:
        with self._lock:
            if self.refreshing:
                return False
            self.refreshing = True
            self.step = 0
            self.total = 11
            self.step_title = "Starting live odds & Pick'em scrape..."
            self.step_detail = ""
            self.error = None

        thread = threading.Thread(target=self._run_refresh_worker, daemon=True)
        thread.start()
        return True

    def _on_progress(self, step: int, total: int, title: str, detail: str) -> None:
        with self._lock:
            self.step = step
            self.total = total
            self.step_title = title
            if detail:
                self.step_detail = detail

    def _run_refresh_worker(self) -> None:
        try:
            payload = run_pipeline(progress_cb=self._on_progress, log_fn=None)
            with self._lock:
                self.payload = payload
                self.step = 11
                self.step_title = "Up to date"
                self.step_detail = f"Refreshed {payload.get('fetched_at_display', '')}"
                self.error = None
        except Exception as exc:
            with self._lock:
                self.error = str(exc)
                self.step_title = "Refresh failed"
        finally:
            with self._lock:
                self.refreshing = False


def make_handler(state: AppState):
    class TrackerHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            # Keep stdout reserved for READY banner and errors
            pass

        def _send_json(self, code: int, obj: Any) -> None:
            raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _send_html(self, html_bytes: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html_bytes)

        def _read_json_body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                return {}

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/" or path == "/index.html":
                if not UI_INDEX_PATH.exists():
                    self._send_json(404, {"error": f"UI not found at {UI_INDEX_PATH}"})
                    return
                self._send_html(UI_INDEX_PATH.read_bytes())
                return
            if path == "/api/health":
                self._send_json(200, {"ok": True})
                return
            if path == "/api/status":
                self._send_json(200, state.status_dict())
                return
            if path == "/api/data":
                self._send_json(200, state.data_dict())
                return
            if path == "/api/keys":
                self._send_json(200, {"keys": get_api_key_status()})
                return
            if path == "/api/accounts":
                self._send_json(200, {"accounts": load_accounts()})
                return
            self._send_json(404, {"error": "Not found"})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/accounts":
                body = self._read_json_body()
                accounts_in = body.get("accounts")
                if not isinstance(accounts_in, list):
                    self._send_json(400, {"error": "Expected 'accounts' list."})
                    return
                saved = save_accounts(accounts_in)
                self._send_json(200, {"accounts": saved})
                return
            if path == "/api/accounts/verify-sleeper":
                body = self._read_json_body()
                try:
                    profile = verify_sleeper_username(str(body.get("username") or ""))
                    self._send_json(200, {"profile": profile})
                except Exception as exc:
                    self._send_json(404, {"error": str(exc)})
                return
            if path == "/api/keys":
                body = self._read_json_body()
                updates = body.get("keys") if isinstance(body.get("keys"), dict) else {}
                keys_status = save_api_keys(updates)
                started = state.start_refresh() if body.get("refresh") else False
                self._send_json(200, {"keys": keys_status, "refresh_started": started, "status": state.status_dict()})
                return
            if path == "/api/refresh":
                started = state.start_refresh()
                self._send_json(202 if started else 200, {"started": started, "status": state.status_dict()})
                return
            if path == "/api/export-sheets":
                body = self._read_json_body()
                snapshot = state.data_dict().get("payload")
                if not snapshot:
                    self._send_json(400, {"error": "No scraped dataset loaded yet. Refresh first."})
                    return
                try:
                    sid = (body.get("spreadsheet_id") or os.getenv("SPREADSHEET_ID", "")).strip() or None
                    res = export_to_google_sheets(
                        dataset=snapshot["dataset"],
                        source_counts=snapshot["source_counts"],
                        optional_status=snapshot["optional_status"],
                        spreadsheet_id=sid,
                    )
                    self._send_json(200, res)
                except Exception as exc:
                    self._send_json(500, {"error": str(exc)})
                return
            self._send_json(404, {"error": "Not found"})

    return TrackerHandler


def main() -> None:
    load_local_env()
    parser = argparse.ArgumentParser(description="NFL EV Tracker Local App Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18420)
    parser.add_argument("--refresh-on-start", action="store_true")
    args = parser.parse_args()

    state = AppState()
    if state.payload is None or args.refresh_on_start:
        state.start_refresh()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    actual_port = server.server_address[1]
    print(f"READY http://{args.host}:{actual_port}", flush=True)

    def _shutdown(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
