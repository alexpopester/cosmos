"""HTTP API for the Flow Automator tool, served by the engine microservice.

Traefik routes ``<prefix>/*`` (the plugin's ROUTE_PREFIX) here without
stripping the prefix. Every route except ``/health`` needs a COSMOS token in the
``Authorization`` header; ``authorize(token, write)`` decides whether it's
good enough.

    GET    <prefix>/api/health
    GET    <prefix>/api/status
    GET    <prefix>/api/flows
    POST   <prefix>/api/flows                 create
    POST   <prefix>/api/validate              check a flow without saving
    GET    <prefix>/api/flows/<id>
    PUT    <prefix>/api/flows/<id>            replace (never changes armed)
    DELETE <prefix>/api/flows/<id>
    POST   <prefix>/api/flows/<id>/arm
    POST   <prefix>/api/flows/<id>/disarm
    POST   <prefix>/api/flows/<id>/run        {"trigger_node": id?, "variables": {}?, "mode": mode?}
    GET    <prefix>/api/runs?flow_id=&limit=
    GET    <prefix>/api/runs/<id>
    POST   <prefix>/api/runs/<id>/stop
    POST   <prefix>/api/runs/<id>/approve     {"approval_id": id, "decision": "go" | "run" | "abort"}
    POST   <prefix>/api/stop_all
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .engine import EngineError
from .graph import validate_flow


MAX_BODY_BYTES = 5 * 1024 * 1024


class AuthError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


def _routes():
    # (method, regex, handler name, needs write permission)
    return [
        ("GET", r"/api/health", "health", None),
        ("GET", r"/api/status", "status", False),
        ("GET", r"/api/flows", "list_flows", False),
        ("POST", r"/api/flows", "create_flow", True),
        ("POST", r"/api/validate", "validate", False),
        ("GET", r"/api/flows/(?P<flow_id>[\w-]+)", "get_flow", False),
        ("PUT", r"/api/flows/(?P<flow_id>[\w-]+)", "update_flow", True),
        ("DELETE", r"/api/flows/(?P<flow_id>[\w-]+)", "delete_flow", True),
        ("POST", r"/api/flows/(?P<flow_id>[\w-]+)/arm", "arm", True),
        ("POST", r"/api/flows/(?P<flow_id>[\w-]+)/disarm", "disarm", True),
        ("POST", r"/api/flows/(?P<flow_id>[\w-]+)/run", "run", True),
        ("GET", r"/api/runs", "list_runs", False),
        ("GET", r"/api/runs/(?P<run_id>[\w-]+)", "get_run", False),
        ("POST", r"/api/runs/(?P<run_id>[\w-]+)/stop", "stop_run", True),
        ("POST", r"/api/runs/(?P<run_id>[\w-]+)/approve", "approve", True),
        ("POST", r"/api/stop_all", "stop_all", True),
    ]


class FlowApi:
    """Routes requests to the engine. Kept separate from the HTTP server so tests can call it directly."""

    def __init__(self, engine, authorize, prefix="/flow-automator"):
        self.engine = engine
        self.authorize = authorize
        self.prefix = "/" + prefix.strip("/")
        self.routes = [(m, re.compile(re.escape(self.prefix) + p + r"/?$"), h, w) for m, p, h, w in _routes()]

    def handle(self, method, path, query, headers, body):
        """Returns (status, json-able body)."""
        for route_method, pattern, handler, write in self.routes:
            match = pattern.match(path)
            if not match or route_method != method:
                continue
            try:
                if write is not None:
                    self.authorize(headers.get("Authorization") or headers.get("authorization"), write)
                payload = json.loads(body) if body else {}
                return 200, getattr(self, handler)(payload=payload, query=query, **match.groupdict())
            except AuthError as error:
                return error.status, {"error": str(error)}
            except EngineError as error:
                return error.status, {"error": str(error), "problems": error.problems}
            except json.JSONDecodeError as error:
                return 400, {"error": f"Invalid JSON: {error}"}
        if any(pattern.match(path) for _m, pattern, _h, _w in self.routes):
            return 405, {"error": f"{method} not allowed on {path}"}
        return 404, {"error": f"No route for {method} {path}"}

    # ---- handlers --------------------------------------------------------

    def health(self, **_):
        return {"ok": True}

    def status(self, **_):
        return self.engine.status()

    def list_flows(self, **_):
        return self.engine.list_flows()

    def create_flow(self, payload, **_):
        flow, problems = self.engine.save_flow(payload)
        return {"flow": flow, "problems": problems}

    def validate(self, payload, **_):
        return {"problems": validate_flow(payload)}

    def get_flow(self, flow_id, **_):
        flow = self.engine.get_flow(flow_id)
        return {"flow": flow, "problems": validate_flow(flow)}

    def update_flow(self, flow_id, payload, **_):
        flow, problems = self.engine.save_flow(payload, flow_id)
        return {"flow": flow, "problems": problems}

    def delete_flow(self, flow_id, **_):
        self.engine.delete_flow(flow_id)
        return {"deleted": flow_id}

    def arm(self, flow_id, **_):
        return {"flow": self.engine.set_armed(flow_id, True)}

    def disarm(self, flow_id, **_):
        return {"flow": self.engine.set_armed(flow_id, False)}

    def run(self, flow_id, payload, **_):
        variables = payload.get("variables") or {}
        if not isinstance(variables, dict):
            raise EngineError("variables must be an object")
        return self.engine.trigger(
            flow_id,
            payload.get("trigger_node"),
            payload.get("reason") or "Manual run",
            variables,
            manual=True,
            mode=payload.get("mode"),
        )

    def list_runs(self, query, **_):
        flow_id = (query.get("flow_id") or [None])[0]
        try:
            limit = max(1, min(500, int((query.get("limit") or [50])[0])))
        except ValueError:
            raise EngineError("limit must be a number") from None
        return self.engine.list_runs(flow_id, limit)

    def get_run(self, run_id, **_):
        return self.engine.get_run(run_id)

    def approve(self, run_id, payload, **_):
        return self.engine.approve(run_id, payload.get("approval_id"), payload.get("decision"))

    def stop_run(self, run_id, **_):
        self.engine.stop_run(run_id)
        return {"stopping": run_id}

    def stop_all(self, **_):
        return self.engine.stop_all()


def make_handler(api, logger=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._send(413, {"error": "Request body too large"})
                return
            body = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                status, result = api.handle(method, parsed.path, parse_qs(parsed.query), self.headers, body)
            except Exception as error:  # noqa: BLE001 - never drop the connection without a response
                if logger:
                    logger.error(f"Flow API {method} {parsed.path} failed: {error}")
                status, result = 500, {"error": f"{type(error).__name__}: {error}"}
            self._send(status, result)

        def _send(self, status, result):
            data = json.dumps(result, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler naming
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self):  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self):  # noqa: N802
            self._dispatch("DELETE")

        def log_message(self, format, *args):  # noqa: A002 - quiet; COSMOS logs are for events, not requests
            pass

    return Handler


class FlowHttpServer:
    def __init__(self, api, port, logger=None, host="0.0.0.0"):  # noqa: S104 - must be reachable by Traefik
        self.server = ThreadingHTTPServer((host, port), make_handler(api, logger))
        self.server.daemon_threads = True
        self.thread = None

    @property
    def port(self):
        return self.server.server_address[1]

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, name="flow-http", daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
