"""
Integration tests for pod healthchecks.

Uses notchristiangarcia/testserver:fastapi — a minimal FastAPI image already
used in test_pods.py. The server exposes GET /health → 200 on port 5000,
which gives us a real HTTP readiness probe to test against.

Run inside the container:
    pytest tests/test_healthchecks.py --disable-pytest-warnings -v
    make test-healthchecks
"""
import json
import time
import pytest
import sys

from tests.test_utils import (
    headers,
    basic_response_checks,
    wait_for_pod_status,
)

sys.path.append('/home/tapis/service')
from api import api
from fastapi.testclient import TestClient

client = TestClient(api, base_url="https://dev.develop.tapis.io", raise_server_exceptions=False)

# ── test pod IDs ──────────────────────────────────────────────────────────────

POD_HTTP_HC    = "testshealthcheckhttp"     # HTTP GET readiness probe
POD_EXEC_HC    = "testshealthcheckexec"     # exec probe (cat /health)
POD_TCP_HC     = "testshealthchecktcp"      # TCP socket probe
POD_GATE_HC    = "testshealthcheckgate"     # networking_requires_ready=True

MINIMAL_IMAGE  = "notchristiangarcia/testserver:fastapi"

# ── teardown ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def teardown(headers):
    yield
    for pod_id in [POD_HTTP_HC, POD_EXEC_HC, POD_TCP_HC, POD_GATE_HC]:
        client.delete(f'/pods/{pod_id}', headers=headers)


# ── helpers ───────────────────────────────────────────────────────────────────

def create_pod(pod_id, healthchecks, headers):
    body = {
        "pod_id": pod_id,
        "image": MINIMAL_IMAGE,
        "description": "healthcheck integration test",
        "networking": {"default": {"port": 5000, "protocol": "http"}},
        "healthchecks": healthchecks,
    }
    rsp = client.post("/pods", data=json.dumps(body), headers=headers)
    return rsp


def get_pod(pod_id, headers):
    rsp = client.get(f'/pods/{pod_id}', headers=headers)
    return basic_response_checks(rsp)


# ── API-level tests: healthchecks stored and returned ────────────────────────

class TestHealthcheckStorage:
    """Verify that the API stores and returns healthcheck config correctly.
    These tests don't wait for the pod to reach AVAILABLE — they only check
    that the API round-trips the healthchecks object correctly.
    """

    def test_http_healthcheck_stored(self, headers):
        hc = {
            "readiness": {
                "http_get_path": "/health",
                "http_get_port": 5000,
                "http_get_scheme": "HTTP",
                "initial_delay_seconds": 5,
                "period_seconds": 5,
                "timeout_seconds": 3,
                "failure_threshold": 3,
                "success_threshold": 1,
            },
            "networking_requires_ready": True,
        }
        rsp = create_pod(POD_HTTP_HC, hc, headers)
        result = basic_response_checks(rsp)
        assert result['pod_id'] == POD_HTTP_HC

        # Fetch and verify healthchecks round-trip
        pod = get_pod(POD_HTTP_HC, headers)
        assert pod['healthchecks'] is not None
        r = pod['healthchecks']['readiness']
        assert r['http_get_path'] == '/health'
        assert r['http_get_port'] == 5000
        assert r['http_get_scheme'] == 'HTTP'
        assert r['initial_delay_seconds'] == 5
        assert r['period_seconds'] == 5
        assert r['failure_threshold'] == 3

    def test_networking_requires_ready_stored(self, headers):
        pod = get_pod(POD_HTTP_HC, headers)
        assert pod['healthchecks']['networking_requires_ready'] is True

    def test_networking_live_starts_false(self, headers):
        """Pod just created — not yet AVAILABLE, so networking_live must be False."""
        pod = get_pod(POD_HTTP_HC, headers)
        assert pod.get('networking_live') is False

    def test_exec_healthcheck_stored(self, headers):
        hc = {
            "liveness": {
                "exec_command": ["cat", "/proc/1/status"],
                "initial_delay_seconds": 10,
                "period_seconds": 10,
            },
        }
        rsp = create_pod(POD_EXEC_HC, hc, headers)
        result = basic_response_checks(rsp)
        assert result['pod_id'] == POD_EXEC_HC

        pod = get_pod(POD_EXEC_HC, headers)
        assert pod['healthchecks']['liveness'] is not None
        lv = pod['healthchecks']['liveness']
        assert lv['exec_command'] == ['cat', '/proc/1/status']
        assert pod['healthchecks'].get('readiness') is None

    def test_tcp_healthcheck_stored(self, headers):
        hc = {
            "readiness": {
                "tcp_socket_port": 5000,
                "initial_delay_seconds": 5,
            },
            "networking_requires_ready": False,
        }
        rsp = create_pod(POD_TCP_HC, hc, headers)
        result = basic_response_checks(rsp)
        assert result['pod_id'] == POD_TCP_HC

        pod = get_pod(POD_TCP_HC, headers)
        r = pod['healthchecks']['readiness']
        assert r['tcp_socket_port'] == 5000
        # networking_requires_ready=False means networking_live won't gate traffic
        assert pod['healthchecks']['networking_requires_ready'] is False

    def test_null_healthchecks_stored(self, headers):
        """A pod without healthchecks should have healthchecks=null and networking_live=False."""
        body = {
            "pod_id": POD_GATE_HC,
            "image": MINIMAL_IMAGE,
            "description": "no healthchecks — networking_live should still default false",
        }
        rsp = client.post("/pods", data=json.dumps(body), headers=headers)
        result = basic_response_checks(rsp)
        assert result['pod_id'] == POD_GATE_HC

        pod = get_pod(POD_GATE_HC, headers)
        assert pod.get('healthchecks') is None
        assert pod.get('networking_live') is False

    def test_all_three_probes_stored(self, headers):
        """Patch an existing pod to add all three probe types and verify round-trip."""
        hc = {
            "liveness":  {"http_get_path": "/health", "http_get_port": 5000},
            "readiness": {"http_get_path": "/ready",  "http_get_port": 5000},
            "startup":   {"exec_command": ["cat", "/tmp/started"]},
            "networking_requires_ready": True,
        }
        rsp = client.put(f'/pods/{POD_HTTP_HC}',
                         data=json.dumps({"healthchecks": hc}),
                         headers=headers)
        result = basic_response_checks(rsp)
        pod = get_pod(POD_HTTP_HC, headers)
        stored = pod['healthchecks']
        assert stored['liveness']['http_get_path'] == '/health'
        assert stored['readiness']['http_get_path'] == '/ready'
        assert stored['startup']['exec_command'] == ['cat', '/tmp/started']

    def test_healthchecks_cleared_by_null(self, headers):
        """Setting healthchecks=null removes the probe config."""
        rsp = client.put(f'/pods/{POD_HTTP_HC}',
                         data=json.dumps({"healthchecks": None}),
                         headers=headers)
        basic_response_checks(rsp)
        pod = get_pod(POD_HTTP_HC, headers)
        assert pod.get('healthchecks') is None


# ── validation tests ──────────────────────────────────────────────────────────

class TestHealthcheckValidation:
    """Verify the API rejects invalid healthcheck configurations."""

    def test_invalid_healthcheck_field_rejected(self, headers):
        """Passing an unknown top-level field in healthchecks should fail."""
        hc = {"readiness": {"unknown_field": "bad"}}
        # PodHealthchecks has extra='forbid' — unknown fields should error
        body = {
            "pod_id": "shouldfailvalidation",
            "image": MINIMAL_IMAGE,
            "healthchecks": hc,
        }
        rsp = client.post("/pods", data=json.dumps(body), headers=headers)
        # Should be 422 Unprocessable Entity or 400
        assert rsp.status_code in (400, 422), f"Expected 4xx, got {rsp.status_code}"

    def test_networking_requires_ready_default_is_true(self, headers):
        """When networking_requires_ready is not set, it should default to True."""
        hc = {"readiness": {"http_get_path": "/health", "http_get_port": 5000}}
        pod = get_pod(POD_HTTP_HC, headers)
        # After the earlier test_null_healthchecks_stored cleared it, re-create via PUT
        rsp = client.put(f'/pods/{POD_HTTP_HC}',
                         data=json.dumps({"healthchecks": hc}),
                         headers=headers)
        basic_response_checks(rsp)
        pod = get_pod(POD_HTTP_HC, headers)
        assert pod['healthchecks']['networking_requires_ready'] is True
