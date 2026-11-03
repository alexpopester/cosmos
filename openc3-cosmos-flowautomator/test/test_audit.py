"""Who did what: users from tokens, permission checks and the audit log."""

import base64
import json
from unittest import mock

from conftest import edge, make_flow, node, wait_until
from flow_automator.cosmos_backend import WRITE_PROBE_KEY, TokenAuthorizer, token_identity
from flow_automator.http_api import AuthError, FlowApi


def jwt(**claims):
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


USERS = {"alice-token": {"username": "alice", "name": "Alice A"}, "bob-token": {"username": "bob", "name": "Bob B"}}


def authorize(token, write):
    if token not in USERS:
        raise AuthError("Not authorized", 401)
    if token == "bob-token" and write:
        raise AuthError("Changing flows needs the script_run permission", 403)
    return USERS[token]


def call(api, method, path, body=None, token="alice-token"):  # noqa: S107 - test token
    return api.handle(
        method, f"/flow-automator/api{path}", {}, {"Authorization": token}, json.dumps(body) if body else ""
    )


def test_token_identity():
    assert token_identity(jwt(preferred_username="alice", name="Alice A")) == {"username": "alice", "name": "Alice A"}
    assert token_identity(jwt(sub="1234"))["username"] == "1234"
    assert token_identity("ses_abc123")["username"] == "anonymous"  # Core session token
    assert token_identity("a.%%%.c")["username"] == "anonymous"


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status

    def json(self):
        return self.body


def test_authorizer_checks_script_run_for_writes_and_caches():
    authorizer = TokenAuthorizer("DEFAULT", "http://api:2901")
    token = jwt(preferred_username="alice")
    with mock.patch("flow_automator.cosmos_backend.requests.post", return_value=Response({"result": None})) as post:
        assert authorizer(token, True) == {"username": "alice", "name": "alice"}
        sent = post.call_args.kwargs["json"]
        assert sent["method"] == "stash_delete" and sent["params"] == [WRITE_PROBE_KEY]
        authorizer(token, True)
        assert post.call_count == 1  # cached
        authorizer(token, False)
        assert post.call_args.kwargs["json"]["method"] == "get_target_names"

    denied = Response({"error": {"message": "User alice does not have permission script_run"}})
    with mock.patch("flow_automator.cosmos_backend.requests.post", return_value=denied):
        try:
            TokenAuthorizer("DEFAULT", "http://api:2901")(token, True)
            raise AssertionError("should be forbidden")
        except AuthError as error:
            assert error.status == 403 and "script_run" in str(error)


def test_writes_are_audited_with_the_user(engine, backend):
    api = FlowApi(engine, authorize)
    status, body = call(
        api,
        "POST",
        "/flows",
        make_flow([node("t", "trigger_manual"), node("c", "send_cmd", command="INST X")], [edge("t", "c")]),
    )
    assert status == 200
    flow_id = body["flow"]["id"]
    assert call(api, "POST", f"/flows/{flow_id}/arm", {})[0] == 200
    assert call(api, "POST", f"/flows/{flow_id}/disarm", {}, token="bob-token")[0] == 403
    assert call(api, "GET", "/flows", token="bob-token")[0] == 200  # reads are fine
    status, result = call(api, "POST", f"/flows/{flow_id}/run", {})
    assert wait_until(lambda: not engine.runs)
    assert engine.get_run(result["run"]["id"])["reason"] == "Manual run by alice"
    assert call(api, "GET", "/whoami", token="bob-token")[1]["user"]["username"] == "bob"

    entries = call(api, "GET", "/audit")[1]
    assert [(e["user"], e["action"]) for e in entries] == [("alice", "ran"), ("alice", "armed"), ("alice", "created")]
    assert entries[0]["target_name"] == "Test flow" and "run " in entries[0]["detail"]
    assert any("[audit] alice armed flow 'Test flow'" in m for _level, m in engine.logger.messages)


def test_approvals_record_who_answered(engine, backend):
    api = FlowApi(engine, authorize)
    flow, _ = engine.save_flow(
        make_flow(
            [node("t", "trigger_manual"), node("c", "send_cmd", command="INST X")], [edge("t", "c")], mode="crawl"
        )
    )
    run_id = engine.trigger(flow["id"], manual=True)["run"]["id"]
    assert wait_until(lambda: engine.status()["approvals"])
    approval = engine.status()["approvals"][0]
    status, _ = call(api, "POST", f"/runs/{run_id}/approve", {"approval_id": approval["id"], "decision": "go"})
    assert status == 200
    assert wait_until(lambda: run_id not in engine.runs)
    step = next(s for s in engine.get_run(run_id)["steps"] if s["node"] == "c")
    assert step["decided_by"] == "alice" and step["message"].startswith("INST X")
    status, entries = api.handle(
        "GET", "/flow-automator/api/audit", {"kind": ["flow"]}, {"Authorization": "bob-token"}, ""
    )
    audit = entries[0]
    assert (
        status == 200
        and audit["user"] == "alice"
        and audit["action"] == "approved"
        and "'go'" in audit["detail"]
        and "crawl" in audit["detail"]
    )


def test_audit_filters_and_cap(engine):
    audit = engine.audit
    audit.limit = 5
    for i in range(8):
        audit.record("alice" if i % 2 else "bob", "did", "flow", f"f{i}", f"Flow {i}")
    assert len(audit.entries(100)) == 5
    assert {e["user"] for e in audit.entries(user="bob")} == {"bob"}
    assert audit.entries(search="flow 7")[0]["target_id"] == "f7"
    assert audit.entries(target_id="f6")[0]["user"] == "bob"
