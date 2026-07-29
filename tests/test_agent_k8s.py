"""
Unit tests for the agent's Kubernetes support (Phase 4.5): RBAC self-probe before use,
namespace resolution, in-cluster pod inventory mapping, and metrics-lite status.

Everything network-y is monkeypatched — no cluster needed. The agent lives in agent/
(not mounted into the pods-api container), so these tests skip in-container and run
locally: `pytest tests/test_agent_k8s.py` from the repo root.
"""

import os
import sys

import pytest

AGENT_DIR = os.path.join(os.path.dirname(__file__), "..", "agent")
if not os.path.exists(os.path.join(AGENT_DIR, "pods_agent.py")):
    pytest.skip("agent/ not present (in-container run) — agent tests run locally", allow_module_level=True)
sys.path.insert(0, AGENT_DIR)

import pods_agent as agent


@pytest.fixture(autouse=True)
def reset_probe_cache():
    agent._k8s_access_cache.update(at=0.0, allowed={})
    yield
    agent._k8s_access_cache.update(at=0.0, allowed={})


def in_cluster(monkeypatch, tmp_path, namespace="podns"):
    """Fake the in-cluster environment: SA dir + env vars."""
    sa = tmp_path / "sa"
    sa.mkdir()
    (sa / "token").write_text("fake-token")
    (sa / "ca.crt").write_text("fake-ca")
    (sa / "namespace").write_text(namespace)
    monkeypatch.setattr(agent, "K8S_SA_DIR", str(sa))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    return sa


# ── namespace resolution ──────────────────────────────────────────────────────

def test_namespace_prefers_central_advisory(monkeypatch, tmp_path):
    in_cluster(monkeypatch, tmp_path, namespace="sa-mounted")
    assert agent.k8s_namespace("from-join-config") == "from-join-config"


def test_namespace_falls_back_to_sa_mount_then_default(monkeypatch, tmp_path):
    in_cluster(monkeypatch, tmp_path, namespace="sa-mounted")
    assert agent.k8s_namespace(None) == "sa-mounted"
    monkeypatch.setattr(agent, "K8S_SA_DIR", str(tmp_path / "missing"))
    assert agent.k8s_namespace(None) == "default"


# ── RBAC self-probe (test the rules BEFORE use) ───────────────────────────────

def ssar_responder(allowed_map):
    """Return a fake k8s_api that answers SelfSubjectAccessReview from allowed_map
    keyed by (resource, subresource, verb)."""
    calls = []

    def fake_api(method, path, body=None, timeout=10):
        assert method == "POST" and "selfsubjectaccessreviews" in path
        attrs = body["spec"]["resourceAttributes"]
        key = (attrs["resource"], attrs.get("subresource", ""), attrs["verb"])
        calls.append(key)
        return {"status": {"allowed": allowed_map.get(key, False)}}

    fake_api.calls = calls
    return fake_api


def test_probe_access_reports_allowed_and_denied(monkeypatch, tmp_path):
    in_cluster(monkeypatch, tmp_path)
    monkeypatch.setattr(agent, "k8s_api", ssar_responder({
        ("pods", "", "list"): True,
        ("pods", "log", "get"): False,
    }))
    allowed = agent.k8s_probe_access("podns")
    assert allowed == {"pods.list": True, "pods/log.get": False}


def test_probe_access_is_cached(monkeypatch, tmp_path):
    in_cluster(monkeypatch, tmp_path)
    fake = ssar_responder({("pods", "", "list"): True, ("pods", "log", "get"): True})
    monkeypatch.setattr(agent, "k8s_api", fake)
    agent.k8s_probe_access("podns")
    first_calls = len(fake.calls)
    agent.k8s_probe_access("podns")
    assert len(fake.calls) == first_calls, "second probe within TTL must hit the cache"


def test_probe_access_error_means_denied(monkeypatch, tmp_path):
    in_cluster(monkeypatch, tmp_path)

    def exploding_api(method, path, body=None, timeout=10):
        raise OSError("api unreachable")

    monkeypatch.setattr(agent, "k8s_api", exploding_api)
    allowed = agent.k8s_probe_access("podns")
    assert allowed == {"pods.list": False, "pods/log.get": False}


# ── capabilities: granular k8s.* only when actually granted ───────────────────

def test_capabilities_include_probed_k8s_access(monkeypatch, tmp_path):
    in_cluster(monkeypatch, tmp_path)
    monkeypatch.setattr(agent, "docker_get", lambda *a, **k: None)
    monkeypatch.setattr(agent, "k8s_api", ssar_responder({
        ("pods", "", "list"): True,
        ("pods", "log", "get"): False,
    }))
    caps = agent.detect_capabilities()
    assert "runtime.k8s" in caps
    assert "k8s.pods.list" in caps
    assert "k8s.pods/log.get" not in caps


def test_capabilities_no_granular_when_not_in_cluster(monkeypatch):
    monkeypatch.setattr(agent, "docker_get", lambda *a, **k: None)
    monkeypatch.setattr(agent, "k8s_in_cluster", lambda: False)
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/kubectl")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    caps = agent.detect_capabilities()
    assert "runtime.k8s" in caps
    assert not any(c.startswith("k8s.") for c in caps)


# ── inventory ─────────────────────────────────────────────────────────────────

POD_LIST_RESPONSE = {
    "items": [
        {
            "metadata": {"name": "zeta"},
            "spec": {"containers": [{"image": "redis:7"}], "nodeName": "n1"},
            "status": {"phase": "Running",
                       "containerStatuses": [{"ready": True, "restartCount": 2}]},
        },
        {
            "metadata": {"name": "alpha"},
            "spec": {"containers": [{"image": "nginx:1"}, {"image": "sidecar:2"}], "nodeName": "n1"},
            "status": {"phase": "Pending", "containerStatuses": []},
        },
    ]
}


def test_k8s_inventory_pods_mapping_and_sort(monkeypatch, tmp_path):
    in_cluster(monkeypatch, tmp_path)
    monkeypatch.setattr(agent, "k8s_api", lambda *a, **k: POD_LIST_RESPONSE)
    pods = agent.k8s_inventory_pods("podns")
    assert [p["name"] for p in pods] == ["alpha", "zeta"]  # sorted for hash stability
    zeta = pods[1]
    assert zeta == {"name": "zeta", "phase": "Running", "images": ["redis:7"],
                    "ready": "1/1", "restarts": 2, "node": "n1"}
    assert pods[0]["images"] == ["nginx:1", "sidecar:2"]
    assert pods[0]["ready"] == "0/0"


def test_collect_inventory_gated_by_probed_capability(monkeypatch, tmp_path):
    in_cluster(monkeypatch, tmp_path)
    monkeypatch.setattr(agent, "k8s_api", lambda *a, **k: POD_LIST_RESPONSE)
    # without the granted capability: no k8s calls at all
    inv = agent.collect_inventory(["runtime.k8s"])
    assert "k8s_pods" not in inv
    # with it: namespace + pods land in the inventory
    inv = agent.collect_inventory(["runtime.k8s", "k8s.pods.list"])
    assert inv["k8s_namespace"] == "podns"
    assert len(inv["k8s_pods"]) == 2


def test_collect_inventory_survives_k8s_failure(monkeypatch, tmp_path):
    in_cluster(monkeypatch, tmp_path)

    def exploding_api(*a, **k):
        raise OSError("kaboom")

    monkeypatch.setattr(agent, "k8s_api", exploding_api)
    inv = agent.collect_inventory(["runtime.k8s", "k8s.pods.list"])
    assert "k8s_pods" not in inv and "k8s_namespace" not in inv


def test_inventory_hash_stable(monkeypatch, tmp_path):
    in_cluster(monkeypatch, tmp_path)
    monkeypatch.setattr(agent, "k8s_api", lambda *a, **k: POD_LIST_RESPONSE)
    inv1 = agent.collect_inventory(["runtime.k8s", "k8s.pods.list"])
    inv2 = agent.collect_inventory(["runtime.k8s", "k8s.pods.list"])
    assert agent.inventory_hash(inv1) == agent.inventory_hash(inv2)


# ── metrics-lite status ───────────────────────────────────────────────────────

def test_sample_status_metrics_lite(monkeypatch):
    monkeypatch.setattr(agent, "docker_get", lambda *a, **k: None)
    inv = {
        "docker_containers": [{"state": "running"}, {"state": "exited"}],
        "k8s_pods": [{"phase": "Running"}, {"phase": "Running"}, {"phase": "Pending"}],
    }
    status = agent.sample_status(["runtime.docker"], inv)
    assert status["docker_containers_running"] == 1
    assert status["docker_containers_total"] == 2
    assert status["k8s_pod_phases"] == {"Running": 2, "Pending": 1}
    # host samples exist on Linux
    if sys.platform.startswith("linux"):
        assert "load_avg" in status and "mem_total_mb" in status
