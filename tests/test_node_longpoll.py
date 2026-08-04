"""
Long-poll commands — API integration tests: classic wait=0 back-compat,
settings piggyback on every response, the timed hold (empty queue waits ~N
seconds), early wake on command-insert and on settings change (via a second
thread; correctness rides the 1 s DB re-check, so these pass regardless of
in-process event delivery), and the wait clamp.
"""

import json
import threading
import time

import pytest
from tests.test_utils import headers, response_format, basic_response_checks

import sys
sys.path.append('/home/tapis/service')

from api import api
from fastapi.testclient import TestClient

client = TestClient(api, base_url="https://dev.develop.tapis.io", raise_server_exceptions=False)

NODE_ID = "longpolltestnode"
AGENT = {"token": None, "tenant": None}


def agent_headers():
    return {"X-Pods-Node-Token": AGENT["token"], "X-Pods-Tenant": AGENT["tenant"], "Content-Type": "application/json"}


@pytest.fixture(scope="module", autouse=True)
def test_node(headers):
    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)
    result = basic_response_checks(client.post(
        "/pods/nodes",
        data=json.dumps({"node_id": NODE_ID, "type": "host", "description": "long-poll test node"}),
        headers=headers))
    AGENT["tenant"] = result["join_command"].split("--tenant")[-1].strip()
    join = basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/join",
        data=json.dumps({"claim_token": result["claim_token"]}),
        headers={"X-Pods-Tenant": AGENT["tenant"], "Content-Type": "application/json"}))
    AGENT["token"] = join["node_token"]
    yield
    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)


def test_wait_zero_is_classic_and_carries_settings():
    t0 = time.monotonic()
    result = basic_response_checks(client.get(
        f"/pods/nodes/{NODE_ID}/commands", headers=agent_headers()))
    assert time.monotonic() - t0 < 2.0                     # no hold without wait
    assert result["commands"] == []
    assert isinstance(result["settings"], dict)            # piggyback always present


def test_checkin_advertises_commands_wait():
    result = basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/checkin", data=json.dumps({}), headers=agent_headers()))
    assert result["endpoints"]["commands_wait"] >= 1


def test_empty_hold_waits_then_returns_empty():
    t0 = time.monotonic()
    result = basic_response_checks(client.get(
        f"/pods/nodes/{NODE_ID}/commands?wait=2", headers=agent_headers()))
    elapsed = time.monotonic() - t0
    assert result["commands"] == []
    assert 1.5 <= elapsed <= 8, elapsed                    # held ~2s, not forever


def test_queued_command_returns_immediately_even_with_wait(headers):
    basic_response_checks(client.post(f"/pods/nodes/{NODE_ID}/restart", headers=headers))
    t0 = time.monotonic()
    result = basic_response_checks(client.get(
        f"/pods/nodes/{NODE_ID}/commands?wait=10", headers=agent_headers()))
    assert time.monotonic() - t0 < 2.0                     # no hold when work exists
    assert [c["type"] for c in result["commands"]] == ["restart"]
    # complete it so later tests start clean
    basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/commands/{result['commands'][0]['command_id']}/result",
        data=json.dumps({"status": "done", "result": {"detail": "test ack"}}),
        headers=agent_headers()))


def test_command_insert_wakes_held_poll(headers):
    def queue_later():
        time.sleep(1.5)
        client.post(f"/pods/nodes/{NODE_ID}/restart", headers=headers)

    t = threading.Thread(target=queue_later)
    t.start()
    t0 = time.monotonic()
    result = basic_response_checks(client.get(
        f"/pods/nodes/{NODE_ID}/commands?wait=15", headers=agent_headers()))
    elapsed = time.monotonic() - t0
    t.join()
    assert [c["type"] for c in result["commands"]] == ["restart"]
    # woke well before the 15s deadline (1.5s queue delay + <=1s DB re-check + slack)
    assert elapsed < 8, elapsed
    basic_response_checks(client.post(
        f"/pods/nodes/{NODE_ID}/commands/{result['commands'][0]['command_id']}/result",
        data=json.dumps({"status": "done", "result": {"detail": "test ack"}}),
        headers=agent_headers()))


def test_settings_change_wakes_held_poll_with_fresh_overlay(headers):
    def flip_later():
        time.sleep(1.5)
        client.put(f"/pods/nodes/{NODE_ID}/settings",
                   data=json.dumps({"logs_tail": 321}), headers=headers)

    t = threading.Thread(target=flip_later)
    t.start()
    t0 = time.monotonic()
    rsp = client.get(f"/pods/nodes/{NODE_ID}/commands?wait=15", headers=agent_headers())
    elapsed = time.monotonic() - t0
    t.join()
    result = basic_response_checks(rsp)
    assert elapsed < 8, elapsed                            # ended early on the change
    assert result["settings"]["logs_tail"] == 321          # fresh overlay in the response
    assert "Settings changed" in response_format(rsp)["message"]


def test_wait_is_clamped():
    t0 = time.monotonic()
    basic_response_checks(client.get(
        f"/pods/nodes/{NODE_ID}/commands?wait=9999", headers=agent_headers()))
    # clamped to NODES_COMMANDS_MAX_WAIT (default 20) — generous ceiling for CI
    assert time.monotonic() - t0 <= 30
