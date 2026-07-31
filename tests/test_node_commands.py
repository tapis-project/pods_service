"""
Node command dispatcher v1 + bench trigger — API integration tests.

Covers the full command lifecycle (user trigger -> sanitized queued command ->
exactly-once agent delivery -> agent result post -> run history), the
one-active-bench guard, result validation, permission gating, and the ingest
dry_run mode (timed, not stored). The suite the agent actually RUNS is covered
by tests/test_agent_bench.py (local).
"""

import json

import pytest
from tests.test_utils import headers, regular_headers, response_format, basic_response_checks

# Allows us to import pods's modules.
import sys
sys.path.append('/home/tapis/service')

from api import api
from fastapi.testclient import TestClient

client = TestClient(api, base_url="https://dev.develop.tapis.io", raise_server_exceptions=False)

NODE_ID = "commandtestnode"
AGENT = {"token": None, "tenant": None}
RUN = {"command_id": None}


def agent_headers():
    return {"X-Pods-Node-Token": AGENT["token"], "X-Pods-Tenant": AGENT["tenant"]}


@pytest.fixture(scope="module", autouse=True)
def test_node(headers):
    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)
    rsp = client.post(
        "/pods/nodes",
        data=json.dumps({"node_id": NODE_ID, "type": "host", "description": "command test node"}),
        headers=headers)
    result = basic_response_checks(rsp)
    AGENT["tenant"] = result["join_command"].split("--tenant")[-1].strip()
    join_rsp = client.post(
        f"/pods/nodes/{NODE_ID}/join",
        data=json.dumps({"claim_token": result["claim_token"]}),
        headers={"X-Pods-Tenant": AGENT["tenant"]})
    AGENT["token"] = basic_response_checks(join_rsp)["node_token"]
    yield
    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)


def test_trigger_bench_sanitizes(headers):
    rsp = client.post(
        f"/pods/nodes/{NODE_ID}/bench",
        data=json.dumps({"encodings": ["gzip:9", "brotli"], "probe_count": 999, "dry_run": True}),
        headers=headers)
    result = basic_response_checks(rsp)
    assert result["status"] == "queued"
    assert result["type"] == "bench"
    assert result["params"]["encodings"] == ["gzip:9"]     # brotli dropped
    assert result["params"]["probe_count"] == 50           # clamped
    assert result["params"]["dry_run"] is True
    RUN["command_id"] = result["command_id"]


def test_second_trigger_rejected_while_active(headers):
    rsp = client.post(f"/pods/nodes/{NODE_ID}/bench", data=json.dumps({}), headers=headers)
    assert rsp.status_code == 409


def test_agent_delivery_exactly_once():
    rsp = client.get(f"/pods/nodes/{NODE_ID}/commands", headers=agent_headers())
    result = basic_response_checks(rsp)
    cmds = result["commands"]
    assert len(cmds) == 1
    assert cmds[0]["command_id"] == RUN["command_id"]
    assert cmds[0]["type"] == "bench"
    assert cmds[0]["params"]["encodings"] == ["gzip:9"]
    # second poll: already delivered, nothing handed out again
    again = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/commands", headers=agent_headers()))
    assert again["commands"] == []


def test_agent_posts_result(headers):
    rsp = client.post(
        f"/pods/nodes/{NODE_ID}/commands/{RUN['command_id']}/result",
        data=json.dumps({"status": "done", "result": {"meta": {"duration_ms": 123}, "compression": []}}),
        headers=agent_headers())
    basic_response_checks(rsp)
    runs = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/bench", headers=headers))
    assert runs[0]["command_id"] == RUN["command_id"]
    assert runs[0]["status"] == "done"
    assert runs[0]["result"]["meta"]["duration_ms"] == 123
    assert runs[0]["completed_ts"]


def test_result_repost_rejected():
    rsp = client.post(
        f"/pods/nodes/{NODE_ID}/commands/{RUN['command_id']}/result",
        data=json.dumps({"status": "done", "result": {}}),
        headers=agent_headers())
    assert rsp.status_code == 404


def test_result_validation():
    # queue a fresh command to shoot bad results at
    rsp = client.post(f"/pods/nodes/{NODE_ID}/bench", data=json.dumps({}),
                      headers={**agent_headers()})
    assert rsp.status_code != 200  # agent headers are not user auth for bench


def test_oversized_result_rejected(headers):
    result = basic_response_checks(
        client.post(f"/pods/nodes/{NODE_ID}/bench", data=json.dumps({}), headers=headers))
    cid = result["command_id"]
    basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/commands", headers=agent_headers()))
    big = {"blob": "x" * (300 * 1024)}
    rsp = client.post(
        f"/pods/nodes/{NODE_ID}/commands/{cid}/result",
        data=json.dumps({"status": "done", "result": big}),
        headers=agent_headers())
    assert rsp.status_code == 400
    # bad status string also rejected
    rsp = client.post(
        f"/pods/nodes/{NODE_ID}/commands/{cid}/result",
        data=json.dumps({"status": "finished", "result": {}}),
        headers=agent_headers())
    assert rsp.status_code == 400
    # clean completion so later tests aren't blocked by the active guard
    rsp = client.post(
        f"/pods/nodes/{NODE_ID}/commands/{cid}/result",
        data=json.dumps({"status": "error", "result": {"error": "test cleanup"}}),
        headers=agent_headers())
    basic_response_checks(rsp)


def test_bench_requires_user_permission(regular_headers):
    rsp = client.post(f"/pods/nodes/{NODE_ID}/bench", data=json.dumps({}), headers=regular_headers)
    assert rsp.status_code != 200
    rsp = client.get(f"/pods/nodes/{NODE_ID}/bench", headers=regular_headers)
    assert rsp.status_code != 200


def test_ingest_dry_run_times_but_stores_nothing(headers):
    rsp = client.post(
        f"/pods/nodes/{NODE_ID}/logs?dry_run=true",
        data=json.dumps({"entries": [
            {"source": "bench", "ts": 1785412800, "line": "dry run line"}]}),
        headers=agent_headers())
    result = basic_response_checks(rsp)
    assert result["accepted"] == 1
    assert result["dry_run"] is True
    assert result["timings"]["decode_ms"] is not None
    assert result["timings"]["insert_ms"] is None          # nothing touched the DB
    assert result["timings"]["wire_bytes"] > 0
    logs = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/logs", headers=headers))
    assert logs["entries"] == []                           # truly not stored
