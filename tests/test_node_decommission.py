"""
Decommission flow — API integration tests: queue + state + ledger, repeat 409,
agent confirmation hard-deletes the row, error keeps the row, unclaimed nodes
delete instantly, plain delete stays the force path, and the health-central
timeout sweep (called directly with timeout 0) removes unconfirmed rows.
"""

import json

import pytest
from tests.test_utils import headers, response_format, basic_response_checks

import sys
sys.path.append('/home/tapis/service')

from api import api
from fastapi.testclient import TestClient

client = TestClient(api, base_url="https://dev.develop.tapis.io", raise_server_exceptions=False)

NODE_ID = "decomtestnode"
AGENT = {"token": None, "tenant": None}


def agent_headers():
    return {"X-Pods-Node-Token": AGENT["token"], "X-Pods-Tenant": AGENT["tenant"]}


def make_claimed_node(headers, node_id=NODE_ID):
    client.delete(f"/pods/nodes/{node_id}", headers=headers)
    result = basic_response_checks(client.post(
        "/pods/nodes",
        data=json.dumps({"node_id": node_id, "type": "host", "description": "decommission test node"}),
        headers=headers))
    AGENT["tenant"] = result["join_command"].split("--tenant")[-1].strip()
    join = basic_response_checks(client.post(
        f"/pods/nodes/{node_id}/join",
        data=json.dumps({"claim_token": result["claim_token"]}),
        headers={"X-Pods-Tenant": AGENT["tenant"]}))
    AGENT["token"] = join["node_token"]


@pytest.fixture(scope="module", autouse=True)
def cleanup(headers):
    yield
    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)
    client.delete(f"/pods/nodes/{NODE_ID}unclaimed", headers=headers)


def test_decommission_queues_and_marks(headers):
    make_claimed_node(headers)
    rsp = client.delete(f"/pods/nodes/{NODE_ID}?decommission=true", headers=headers)
    assert rsp.status_code == 200
    msg = response_format(rsp)["message"]
    assert "Decommission queued" in msg and "force" in msg.lower()
    # Row still exists, marked decommissioning
    node = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}", headers=headers))
    assert node["decommission_ts"] is not None
    ledger = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/ledger", headers=headers))
    assert any("decommission requested" in e for e in ledger)
    # Repeat is a clear 409 pointing at the force path
    rsp = client.delete(f"/pods/nodes/{NODE_ID}?decommission=true", headers=headers)
    assert rsp.status_code == 409
    assert "force" in response_format(rsp)["message"].lower()


def test_agent_confirmation_deletes_row(headers):
    cmds = basic_response_checks(client.get(
        f"/pods/nodes/{NODE_ID}/commands", headers=agent_headers()))["commands"]
    assert [c["type"] for c in cmds] == ["decommission"]
    rsp = client.post(
        f"/pods/nodes/{NODE_ID}/commands/{cmds[0]['command_id']}/result",
        data=json.dumps({"status": "done",
                         "result": {"outcome": "bare-exit",
                                    "detail": "wiped state and exited"}}),
        headers=agent_headers())
    assert rsp.status_code == 200
    assert "node deleted" in response_format(rsp)["message"].lower()
    assert client.get(f"/pods/nodes/{NODE_ID}", headers=headers).status_code == 404


def test_error_result_keeps_row_then_force_delete(headers):
    make_claimed_node(headers)
    client.delete(f"/pods/nodes/{NODE_ID}?decommission=true", headers=headers)
    cmds = basic_response_checks(client.get(
        f"/pods/nodes/{NODE_ID}/commands", headers=agent_headers()))["commands"]
    basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/commands/{cmds[0]['command_id']}/result",
        data=json.dumps({"status": "error", "result": {"error": "could not wipe state"}}),
        headers=agent_headers()))
    node = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}", headers=headers))
    assert node["decommission_ts"] is not None                   # row kept
    ledger = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/ledger", headers=headers))
    assert any("decommission FAILED" in e for e in ledger)
    # plain delete = the force path, works mid-decommission
    assert client.delete(f"/pods/nodes/{NODE_ID}", headers=headers).status_code == 200
    assert client.get(f"/pods/nodes/{NODE_ID}", headers=headers).status_code == 404


def test_unclaimed_node_decommission_deletes_instantly(headers):
    nid = f"{NODE_ID}unclaimed"
    client.delete(f"/pods/nodes/{nid}", headers=headers)
    basic_response_checks(client.post(
        "/pods/nodes",
        data=json.dumps({"node_id": nid, "type": "host", "description": "never claimed"}),
        headers=headers))
    rsp = client.delete(f"/pods/nodes/{nid}?decommission=true", headers=headers)
    assert rsp.status_code == 200
    assert "never claimed" in response_format(rsp)["message"]
    assert client.get(f"/pods/nodes/{nid}", headers=headers).status_code == 404


def test_timeout_sweep_removes_unconfirmed(headers):
    make_claimed_node(headers)
    client.delete(f"/pods/nodes/{NODE_ID}?decommission=true", headers=headers)
    from health_central import sweep_decommissioning_nodes
    removed = sweep_decommissioning_nodes(timeout_minutes=0)
    assert removed >= 1
    assert client.get(f"/pods/nodes/{NODE_ID}", headers=headers).status_code == 404
    # nothing left to sweep
    assert sweep_decommissioning_nodes(timeout_minutes=0) == 0
