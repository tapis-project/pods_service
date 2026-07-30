"""
Node telemetry (Phase 3) — API integration tests.

Covers the agent write paths (POST /pods/nodes/{node_id}/logs with identity+gzip
bodies, checkin metrics_samples with dedupe) and the user read paths (GET logs
with source/since/limit paging, GET metrics series/caps/window clamping), plus
agent-auth rejection and the node-delete telemetry cascade. Pure normalization/
decode/downsample logic is covered by tests/test_node_telemetry_utils.py.
"""

import gzip
import json

import pytest
from tests.test_utils import headers, regular_headers, response_format, basic_response_checks

# Allows us to import pods's modules.
import sys
sys.path.append('/home/tapis/service')

from api import api
from fastapi.testclient import TestClient

client = TestClient(api, base_url="https://dev.develop.tapis.io", raise_server_exceptions=False)

NODE_ID = "telemetrytestnode"

# Populated by the module fixture after join; agent calls need both.
AGENT = {"token": None, "tenant": None}


def agent_headers():
    return {"X-Pods-Node-Token": AGENT["token"], "X-Pods-Tenant": AGENT["tenant"]}


@pytest.fixture(scope="module", autouse=True)
def test_node(headers):
    """Create + JOIN the node telemetry hangs off of (join mints the agent token
    the write paths authenticate with); delete it when the module is done."""
    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)  # clean slate if a prior run died
    rsp = client.post(
        "/pods/nodes",
        data=json.dumps({"node_id": NODE_ID, "type": "host", "description": "telemetry test node"}),
        headers=headers)
    result = basic_response_checks(rsp)
    claim_token = result["claim_token"]
    # tenant is baked into the join one-liner — reuse it for agent headers
    AGENT["tenant"] = result["join_command"].split("--tenant")[-1].strip()

    join_rsp = client.post(
        f"/pods/nodes/{NODE_ID}/join",
        data=json.dumps({"claim_token": claim_token, "agent_version": "test-0.0"}),
        headers={"X-Pods-Tenant": AGENT["tenant"]})
    join_result = basic_response_checks(join_rsp)
    AGENT["token"] = join_result["node_token"]
    yield
    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)


def ship_logs(payload, extra_headers=None, raw_body=None):
    hdrs = agent_headers()
    if extra_headers:
        hdrs.update(extra_headers)
    body = raw_body if raw_body is not None else json.dumps(payload)
    return client.post(f"/pods/nodes/{NODE_ID}/logs", data=body, headers=hdrs)


# ── Log ingest (agent path) ──────────────────────────────────────────────────

def test_ingest_logs_identity():
    rsp = ship_logs({"entries": [
        {"source": "web", "ts": 1785412800, "line": "GET / 200"},
        {"source": "web", "ts": 1785412801, "line": "GET /x 404"},
        {"source": "agent", "ts": 1785412802, "line": "checkin ok"},
    ]})
    result = basic_response_checks(rsp)
    assert result["accepted"] == 3
    assert result["dropped"] == 0
    # retention block tells agents how to size buffers
    assert result["retention"]["max_rows_per_node"] > 0
    assert "gzip" in result["retention"]["encodings"]


def test_ingest_logs_gzip():
    raw = json.dumps({"entries": [
        {"source": "web", "ts": 1785412803, "line": "gzip shipped line"}]}).encode()
    rsp = ship_logs(None, extra_headers={"Content-Encoding": "gzip"},
                    raw_body=gzip.compress(raw))
    result = basic_response_checks(rsp)
    assert result["accepted"] == 1


def test_ingest_logs_counts_dropped_and_truncated():
    rsp = ship_logs({"entries": [
        {"source": "web", "ts": 1785412804, "line": "ok"},
        {"bad": "no line"},
    ]})
    result = basic_response_checks(rsp)
    assert result["accepted"] == 1
    assert result["dropped"] == 1


def test_ingest_logs_bad_encoding_rejected():
    rsp = ship_logs(None, extra_headers={"Content-Encoding": "br"}, raw_body=b"xx")
    assert rsp.status_code == 400
    data = response_format(rsp)
    assert "unsupported" in data["message"]


def test_ingest_logs_requires_agent_token():
    rsp = client.post(
        f"/pods/nodes/{NODE_ID}/logs",
        data=json.dumps({"entries": [{"line": "sneaky"}]}),
        headers={"X-Pods-Tenant": AGENT["tenant"], "X-Pods-Node-Token": "pna_wrong"})
    assert rsp.status_code == 403


# ── Log reads (user path) ────────────────────────────────────────────────────

def test_get_logs_oldest_first_with_sources(headers):
    rsp = client.get(f"/pods/nodes/{NODE_ID}/logs", headers=headers)
    result = basic_response_checks(rsp)
    lines = [e["line"] for e in result["entries"]]
    assert "GET / 200" in lines and "gzip shipped line" in lines
    # oldest-first within the page
    assert lines.index("GET / 200") < lines.index("gzip shipped line")
    assert sorted(result["sources"]) == ["agent", "web"]
    assert result["has_more"] is False


def test_get_logs_source_filter(headers):
    rsp = client.get(f"/pods/nodes/{NODE_ID}/logs", params={"source": "agent"}, headers=headers)
    result = basic_response_checks(rsp)
    assert result["entries"], "agent-source lines expected"
    assert all(e["source"] == "agent" for e in result["entries"])


def test_get_logs_limit_and_has_more(headers):
    rsp = client.get(f"/pods/nodes/{NODE_ID}/logs", params={"limit": 2}, headers=headers)
    result = basic_response_checks(rsp)
    assert len(result["entries"]) == 2
    assert result["has_more"] is True
    # the newest window is returned — page back with before=<oldest ts on page>
    oldest = result["entries"][0]["ts"]
    back = client.get(f"/pods/nodes/{NODE_ID}/logs", params={"limit": 2, "before": oldest}, headers=headers)
    back_result = basic_response_checks(back)
    assert all(e["ts"] < oldest for e in back_result["entries"])


def test_get_logs_since_epoch(headers):
    # strictly-after semantics: the ts==1785412803 line itself is excluded
    rsp = client.get(f"/pods/nodes/{NODE_ID}/logs", params={"since": "1785412803"}, headers=headers)
    result = basic_response_checks(rsp)
    assert {e["line"] for e in result["entries"]} == {"ok"}


def test_get_logs_bad_since_rejected(headers):
    rsp = client.get(f"/pods/nodes/{NODE_ID}/logs", params={"since": "not-a-time"}, headers=headers)
    assert rsp.status_code == 400


def test_get_logs_requires_read_permission(regular_headers):
    rsp = client.get(f"/pods/nodes/{NODE_ID}/logs", headers=regular_headers)
    assert rsp.status_code != 200


# ── Metrics via checkin (agent path) + reads (user path) ─────────────────────

def checkin_with_samples(samples):
    return client.post(
        f"/pods/nodes/{NODE_ID}/checkin",
        data=json.dumps({"metrics_samples": samples}),
        headers=agent_headers())


def test_checkin_metrics_and_read_series(headers):
    samples = [
        {"ts": 1785412800 + i * 60, "load1": 0.5 + i, "cpu_count": 8,
         "mem_used_bytes": 1_000_000_000 + i, "mem_total_bytes": 8_000_000_000,
         "root_disk_pct": 40.0, "docker_running": 2, "docker_total": 3}
        for i in range(5)
    ]
    result = basic_response_checks(checkin_with_samples(samples))
    assert "endpoints" in result
    # config-as-data: ingest target + encodings advertised every heartbeat
    assert result["endpoints"]["log_ingest"].endswith(f"/pods/nodes/{NODE_ID}/logs")
    assert "gzip" in result["endpoints"]["log_encodings"]

    # Window chosen to include the fixed sample epochs is not possible against
    # utcnow-based windows — instead read the widest window and check via sample_count.
    rsp = client.get(f"/pods/nodes/{NODE_ID}/metrics",
                     params={"window_s": 30 * 86400, "step_s": 60}, headers=headers)
    m = basic_response_checks(rsp)
    assert m["sample_count"] >= 5
    assert m["caps"]["cpu_count"] == 8
    assert m["caps"]["mem_total_bytes"] == 8_000_000_000
    assert m["series"]["load1"], "load1 series expected"
    assert m["start_ts"] < m["end_ts"]


def test_checkin_metrics_resend_dedupes(headers):
    samples = [{"ts": 1785412800, "load1": 0.5, "cpu_count": 8}]
    basic_response_checks(checkin_with_samples(samples))
    before = basic_response_checks(
        client.get(f"/pods/nodes/{NODE_ID}/metrics",
                   params={"window_s": 30 * 86400}, headers=headers))["sample_count"]
    basic_response_checks(checkin_with_samples(samples))   # lost-ack resend
    after = basic_response_checks(
        client.get(f"/pods/nodes/{NODE_ID}/metrics",
                   params={"window_s": 30 * 86400}, headers=headers))["sample_count"]
    assert after == before


def test_metrics_window_clamped(headers):
    rsp = client.get(f"/pods/nodes/{NODE_ID}/metrics",
                     params={"window_s": 999999999, "step_s": 1}, headers=headers)
    m = basic_response_checks(rsp)
    assert m["window_s"] == 30 * 86400
    assert m["window_s"] // m["step_s"] <= 500


def test_metrics_requires_read_permission(regular_headers):
    rsp = client.get(f"/pods/nodes/{NODE_ID}/metrics", headers=regular_headers)
    assert rsp.status_code != 200


# ── Delete cascade ───────────────────────────────────────────────────────────

def test_node_delete_cascades_telemetry(headers):
    """Delete the node, recreate it with the same id — old telemetry must be gone."""
    rsp = client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)
    basic_response_checks(rsp)
    rsp = client.post(
        "/pods/nodes",
        data=json.dumps({"node_id": NODE_ID, "type": "host", "description": "recreated"}),
        headers=headers)
    basic_response_checks(rsp)
    logs = basic_response_checks(client.get(f"/pods/nodes/{NODE_ID}/logs", headers=headers))
    assert logs["entries"] == [] and logs["sources"] == []
    metrics = basic_response_checks(
        client.get(f"/pods/nodes/{NODE_ID}/metrics", params={"window_s": 30 * 86400}, headers=headers))
    assert metrics["sample_count"] == 0
    # module fixture teardown deletes the recreated node
