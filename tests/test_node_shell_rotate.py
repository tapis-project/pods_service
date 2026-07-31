"""
Shell exec + no-downtime token rotation — API integration tests. Shell: the
env-only gate refuses centrally (with the fix in the message), an agent
reporting the capability can queue/deliver/complete, and the ledger records the
command AND its exit code. Rotate: the two-phase handshake — both tokens work
while pending, confirming with the OLD token does NOT promote, confirming with
the NEW one promotes and revokes the old.
"""

import json

import pytest
from tests.test_utils import headers, response_format, basic_response_checks

import sys
sys.path.append('/home/tapis/service')

from api import api
from fastapi.testclient import TestClient

client = TestClient(api, base_url="https://dev.develop.tapis.io", raise_server_exceptions=False)

NODE_ID = "shellrotatenode"
AGENT = {"token": None, "tenant": None}


def agent_headers(token=None):
    return {"X-Pods-Node-Token": token or AGENT["token"], "X-Pods-Tenant": AGENT["tenant"]}


def report_shell_capability(allowed):
    """Agent status drives the server's shell precheck (env-only at the edge)."""
    basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/checkin",
        data=json.dumps({"status": {"os": "linux",
                                    "applied_settings": {"allow_shell": allowed},
                                    "env_pinned": ["allow_shell"] if allowed else []}}),
        headers=agent_headers()))


@pytest.fixture(scope="module", autouse=True)
def test_node(headers):
    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)
    result = basic_response_checks(client.post(
        "/pods/nodes",
        data=json.dumps({"node_id": NODE_ID, "type": "host", "description": "shell/rotate test node"}),
        headers=headers))
    AGENT["tenant"] = result["join_command"].split("--tenant")[-1].strip()
    join = basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/join",
        data=json.dumps({"claim_token": result["claim_token"]}),
        headers={"X-Pods-Tenant": AGENT["tenant"]}))
    AGENT["token"] = join["node_token"]
    yield
    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)


def test_shell_refused_until_agent_reports_capability(headers):
    report_shell_capability(False)
    rsp = client.post(f"/pods/nodes/{NODE_ID}/shell",
                      data=json.dumps({"command": "echo hi"}), headers=headers)
    assert rsp.status_code == 403
    msg = response_format(rsp)["message"]
    assert "PODS_AGENT_ALLOW_SHELL=true" in msg and "cannot enable it remotely" in msg


def test_shell_validates_body(headers):
    report_shell_capability(True)
    for bad in [{}, {"command": "   "}, {"command": 5}]:
        rsp = client.post(f"/pods/nodes/{NODE_ID}/shell",
                          data=json.dumps(bad), headers=headers)
        assert rsp.status_code == 400


def test_shell_full_flow_and_ledger(headers):
    report_shell_capability(True)
    result = basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/shell",
        data=json.dumps({"command": "df -h /scratch", "timeout": 9999}),
        headers=headers))
    assert result["type"] == "shell"
    assert result["params"]["command"] == "df -h /scratch"
    assert result["params"]["timeout"] == 300              # clamped to the max

    cmds = basic_response_checks(client.get(
        f"/pods/nodes/{NODE_ID}/commands", headers=agent_headers()))["commands"]
    assert [c["type"] for c in cmds] == ["shell"]
    basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/commands/{cmds[0]['command_id']}/result",
        data=json.dumps({"status": "done",
                         "result": {"exit_code": 0, "stdout": "Filesystem  Size",
                                    "stderr": "", "duration_ms": 12.0}}),
        headers=agent_headers()))

    ledger = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/ledger", headers=headers))
    assert any("shell queued" in e and "df -h /scratch" in e for e in ledger)
    assert any("shell completed" in e and "exit 0" in e for e in ledger)
    runs = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/shell", headers=headers))
    assert runs[0]["result"]["stdout"].startswith("Filesystem")


def test_rotate_two_phase_old_token_valid_until_confirmed(headers):
    old_token = AGENT["token"]
    basic_response_checks(client.post(f"/pods/nodes/{NODE_ID}/rotate", headers=headers))
    cmds = basic_response_checks(client.get(
        f"/pods/nodes/{NODE_ID}/commands", headers=agent_headers(old_token)))["commands"]
    rotate = [c for c in cmds if c["type"] == "rotate"][0]
    new_token = rotate["params"]["node_token"]
    assert new_token and new_token != old_token

    # BOTH tokens authenticate during the window — that is the no-downtime part
    assert client.post(f"/pods/nodes/{NODE_ID}/checkin", data=json.dumps({}),
                       headers=agent_headers(old_token)).status_code == 200
    assert client.post(f"/pods/nodes/{NODE_ID}/checkin", data=json.dumps({}),
                       headers=agent_headers(new_token)).status_code == 200

    # confirming with the OLD token must NOT promote (no proof of persistence)
    basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/commands/{rotate['command_id']}/result",
        data=json.dumps({"status": "done", "result": {"detail": "wrong token"}}),
        headers=agent_headers(old_token)))
    ledger = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/ledger", headers=headers))
    assert any("confirmed with the OLD token" in e for e in ledger)
    assert client.post(f"/pods/nodes/{NODE_ID}/checkin", data=json.dumps({}),
                       headers=agent_headers(old_token)).status_code == 200   # still active


def test_rotate_confirmed_with_new_token_promotes_and_revokes(headers):
    old_token = AGENT["token"]
    basic_response_checks(client.post(f"/pods/nodes/{NODE_ID}/rotate", headers=headers))
    cmds = basic_response_checks(client.get(
        f"/pods/nodes/{NODE_ID}/commands", headers=agent_headers(old_token)))["commands"]
    rotate = [c for c in cmds if c["type"] == "rotate"][0]
    new_token = rotate["params"]["node_token"]

    basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/commands/{rotate['command_id']}/result",
        data=json.dumps({"status": "done", "result": {"detail": "persisted + confirmed"}}),
        headers=agent_headers(new_token)))

    ledger = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/ledger", headers=headers))
    assert any("rotation CONFIRMED" in e for e in ledger)
    # new token works, old one is dead
    assert client.post(f"/pods/nodes/{NODE_ID}/checkin", data=json.dumps({}),
                       headers=agent_headers(new_token)).status_code == 200
    assert client.post(f"/pods/nodes/{NODE_ID}/checkin", data=json.dumps({}),
                       headers=agent_headers(old_token)).status_code == 403
    AGENT["token"] = new_token
