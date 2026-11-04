"""Regression tests for HTTP API robustness from docs/EVALUATION-2026-11-04.md (SEC-9, SEC-11)."""

import socket

import pytest
from flow_automator.http_api import FlowApi
from test_triggers import call, fake_authorize, server  # noqa: F401 - server is a fixture


@pytest.mark.parametrize(
    "body",
    [b"[]", b'{"nodes": "x"}', b'{"edges": [1]}', b'{"nodes": [{"id": "a", "type": "log", "data": "x"}]}', b'"text"'],
)
def test_malformed_payloads_get_400_not_500(engine, body):
    api = FlowApi(engine, fake_authorize)
    for path in ("/flow-automator/api/validate", "/flow-automator/api/flows"):
        status, result = api.handle("POST", path, {}, {"Authorization": "good"}, body.decode())
        if path.endswith("validate") and status == 200:
            assert result["problems"], result  # a shape problem, reported as a problem
        else:
            assert status == 400, (path, status, result)


def test_bad_base_version_is_a_400(engine):
    api = FlowApi(engine, fake_authorize)
    status, created = api.handle(
        "POST", "/flow-automator/api/flows", {}, {"Authorization": "good"}, '{"name": "x", "nodes": [], "edges": []}'
    )
    assert status == 200
    flow_id = created["flow"]["id"]
    status, _ = api.handle(
        "PUT",
        f"/flow-automator/api/flows/{flow_id}",
        {},
        {"Authorization": "good"},
        '{"name": "y", "base_version": "abc"}',
    )
    assert status == 400


def _raw(url, request):
    host, port = url.split("//")[1].split("/")[0].split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(request)
        return sock.recv(4096).decode()


def test_bad_content_length_and_missing_token_are_refused_early(server):  # noqa: F811
    path = "/flow-automator/api/validate"
    reply = _raw(server, f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: -5\r\n\r\n".encode())
    assert reply.startswith("HTTP/1.1 400")
    reply = _raw(server, f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n".encode())
    assert reply.startswith("HTTP/1.1 400")
    # A body without a token is refused before it's read (only the headers were sent)
    reply = _raw(server, f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: 1000000\r\n\r\n".encode())
    assert reply.startswith("HTTP/1.1 401")
    assert "Python" not in reply  # no Server: BaseHTTP/... Python/3.x


def test_internal_errors_do_not_leak_details(server, engine, monkeypatch):  # noqa: F811
    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail at /gems/x")

    monkeypatch.setattr(engine, "list_flows", boom)
    status, body = call(f"{server}/flows")
    assert status == 500 and "secret" not in body["error"]
