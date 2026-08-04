"""
Node agent settings channel + audit ledger — API integration tests.
PUT sanitize/replace semantics, checkin carriage, adoption-ack ledger, perms.
"""

import json

import pytest
from tests.test_utils import headers, regular_headers, response_format, basic_response_checks

import sys
sys.path.append('/home/tapis/service')

from api import api
from fastapi.testclient import TestClient

client = TestClient(api, base_url="https://dev.develop.tapis.io", raise_server_exceptions=False)

NODE_ID = "settingstestnode"
AGENT = {"token": None, "tenant": None}


def agent_headers():
    return {"X-Pods-Node-Token": AGENT["token"], "X-Pods-Tenant": AGENT["tenant"], "Content-Type": "application/json"}


@pytest.fixture(scope="module", autouse=True)
def test_node(headers):
    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)
    result = basic_response_checks(client.post(
        "/pods/nodes",
        data=json.dumps({"node_id": NODE_ID, "type": "host", "description": "settings test node"}),
        headers=headers))
    AGENT["tenant"] = result["join_command"].split("--tenant")[-1].strip()
    join = basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/join",
        data=json.dumps({"claim_token": result["claim_token"]}),
        headers={"X-Pods-Tenant": AGENT["tenant"], "Content-Type": "application/json"}))
    AGENT["token"] = join["node_token"]
    yield
    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)


def test_put_settings_sanitizes_and_ledgers(headers):
    rsp = client.put(
        f"/pods/nodes/{NODE_ID}/settings",
        data=json.dumps({
            "ship_logs": False,
            "metrics_interval": 5,          # clamped to 15
            "containers": ["web-*", "db"],
            "log_encoding": "gzip",
            "bogus_key": 1,                 # ignored
            "container_label_optin": "yes", # wrong type -> ignored
        }),
        headers=headers)
    result = basic_response_checks(rsp)
    st = result["agent_settings"]
    assert st == {"ship_logs": False, "metrics_interval": 15,
                  "containers": ["web-*", "db"], "log_encoding": "gzip"}
    assert "bogus_key" in response_format(rsp)["message"]
    ledger = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/ledger", headers=headers))
    assert any("agent settings changed" in e and "ship_logs" in e for e in ledger)


def test_checkin_carries_settings():
    result = basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/checkin", data=json.dumps({}), headers=agent_headers()))
    assert result["settings"]["ship_logs"] is False
    assert result["settings"]["metrics_interval"] == 15


def test_adoption_ack_lands_in_ledger(headers):
    basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/checkin",
        data=json.dumps({"status": {"os": "linux",
                                    "applied_settings": {"ship_logs": False},
                                    "env_pinned": []}}),
        headers=agent_headers()))
    ledger = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/ledger", headers=headers))
    assert any("agent adopted settings" in e for e in ledger)
    # unchanged applied_settings on the next checkin must NOT add another entry
    before = sum(1 for e in ledger if "agent adopted settings" in e)
    basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/checkin",
        data=json.dumps({"status": {"os": "linux",
                                    "applied_settings": {"ship_logs": False},
                                    "env_pinned": []}}),
        headers=agent_headers()))
    ledger2 = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/ledger", headers=headers))
    assert sum(1 for e in ledger2 if "agent adopted settings" in e) == before


def test_put_replaces_sparse_overlay(headers):
    result = basic_response_checks(client.put(
        f"/pods/nodes/{NODE_ID}/settings", data=json.dumps({"metrics": False}), headers=headers))
    assert result["agent_settings"] == {"metrics": False}   # old keys gone -> agent defaults


def test_settings_require_admin(regular_headers):
    rsp = client.put(f"/pods/nodes/{NODE_ID}/settings",
                     data=json.dumps({"metrics": False}), headers=regular_headers)
    assert rsp.status_code != 200
    rsp = client.get(f"/pods/nodes/{NODE_ID}/ledger", headers=regular_headers)
    assert rsp.status_code != 200


def test_checkin_rejects_oversize_blobs():
    # A valid agent token is not licence to OOM/bloat central — status/inventory
    # bytes and capability count are capped at checkin (413), not silently trimmed.
    big_status = {"os": "linux", "junk": "x" * (256 * 1024 + 10)}
    rsp = client.post(f"/pods/nodes/{NODE_ID}/checkin",
                      data=json.dumps({"status": big_status}), headers=agent_headers())
    assert rsp.status_code == 413, f"oversize status should 413, got {rsp.status_code}"

    big_inv = {"docker_containers": ["c" * 1024] * 1025}  # > 1 MiB
    rsp = client.post(f"/pods/nodes/{NODE_ID}/checkin",
                      data=json.dumps({"inventory": big_inv, "inventory_hash": "h"}),
                      headers=agent_headers())
    assert rsp.status_code == 413, f"oversize inventory should 413, got {rsp.status_code}"

    rsp = client.post(f"/pods/nodes/{NODE_ID}/checkin",
                      data=json.dumps({"capabilities": [f"cap.{i}" for i in range(200)]}),
                      headers=agent_headers())
    assert rsp.status_code == 413, f"too many capabilities should 413, got {rsp.status_code}"

    # a normal-size checkin still succeeds right after
    basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/checkin",
        data=json.dumps({"status": {"os": "linux"}}), headers=agent_headers()))
