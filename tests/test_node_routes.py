"""
Node routes (publish v0) — API integration tests.

Covers the route CRUD surface on /pods/nodes/{node_id}/routes: create with defaults,
the full per-route tapis_auth config roundtrip, duplicate/validation rejections, list,
get, delete, and the node-delete cascade. The traefik rendering side is covered by
tests/test_traefik_template.py (pure render tests); the health_central pass picks
routes up out-of-process.
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

NODE_ID = "routetestnode"


@pytest.fixture(scope="module", autouse=True)
def test_node(headers):
    """Create the node routes hang off of; delete it (and any leftover routes, via the
    cascade) when the module is done."""
    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)  # clean slate if a prior run died
    rsp = client.post(
        "/pods/nodes",
        data=json.dumps({"node_id": NODE_ID, "type": "host", "description": "route test node"}),
        headers=headers)
    result = basic_response_checks(rsp)
    assert result["node"]["node_id"] == NODE_ID
    yield
    client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)


def create_route(hdrs, body):
    return client.post(f"/pods/nodes/{NODE_ID}/routes", data=json.dumps(body), headers=hdrs)


def test_create_route_defaults(headers):
    rsp = create_route(headers, {
        "route_id": "routetest-alpha",
        "port": 8137,
        "backend_host": "192.168.49.1",
    })
    result = basic_response_checks(rsp)
    assert result["route_id"] == "routetest-alpha"
    assert result["node_id"] == NODE_ID
    assert result["port"] == 8137
    assert result["backend_host"] == "192.168.49.1"
    # url is service-generated from the tenant base url
    assert result["url"].startswith("routetest-alpha.pods.")
    # secure-by-default: tapis_auth defaults off but allowed_users default is USER+
    assert result["tapis_auth"] is False
    assert result["tapis_auth_allowed_users"] == ["AUTHORIZED_USERS"]


def test_create_route_full_auth_config(headers):
    rsp = create_route(headers, {
        "route_id": "routetest-authed",
        "port": 9000,
        "backend_host": "10.4.0.7",
        "description": "authed test route",
        "tapis_auth": True,
        "tapis_auth_allowed_users": ["AUTHORIZED_ADMINS", "someguest"],
        "tapis_auth_response_headers": {"X-Tapis-Username": "<<tapisusername>>"},
        "tapis_auth_return_path": "/app",
        "tapis_auth_excluded_paths": ["/assets"],
        "tapis_auth_excluded_path_regex": [r"\.(js|css)$"],
    })
    result = basic_response_checks(rsp)
    assert result["tapis_auth"] is True
    assert result["tapis_auth_allowed_users"] == ["AUTHORIZED_ADMINS", "someguest"]
    assert result["tapis_auth_response_headers"] == {"X-Tapis-Username": "<<tapisusername>>"}
    assert result["tapis_auth_return_path"] == "/app"
    assert result["tapis_auth_excluded_paths"] == ["/assets"]
    assert result["tapis_auth_excluded_path_regex"] == [r"\.(js|css)$"]


def test_create_route_duplicate_rejected(headers):
    rsp = create_route(headers, {
        "route_id": "routetest-alpha",
        "port": 1234,
        "backend_host": "192.168.49.1",
    })
    assert rsp.status_code == 400
    assert "already exists" in rsp.json()["message"]


def test_create_route_bad_backend_host_rejected(headers):
    for bad_host in ["http://myhost", "myhost:8000", "my host"]:
        rsp = create_route(headers, {
            "route_id": "routetest-badhost",
            "port": 1234,
            "backend_host": bad_host,
        })
        assert rsp.status_code != 200, f"backend_host '{bad_host}' should be rejected"


def test_create_route_bad_route_id_rejected(headers):
    for bad_id in ["ab", "UPPER", "9starts-with-digit", "has_underscore"]:
        rsp = create_route(headers, {
            "route_id": bad_id,
            "port": 1234,
            "backend_host": "192.168.49.1",
        })
        assert rsp.status_code != 200, f"route_id '{bad_id}' should be rejected"


def test_list_routes(headers):
    rsp = client.get(f"/pods/nodes/{NODE_ID}/routes", headers=headers)
    result = basic_response_checks(rsp)
    route_ids = {r["route_id"] for r in result}
    assert {"routetest-alpha", "routetest-authed"} <= route_ids


def test_get_route(headers):
    rsp = client.get(f"/pods/nodes/{NODE_ID}/routes/routetest-alpha", headers=headers)
    result = basic_response_checks(rsp)
    assert result["route_id"] == "routetest-alpha"


def test_get_route_wrong_node_404(headers):
    rsp = client.get("/pods/nodes/nosuchnode/routes/routetest-alpha", headers=headers)
    assert rsp.status_code == 404


def test_delete_route(headers):
    rsp = client.delete(f"/pods/nodes/{NODE_ID}/routes/routetest-authed", headers=headers)
    basic_response_checks(rsp)
    rsp = client.get(f"/pods/nodes/{NODE_ID}/routes", headers=headers)
    result = basic_response_checks(rsp)
    assert "routetest-authed" not in {r["route_id"] for r in result}


def test_probe_route_reachable_backend(headers):
    # pods-api itself is a backend central can always dial in-cluster — the direct leg
    # must connect (any HTTP status counts as reachable).
    rsp = create_route(headers, {
        "route_id": "routetest-probe",
        "port": 8000,
        "backend_host": "pods-api",
    })
    basic_response_checks(rsp)
    rsp = client.get(f"/pods/nodes/{NODE_ID}/routes/routetest-probe/probe", headers=headers)
    result = basic_response_checks(rsp)
    assert result["route_id"] == "routetest-probe"
    assert result["direct"]["ok"] is True
    assert isinstance(result["direct"]["status_code"], int)
    assert isinstance(result["direct"]["latency_ms"], int)
    # via_proxy just needs the full check shape — whether traefik has rendered this
    # transient route yet depends on health-pass timing.
    assert set(result["via_proxy"].keys()) == {"ok", "status_code", "latency_ms", "error", "snippet"}
    client.delete(f"/pods/nodes/{NODE_ID}/routes/routetest-probe", headers=headers)


def test_probe_route_unreachable_backend(headers):
    # 192.0.2.1 is TEST-NET-1 (RFC 5737) — guaranteed unroutable, so the direct leg
    # must fail with a connection error rather than hang or blow up.
    rsp = create_route(headers, {
        "route_id": "routetest-dead",
        "port": 9,
        "backend_host": "192.0.2.1",
    })
    basic_response_checks(rsp)
    rsp = client.get(f"/pods/nodes/{NODE_ID}/routes/routetest-dead/probe", headers=headers)
    result = basic_response_checks(rsp)
    assert result["direct"]["ok"] is False
    assert result["direct"]["error"]
    client.delete(f"/pods/nodes/{NODE_ID}/routes/routetest-dead", headers=headers)


def test_probe_missing_route_404(headers):
    rsp = client.get(f"/pods/nodes/{NODE_ID}/routes/nosuchroute/probe", headers=headers)
    assert rsp.status_code == 404


def test_traefik_config_alias_admin_gated(headers, regular_headers):
    # /pods/traefik-config discloses every pod/route hostname — a regular (non-admin)
    # user must be rejected; an admin gets the parsed dynamic config.
    rsp = client.get("/pods/traefik-config", headers=regular_headers)
    assert rsp.status_code not in (200, 201)
    rsp = client.get("/pods/traefik-config", headers=headers)
    assert rsp.status_code == 200
    assert "http" in rsp.json()


def test_node_delete_cascades_routes(headers):
    # routetest-alpha still exists on the node; deleting the node must take it along —
    # an orphaned route row would keep rendering into the proxy config forever.
    rsp = client.delete(f"/pods/nodes/{NODE_ID}", headers=headers)
    basic_response_checks(rsp)
    # Recreate the node (also keeps the module fixture teardown a clean no-op) and
    # verify no routes resurrected with it.
    rsp = client.post(
        "/pods/nodes",
        data=json.dumps({"node_id": NODE_ID, "type": "host", "description": "route test node"}),
        headers=headers)
    basic_response_checks(rsp)
    rsp = client.get(f"/pods/nodes/{NODE_ID}/routes", headers=headers)
    result = basic_response_checks(rsp)
    assert result == []


def test_non_admin_cannot_create_a_node(regular_headers):
    """Node creation is admin-gated (see create_node).

    NewNode carries login_server, which join uses as the target for a bearer request
    carrying the headscale ADMIN key — so until that field is validated against an
    allowlist, an open create would let any authenticated user harvest it. This is the
    negative test that was missing when the gate was added.
    """
    rsp = client.post(
        "/pods/nodes",
        data=json.dumps({"node_id": "routetest-nonadmin", "type": "host",
                         "description": "should be refused"}),
        headers=regular_headers)
    assert rsp.status_code == 403, f"expected 403 for non-admin node create, got {rsp.status_code}"
    assert "requires admin" in rsp.text


def test_internal_backend_guard_rejects_cluster_internal_hosts():
    """SSRF containment on publish, exercised directly against the guard.

    This previously ran end-to-end as a regular user on their own node. Node creation
    is now admin-gated and there are still no node permission endpoints, so a non-admin
    cannot own a node to publish from — the guard is unreachable through the API for the
    exact user class it targets until on-behalf-of minting lands (roadmap R28 + R8).
    Testing the guard directly keeps the containment covered rather than losing it.
    """
    from api_nodes import _backend_host_is_internal

    for bad in ("pods-api", "pods-api.pods.svc.cluster.local", "something.svc",
                "127.0.0.1", "169.254.169.254", "10.96.0.1", "192.168.5.5",
                "172.16.4.4", "100.64.1.1", "metadata.google.internal", "foo.internal"):
        assert _backend_host_is_internal(bad) is True, f"{bad!r} should be treated as internal"
    assert _backend_host_is_internal("8.8.8.8") is False


# ── login_server allowlist (R2) ──────────────────────────────────────────────
# Join sends the headscale ADMIN key (TS_API_KEY) as a bearer to login_server,
# so it must be exact-matched against operator-controlled values.

def test_login_server_default_allowed():
    import api_nodes
    api_nodes._check_login_server(api_nodes.DEFAULT_LOGIN_SERVER)
    api_nodes._check_login_server(api_nodes.DEFAULT_LOGIN_SERVER + "/")  # slash-normalized


def test_login_server_unknown_rejected_400():
    import api_nodes
    with pytest.raises(Exception) as ei:
        api_nodes._check_login_server("https://evil.example.com")
    assert getattr(ei.value, "code", None) == 400
    assert "not an allowed control plane" in str(ei.value)


def test_login_server_allowlist_extension(monkeypatch):
    import api_nodes
    monkeypatch.setattr(api_nodes, "LOGIN_SERVER_ALLOWLIST", "https://extra.example.com, https://two.example.com")
    api_nodes._check_login_server("https://extra.example.com/")
    api_nodes._check_login_server("https://two.example.com")
    with pytest.raises(Exception):
        api_nodes._check_login_server("https://three.example.com")
