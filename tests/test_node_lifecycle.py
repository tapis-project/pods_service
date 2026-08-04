"""
Agent lifecycle (restart + self-update) — API integration tests: source
serving + sha/version headers, trigger gates (source availability, effective
allow_self_update incl. agent-reported override), queue/deliver/result flow,
one-active guard, and the ledger trail. Uses PODS_AGENT_SOURCE to point central
at a fixture (TestClient runs in-process, so env edits reach the handlers).
"""

import hashlib
import json
import os
import tempfile

import pytest
from tests.test_utils import headers, response_format, basic_response_checks

import sys
sys.path.append('/home/tapis/service')

from api import api
from fastapi.testclient import TestClient

client = TestClient(api, base_url="https://dev.develop.tapis.io", raise_server_exceptions=False)

NODE_ID = "lifecycletestnode"
AGENT = {"token": None, "tenant": None}
FIXTURE_SRC = b'AGENT_VERSION = "88.0.0"\nprint("fixture agent")\n'
FIXTURE_SHA = hashlib.sha256(FIXTURE_SRC).hexdigest()


def agent_headers():
    return {"X-Pods-Node-Token": AGENT["token"], "X-Pods-Tenant": AGENT["tenant"], "Content-Type": "application/json"}


@pytest.fixture(scope="module", autouse=True)
def test_node(headers):
    fd, path = tempfile.mkstemp(suffix="_pods_agent.py")
    with os.fdopen(fd, "wb") as f:
        f.write(FIXTURE_SRC)
    os.environ["PODS_AGENT_SOURCE"] = path

    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)
    result = basic_response_checks(client.post(
        "/pods/nodes",
        data=json.dumps({"node_id": NODE_ID, "type": "host", "description": "lifecycle test node"}),
        headers=headers))
    AGENT["tenant"] = result["join_command"].split("--tenant")[-1].strip()
    join = basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/join",
        data=json.dumps({"claim_token": result["claim_token"]}),
        headers={"X-Pods-Tenant": AGENT["tenant"], "Content-Type": "application/json"}))
    AGENT["token"] = join["node_token"]
    yield
    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)
    os.environ.pop("PODS_AGENT_SOURCE", None)
    os.unlink(path)


def test_agent_source_served_with_verifiable_headers():
    rsp = client.get(f"/pods/nodes/{NODE_ID}/agent-source", headers=agent_headers())
    assert rsp.status_code == 200
    assert rsp.content == FIXTURE_SRC
    assert rsp.headers["X-Agent-Sha256"] == FIXTURE_SHA
    assert rsp.headers["X-Agent-Version"] == "88.0.0"


def test_checkin_advertises_source(headers):
    result = basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/checkin", data=json.dumps({}), headers=agent_headers()))
    eps = result["endpoints"]
    assert eps["agent_source"].endswith(f"/nodes/{NODE_ID}/agent-source")
    assert eps["agent_source_sha256"] == FIXTURE_SHA
    assert eps["agent_source_version"] == "88.0.0"


def test_update_blocked_until_allowed(headers):
    rsp = client.post(f"/pods/nodes/{NODE_ID}/update", headers=headers)
    assert rsp.status_code != 200
    assert "allow_self_update" in response_format(rsp)["message"]

    basic_response_checks(client.put(
        f"/pods/nodes/{NODE_ID}/settings",
        data=json.dumps({"allow_self_update": True}), headers=headers))
    result = basic_response_checks(client.post(f"/pods/nodes/{NODE_ID}/update", headers=headers))
    assert result["type"] == "update" and result["status"] == "queued"
    assert result["params"] == {"to_version": "88.0.0", "sha256": FIXTURE_SHA}

    # one-active guard: a second update (and a restart) both 409 while queued
    assert client.post(f"/pods/nodes/{NODE_ID}/update", headers=headers).status_code == 409
    assert client.post(f"/pods/nodes/{NODE_ID}/restart", headers=headers).status_code == 409

    # agent delivers + completes; completion lands in the ledger
    cmds = basic_response_checks(client.get(
        f"/pods/nodes/{NODE_ID}/commands", headers=agent_headers()))["commands"]
    assert [c["type"] for c in cmds] == ["update"]
    basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/commands/{cmds[0]['command_id']}/result",
        data=json.dumps({"status": "done",
                         "result": {"detail": "installed 88.0.0 over 0.3.0; re-exec'ing"}}),
        headers=agent_headers()))
    ledger = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/ledger", headers=headers))
    assert any("self-update queued" in e and "88.0.0" in e for e in ledger)
    assert any("agent update completed" in e and "installed 88.0.0" in e for e in ledger)


def test_agent_reported_override_wins_precheck(headers):
    # Central says allowed (set in the previous test), but the agent REPORTS the
    # effective value as off (env-pinned on the box) — the precheck must refuse.
    basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/checkin",
        data=json.dumps({"status": {"os": "linux",
                                    "applied_settings": {"allow_self_update": False},
                                    "env_pinned": ["allow_self_update"]}}),
        headers=agent_headers()))
    rsp = client.post(f"/pods/nodes/{NODE_ID}/update", headers=headers)
    assert rsp.status_code != 200
    assert "allow_self_update" in response_format(rsp)["message"]


def test_restart_flow_and_ledger(headers):
    result = basic_response_checks(client.post(f"/pods/nodes/{NODE_ID}/restart", headers=headers))
    assert result["type"] == "restart" and result["status"] == "queued"
    cmds = basic_response_checks(client.get(
        f"/pods/nodes/{NODE_ID}/commands", headers=agent_headers()))["commands"]
    assert [c["type"] for c in cmds] == ["restart"]
    basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/commands/{cmds[0]['command_id']}/result",
        data=json.dumps({"status": "done", "result": {"detail": "re-exec'ing in place"}}),
        headers=agent_headers()))
    ledger = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/ledger", headers=headers))
    assert any("restart queued" in e for e in ledger)
    assert any("agent restart completed" in e for e in ledger)


def test_source_gate_without_env_override(headers):
    """Without the env fixture, behavior depends on whether the container has a
    real agent copy (image COPY / dev mount): present -> the update trigger gets
    past the source check and refuses on the setting gate (the last checkin
    reported allow_self_update pinned off); absent -> a clear 503 explains the
    missing source, on the trigger AND the source endpoint."""
    saved = os.environ.pop("PODS_AGENT_SOURCE")
    try:
        fallback = os.path.isfile("/home/tapis/agent/pods_agent.py")
        rsp = client.post(f"/pods/nodes/{NODE_ID}/update", headers=headers)
        if fallback:
            assert rsp.status_code == 403
            assert "allow_self_update" in response_format(rsp)["message"]
            assert client.get(f"/pods/nodes/{NODE_ID}/agent-source",
                              headers=agent_headers()).status_code == 200
        else:
            assert rsp.status_code == 503
            assert "no agent source" in response_format(rsp)["message"]
            assert client.get(f"/pods/nodes/{NODE_ID}/agent-source",
                              headers=agent_headers()).status_code == 503
    finally:
        os.environ["PODS_AGENT_SOURCE"] = saved
