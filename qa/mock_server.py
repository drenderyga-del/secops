#!/usr/bin/env python3
"""Deterministic local HTTP doubles for ScanOps integration acceptance.

The server uses only Python's standard library and never opens an outbound
connection. Request headers and bodies are deliberately excluded from its
observable state so dummy credentials cannot leak into evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse


SERVICES = frozenset({"gitlab", "asoc", "ptai", "mattermost"})
SCENARIOS = frozenset(
    {
        "success",
        "unauthorized",
        "forbidden",
        "rate_limited",
        "malformed",
        "timeout",
        "transport",
    }
)
SCENARIO_ALIASES = {
    "happy": "success",
    "slow": "timeout",
    "malformed_json": "malformed",
    "connection_drop": "transport",
}
DUMMY_CREDENTIALS = {
    "gitlab": "local-gitlab-token",
    "asoc": "local-asoc-token",
    "ptai": "local-ptai-token",
    "ptai_jwt": "local-ptai-jwt",
    "mattermost": "local-mattermost-token",
}


@dataclass
class MockState:
    """Thread-safe scenario, pipeline sequence and sanitized request evidence."""

    delay_seconds: float = 0.25
    scenarios: dict[str, str] = field(
        default_factory=lambda: {service: "success" for service in SERVICES}
    )
    requests: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=500)
    )
    pipeline_polls: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)

    def reset(self) -> None:
        with self.lock:
            self.scenarios = {service: "success" for service in SERVICES}
            self.requests.clear()
            self.pipeline_polls = 0

    def set_scenario(
        self,
        scenario: str,
        service: str = "all",
        delay_seconds: Any = None,
    ) -> None:
        scenario = SCENARIO_ALIASES.get(scenario, scenario)
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario: {scenario}")
        if service != "all" and service not in SERVICES:
            raise ValueError(f"unknown service: {service}")
        if delay_seconds is not None:
            delay = float(delay_seconds)
            if not 0 <= delay <= 30:
                raise ValueError("delay_seconds must be between 0 and 30")
            self.delay_seconds = delay
        with self.lock:
            for target in SERVICES if service == "all" else {service}:
                self.scenarios[target] = scenario

    def scenario_for(self, service: str) -> str:
        with self.lock:
            return self.scenarios[service]

    def next_pipeline_status(self) -> str:
        with self.lock:
            self.pipeline_polls += 1
            return "running" if self.pipeline_polls == 1 else "success"

    def record(
        self,
        *,
        method: str,
        path: str,
        service: str,
        status: int | str,
        body_size: int,
    ) -> None:
        safe_path = path
        if service == "mattermost":
            safe_path = re.sub(r"(/hooks/)[^/?]+", r"\1***", path)
        with self.lock:
            self.requests.append(
                {
                    "method": method,
                    "path": safe_path,
                    "service": service,
                    "status": status,
                    "request_body_bytes": body_size,
                }
            )

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "scenarios": dict(self.scenarios),
                "delay_seconds": self.delay_seconds,
                "pipeline_polls": self.pipeline_polls,
                "request_count": len(self.requests),
                "requests": list(self.requests),
                "outbound_requests": 0,
            }


class ScanOpsMockHandler(BaseHTTPRequestHandler):
    server_version = "ScanOpsIntegrationMock/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def state(self) -> MockState:
        return self.server.mock_state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.getenv("SCANOPS_MOCK_ACCESS_LOG", "").lower() in {
            "1",
            "true",
            "yes",
        }:
            super().log_message(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()

        if path == "/healthz":
            self._json(
                HTTPStatus.OK,
                {"status": "ok", "outbound_requests": 0},
            )
            return
        if path.startswith("/__control__/"):
            self._control(path, body)
            return

        service = self._service_for(path)
        if service is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "unknown local mock route"})
            return

        scenario = self.state.scenario_for(service)
        if self._apply_scenario(
            scenario,
            service=service,
            path=path,
            body_size=len(body),
        ):
            return
        if not self._authorized(service, path):
            self._record(service, path, HTTPStatus.UNAUTHORIZED, len(body))
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid dummy credential"})
            return

        if service == "gitlab":
            status, payload, content_type = self._gitlab(path)
        elif service == "asoc":
            status, payload, content_type = self._asoc(path)
        elif service == "ptai":
            status, payload, content_type = self._ptai(path)
        else:
            status, payload, content_type = self._mattermost(path)

        self._record(service, path, status, len(body))
        self._send(status, payload, content_type)

    def _control(self, path: str, body: bytes) -> None:
        if path == "/__control__/state" and self.command == "GET":
            self._json(HTTPStatus.OK, self.state.snapshot())
            return
        if path == "/__control__/reset" and self.command == "POST":
            self.state.reset()
            self._json(HTTPStatus.OK, self.state.snapshot())
            return
        if path == "/__control__/scenario" and self.command in {"POST", "PUT"}:
            payload = self._decode_json(body)
            try:
                self.state.set_scenario(
                    str(payload.get("scenario", "")),
                    str(payload.get("service", "all")),
                    payload.get("delay_seconds"),
                )
            except (TypeError, ValueError) as exc:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": str(exc), "allowed": sorted(SCENARIOS)},
                )
                return
            self._json(HTTPStatus.OK, self.state.snapshot())
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "unknown control route"})

    def _apply_scenario(
        self,
        scenario: str,
        *,
        service: str,
        path: str,
        body_size: int,
    ) -> bool:
        if scenario == "success":
            return False
        if scenario == "timeout":
            time.sleep(self.state.delay_seconds)
            return False
        if scenario == "transport":
            self._record(service, path, "transport", body_size)
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return True
        if scenario == "malformed":
            self._record(service, path, HTTPStatus.OK, body_size)
            self._send(HTTPStatus.OK, b'{"broken":', "application/json")
            return True
        status = {
            "unauthorized": HTTPStatus.UNAUTHORIZED,
            "forbidden": HTTPStatus.FORBIDDEN,
            "rate_limited": HTTPStatus.TOO_MANY_REQUESTS,
        }[scenario]
        self._record(service, path, status, body_size)
        self._json(status, {"error": scenario, "service": service})
        return True

    def _authorized(self, service: str, path: str) -> bool:
        if service == "gitlab":
            return self.headers.get("PRIVATE-TOKEN") == DUMMY_CREDENTIALS["gitlab"]
        if service == "asoc":
            return self.headers.get("Authorization") == (
                f"Bearer {DUMMY_CREDENTIALS['asoc']}"
            )
        if service == "ptai":
            if path.endswith("/api/auth/signin"):
                return self.headers.get("Access-Token") == DUMMY_CREDENTIALS["ptai"]
            return self.headers.get("Authorization") == (
                f"Bearer {DUMMY_CREDENTIALS['ptai_jwt']}"
            )
        return path.endswith(f"/hooks/{DUMMY_CREDENTIALS['mattermost']}")

    @staticmethod
    def _service_for(path: str) -> str | None:
        if path.startswith("/gitlab/api/v4"):
            return "gitlab"
        if path.startswith("/asoc/api"):
            return "asoc"
        if path.startswith("/ptai"):
            return "ptai"
        if path.startswith("/mattermost/hooks/"):
            return "mattermost"
        return None

    def _gitlab(self, path: str) -> tuple[int, Any, str]:
        route = path.removeprefix("/gitlab/api/v4") or "/"
        if route == "/version" and self.command == "GET":
            return 200, {"version": "18.0.0-local"}, "application/json"
        if route == "/projects" and self.command == "GET":
            return 200, [{"id": 101, "path_with_namespace": "qa/security/repo"}], "application/json"
        if re.fullmatch(r"/projects/[^/]+/ci/lint", route) and self.command == "POST":
            return 200, {"valid": True, "errors": []}, "application/json"
        if (
            re.fullmatch(r"/projects/[^/]+/pipelines/[^/]+/jobs", route)
            and self.command == "GET"
        ):
            status = self.state.next_pipeline_status()
            finished_at = "2026-07-24T10:02:00Z" if status == "success" else None
            duration = 120.0 if status == "success" else None
            return (
                200,
                [
                    {
                        "id": 9101,
                        "name": "sast_scan",
                        "stage": "security",
                        "status": status,
                        "ref": "main",
                        "web_url": "http://127.0.0.1/local/jobs/9101",
                        "started_at": "2026-07-24T10:00:00Z",
                        "finished_at": finished_at,
                        "duration": duration,
                    }
                ],
                "application/json",
            )
        return 404, {"error": "unimplemented GitLab route", "route": unquote(route)}, "application/json"

    def _asoc(self, path: str) -> tuple[int, Any, str]:
        route = path.removeprefix("/asoc/api") or "/"
        if route == "/ext/v1/products/" and self.command == "GET":
            return (
                200,
                {
                    "items": [
                        {
                            "id": "product-local-1",
                            "name": "Local product",
                            "slug": "local-product",
                        }
                    ],
                    "hasNext": False,
                },
                "application/json",
            )
        return 404, {"error": "unimplemented ASOC route", "route": route}, "application/json"

    def _ptai(self, path: str) -> tuple[int, Any, str]:
        route = path.removeprefix("/ptai") or "/"
        if route == "/api/auth/signin" and self.command == "GET":
            return (
                200,
                {"accessToken": DUMMY_CREDENTIALS["ptai_jwt"]},
                "application/json",
            )
        if (
            re.fullmatch(r"/api/projects/name/[^/]+", route)
            and self.command == "GET"
        ):
            return 200, {"id": "pt-project-local-1"}, "application/json"
        if (
            re.fullmatch(r"/api/projects/[^/]+/branches", route)
            and self.command == "GET"
        ):
            return (
                200,
                [{"id": "pt-branch-local-1", "name": "main"}],
                "application/json",
            )
        if (
            re.fullmatch(r"/api/branches/[^/]+/scanResults/last", route)
            and self.command == "GET"
        ):
            return (
                200,
                {
                    "id": "pt-result-local-1",
                    "progress": {"stage": "Scan", "value": 73},
                    "hasErrors": False,
                },
                "application/json",
            )
        return 404, {"error": "unimplemented PT AI route", "route": route}, "application/json"

    def _mattermost(self, path: str) -> tuple[int, Any, str]:
        if self.command == "POST":
            return 200, b"ok", "text/plain; charset=utf-8"
        return 405, b"method not allowed", "text/plain; charset=utf-8"

    def _record(
        self,
        service: str,
        path: str,
        status: int | str,
        body_size: int,
    ) -> None:
        self.state.record(
            method=self.command,
            path=path,
            service=service,
            status=int(status) if isinstance(status, HTTPStatus) else status,
            body_size=body_size,
        )

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    @staticmethod
    def _decode_json(body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _json(
        self,
        status: int,
        payload: Any,
    ) -> None:
        self._send(
            status,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            "application/json",
        )

    def _send(self, status: int, payload: Any, content_type: str) -> None:
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            self.send_response(int(status))
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # Expected after a deterministic timeout scenario.
            return


class LocalMockServer:
    """Context manager used by acceptance tests without a subprocess."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        delay_seconds: float = 0.25,
    ) -> None:
        self.state = MockState(delay_seconds=delay_seconds)
        self.httpd = ThreadingHTTPServer((host, port), ScanOpsMockHandler)
        self.httpd.mock_state = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="scanops-local-integration-mock",
            daemon=True,
        )

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        visible_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        return f"http://{visible_host}:{port}"

    def __enter__(self) -> "LocalMockServer":
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--delay-seconds", type=float, default=0.25)
    args = parser.parse_args()
    server = LocalMockServer(
        args.host,
        args.port,
        delay_seconds=args.delay_seconds,
    )
    print(
        f"ScanOps integration mock listening on {server.base_url}",
        flush=True,
    )
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.httpd.server_close()


if __name__ == "__main__":
    main()
