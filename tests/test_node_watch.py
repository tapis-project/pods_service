"""
Storage watch — API integration tests: watch_paths settings PUT (sanitize +
ledger), extras riding checkin metrics_samples into series + caps, and
edge-triggered warn/clear ledger entries from checkin status watches.
"""

import json
import time

import pytest
from tests.test_utils import headers, regular_headers, response_format, basic_response_checks

import sys
sys.path.append('/home/tapis/service')

from api import api
from fastapi.testclient import TestClient

client = TestClient(api, base_url="https://dev.develop.tapis.io", raise_server_exceptions=False)

NODE_ID = "watchtestnode"
AGENT = {"token": None, "tenant": None}


def agent_headers():
    return {"X-Pods-Node-Token": AGENT["token"], "X-Pods-Tenant": AGENT["tenant"], "Content-Type": "application/json"}


@pytest.fixture(scope="module", autouse=True)
def test_node(headers):
    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)
    result = basic_response_checks(client.post(
        "/pods/nodes",
        data=json.dumps({"node_id": NODE_ID, "type": "host", "description": "storage watch test node"}),
        headers=headers))
    AGENT["tenant"] = result["join_command"].split("--tenant")[-1].strip()
    join = basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/join",
        data=json.dumps({"claim_token": result["claim_token"]}),
        headers={"X-Pods-Tenant": AGENT["tenant"], "Content-Type": "application/json"}))
    AGENT["token"] = join["node_token"]
    yield
    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)


def test_put_watch_paths_sanitizes_and_ledgers(headers):
    rsp = client.put(
        f"/pods/nodes/{NODE_ID}/settings",
        data=json.dumps({"watch_paths": [
            {"path": "/scratch/", "warn": "90%", "interval_s": 5},   # clamped to 300
            {"path": "/data", "warn": "banana", "wat": 1},           # bad subfields dropped
            {"path": "relative"},                                    # rejected
        ]}),
        headers=headers)
    result = basic_response_checks(rsp)
    assert result["agent_settings"]["watch_paths"] == [
        {"path": "/scratch", "warn": "90%", "interval_s": 300},
        {"path": "/data"},
    ]
    msg = response_format(rsp)["message"]
    assert "watch_paths[1].warn" in msg and "watch_paths[2].path" in msg
    ledger = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/ledger", headers=headers))
    assert any("watch_paths" in e for e in ledger)


def test_extras_ride_metrics_into_series_and_caps(headers):
    now = time.time()
    basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/checkin",
        data=json.dumps({"metrics_samples": [
            {"ts": now - 60, "load1": 0.5,
             "extras": {"disk:/scratch:used": 100.0, "disk:/scratch:total": 1000.0}},
            {"ts": now, "load1": 0.7,
             "extras": {"disk:/scratch:used": 200.0, "disk:/scratch:total": 1000.0}},
        ]}),
        headers=agent_headers()))
    result = basic_response_checks(client.get(
        f"/pods/nodes/{NODE_ID}/metrics?window_s=3600&step_s=60", headers=headers))
    assert result["series"]["disk:/scratch:used"], "extras series missing"
    assert "disk:/scratch:total" not in result["series"]
    assert result["caps"]["disk:/scratch:total"] == 1000.0


def test_watch_warn_and_clear_ledger_edges(headers):
    def checkin_with(state, pct, used_h):
        basic_response_checks(client.post(
            f"/pods/nodes/{NODE_ID}/checkin",
            data=json.dumps({"status": {
                "os": "linux",
                "watches": {"/scratch": {"state": state, "pct": pct, "used_h": used_h,
                                         "threshold": "90%"}}}}),
            headers=agent_headers()))

    checkin_with("warn", 92.1, "184.2 GB")
    ledger = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/ledger", headers=headers))
    warns = [e for e in ledger if "storage watch WARN: /scratch" in e]
    assert len(warns) == 1 and "92.1%" in warns[0] and "184.2 GB" in warns[0]

    # steady warn on the next checkin must NOT add another entry
    checkin_with("warn", 92.5, "185.0 GB")
    ledger = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/ledger", headers=headers))
    assert sum(1 for e in ledger if "storage watch WARN: /scratch" in e) == 1

    # clearing ledgers once
    checkin_with("ok", 61.0, "122.0 GB")
    ledger = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/ledger", headers=headers))
    assert sum(1 for e in ledger if "storage watch cleared: /scratch" in e) == 1

    # steady ok stays quiet
    checkin_with("ok", 60.0, "120.0 GB")
    ledger = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/ledger", headers=headers))
    assert sum(1 for e in ledger if "storage watch cleared: /scratch" in e) == 1
