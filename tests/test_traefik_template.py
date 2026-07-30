"""
Render tests for service/templates/traefik-template.j2.

The template's whitespace/indentation is fragile (jinja block-trim markers inside YAML):
a bad edit can render a config that YAML-parses to null middlewares or mis-nested
routers, and traefik rejects the WHOLE dynamic config file over it (every router 404s).
These tests render the template exactly like kubernetes_utils.update_traefik_configmap
does (default jinja2 Environment) with representative http/tcp/postgres/node-route
inputs and assert on the PARSED structure, so template edits fail here instead of in a
live deployment.

Standalone on purpose — no service imports, so it runs with plain pytest anywhere
(in-container via `make test` or on a dev box with jinja2+pyyaml).
"""

import os

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), '..', 'service', 'templates')


def render(http=None, tcp=None, postgres=None):
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template('traefik-template.j2')
    rendered = template.render(
        tcp_proxy_info=tcp or {},
        http_proxy_info=http or {},
        postgres_proxy_info=postgres or {})
    parsed = yaml.safe_load(rendered)
    assert parsed is not None, "template rendered to empty/unparseable YAML"
    return rendered, parsed


def http_pod_entry(**overrides):
    """An http_proxy_info entry shaped exactly like health_central builds for a pod."""
    entry = {
        "routing_port": 5000,
        "url": "mypod.pods.tacc.develop.tapis.io",
        "k8_service": "pods-tacc-tacc-mypod",
        "splash_mode": False,
        "ip_allow_list": [],
        "proxy_compression": True,
        "proxy_compression_encodings": ["zstd", "br", "gzip"],
        "proxy_compression_excluded_content_types": ["image/png"],
        "proxy_compression_min_response_body_bytes": 1024,
        "custom_domain": "",
        "custom_domain_verified": False,
    }
    entry.update(overrides)
    return entry


def route_entry(**overrides):
    """An http_proxy_info entry shaped exactly like health_central builds for a node
    route (publish v0): k8_service is an arbitrary reachable host, not a k8s service."""
    entry = {
        "routing_port": 8080,
        "url": "myroute.pods.tacc.develop.tapis.io",
        "k8_service": "host.minikube.internal",
        "splash_mode": False,
        "ip_allow_list": [],
        "proxy_compression": True,
        "proxy_compression_encodings": ["zstd", "br", "gzip"],
        "proxy_compression_excluded_content_types": ["image/png"],
        "proxy_compression_min_response_body_bytes": 1024,
        "custom_domain": "",
        "custom_domain_verified": False,
    }
    entry.update(overrides)
    return entry


# ---------------------------------------------------------------------------
# Base cases
# ---------------------------------------------------------------------------

def test_render_empty():
    _, parsed = render()
    # Static http skeleton always present
    assert "path-strip-v3" in parsed["http"]["middlewares"]
    assert "pods-service" in parsed["http"]["routers"]
    assert "catch-all-error-handler" in parsed["http"]["routers"]
    assert parsed["http"]["services"]["pods-service"]["loadBalancer"]["servers"][0]["url"] == "http://pods-api:8000"
    # No pods/routes -> no tcp section at all (a bare `tcp:`/`middlewares:` key is YAML
    # null and traefik rejects the whole file)
    assert "tcp" not in parsed
    assert parsed["http"]["middlewares"] is not None


def test_render_plain_http_pod():
    _, parsed = render(http={"pods-tacc-tacc-mypod": http_pod_entry()})
    router = parsed["http"]["routers"]["pods-tacc-tacc-mypod"]
    assert router["rule"] == "Host(`mypod.pods.tacc.develop.tapis.io`)"
    assert router["service"] == "pods-tacc-tacc-mypod"
    assert "priority" not in router
    # compression middleware attached, no auth/gate middlewares
    assert router["middlewares"] == ["tapis-compress-pods-tacc-tacc-mypod"]
    compress = parsed["http"]["middlewares"]["tapis-compress-pods-tacc-tacc-mypod"]["compress"]
    assert compress["encodings"] == ["zstd", "br", "gzip"]
    assert compress["minResponseBodyBytes"] == 1024
    svc = parsed["http"]["services"]["pods-tacc-tacc-mypod"]["loadBalancer"]["servers"][0]
    assert svc["url"] == "http://pods-tacc-tacc-mypod:5000"


def test_render_splash_mode():
    entry = http_pod_entry(splash_mode=True, routing_port=8000, k8_service="pods-api")
    _, parsed = render(http={"pods-tacc-tacc-mypod": entry})
    router = parsed["http"]["routers"]["pods-tacc-tacc-mypod"]
    assert "tapis-splash-pods-tacc-tacc-mypod" in router["middlewares"]
    # splash rewrites to the pods-api splash endpoint
    splash = parsed["http"]["middlewares"]["tapis-splash-pods-tacc-tacc-mypod"]
    assert splash["replacePathRegex"]["replacement"] == "/pod-splash"


# ---------------------------------------------------------------------------
# tapis_auth
# ---------------------------------------------------------------------------

def test_render_tapis_auth_pod():
    entry = http_pod_entry(
        tapis_auth=True,
        auth_url="https://tacc.develop.tapis.io/v3/pods/mypod/auth",
        tapis_auth_response_headers={"X-Tapis-Username": "<<tapisusername>>"},
        tapis_auth_excluded_paths=[],
        tapis_auth_excluded_path_regex=[],
    )
    _, parsed = render(http={"pods-tacc-tacc-mypod": entry})
    auth_mw = parsed["http"]["middlewares"]["tapis-auth-pods-tacc-tacc-mypod"]["forwardAuth"]
    assert auth_mw["address"] == "https://tacc.develop.tapis.io/v3/pods/mypod/auth"
    assert auth_mw["authResponseHeaders"] == ["X-Tapis-Username"]
    router = parsed["http"]["routers"]["pods-tacc-tacc-mypod"]
    assert "tapis-auth-pods-tacc-tacc-mypod" in router["middlewares"]
    # no exclusions -> single router, no -noauth sibling, no priority juggling
    assert "priority" not in router
    assert "pods-tacc-tacc-mypod-noauth" not in parsed["http"]["routers"]


def test_render_tapis_auth_with_exclusions():
    entry = http_pod_entry(
        tapis_auth=True,
        auth_url="https://tacc.develop.tapis.io/v3/pods/mypod/auth",
        tapis_auth_response_headers={},
        tapis_auth_excluded_paths=["/assets", "/static"],
        tapis_auth_excluded_path_regex=[r"\.(js|css)$"],
    )
    _, parsed = render(http={"pods-tacc-tacc-mypod": entry})
    main = parsed["http"]["routers"]["pods-tacc-tacc-mypod"]
    noauth = parsed["http"]["routers"]["pods-tacc-tacc-mypod-noauth"]
    # excluded-path router wins on priority and carries NO auth middleware
    assert main["priority"] == 1
    assert noauth["priority"] == 100
    assert "PathPrefix(`/assets`)" in noauth["rule"]
    assert "PathPrefix(`/static`)" in noauth["rule"]
    assert "PathRegexp(" in noauth["rule"]
    assert noauth["service"] == "pods-tacc-tacc-mypod"
    assert not any("tapis-auth" in m for m in noauth.get("middlewares", []))
    assert any("tapis-auth" in m for m in main["middlewares"])


# ---------------------------------------------------------------------------
# tcp / postgres — the historical whitespace hazard (bare `middlewares:` = YAML null)
# ---------------------------------------------------------------------------

def test_render_postgres_only():
    entry = {
        "routing_port": 5432,
        "url": "mydb.pods.tacc.develop.tapis.io",
        "k8_service": "pods-tacc-tacc-mydb",
        "ip_allow_list": [],
    }
    _, parsed = render(postgres={"pods-tacc-tacc-mydb": entry})
    router = parsed["tcp"]["routers"]["pods-tacc-tacc-mydb"]
    assert router["rule"] == "HostSNI(`mydb.pods.tacc.develop.tapis.io`)"
    assert router["tls"]["passthrough"] is True
    # THE hazard: no ip allowlists -> tcp.middlewares key must be absent, not null
    assert parsed["tcp"].get("middlewares") is not None or "middlewares" not in parsed["tcp"]
    assert parsed["tcp"]["services"]["pods-tacc-tacc-mydb"]["loadBalancer"]["servers"][0]["address"] == "pods-tacc-tacc-mydb:5432"


def test_render_tcp_with_ip_allow_list():
    entry = {
        "routing_port": 6000,
        "url": "mytcp.pods.tacc.develop.tapis.io",
        "k8_service": "pods-tacc-tacc-mytcp",
        "ip_allow_list": ["127.0.0.1/32", "10.0.0.0/8"],
    }
    _, parsed = render(tcp={"pods-tacc-tacc-mytcp": entry})
    mw = parsed["tcp"]["middlewares"]["tapis-ipallowlist-pods-tacc-tacc-mytcp"]
    assert mw["ipAllowList"]["sourceRange"] == ["127.0.0.1/32", "10.0.0.0/8"]
    assert parsed["tcp"]["routers"]["pods-tacc-tacc-mytcp"]["middlewares"] == ["tapis-ipallowlist-pods-tacc-tacc-mytcp"]


# ---------------------------------------------------------------------------
# Node routes (publish v0) — arbitrary backend hosts through the same template
# ---------------------------------------------------------------------------

def test_render_node_route_plain():
    _, parsed = render(http={"pods-tacc-tacc-noderoute-myroute": route_entry()})
    router = parsed["http"]["routers"]["pods-tacc-tacc-noderoute-myroute"]
    assert router["rule"] == "Host(`myroute.pods.tacc.develop.tapis.io`)"
    # backend is a plain reachable host, NOT a k8s service name
    svc = parsed["http"]["services"]["pods-tacc-tacc-noderoute-myroute"]["loadBalancer"]["servers"][0]
    assert svc["url"] == "http://host.minikube.internal:8080"


def test_render_node_route_tapis_auth():
    entry = route_entry(
        tapis_auth=True,
        auth_url="https://tacc.develop.tapis.io/v3/pods/routes/myroute/auth",
        tapis_auth_response_headers={"X-Tapis-Username": "<<tapisusername>>"},
        tapis_auth_excluded_paths=["/assets"],
        tapis_auth_excluded_path_regex=[],
    )
    _, parsed = render(http={"pods-tacc-tacc-noderoute-myroute": entry})
    auth_mw = parsed["http"]["middlewares"]["tapis-auth-pods-tacc-tacc-noderoute-myroute"]["forwardAuth"]
    # forwardAuth points at the ROUTE auth endpoint, not a pod's
    assert auth_mw["address"] == "https://tacc.develop.tapis.io/v3/pods/routes/myroute/auth"
    main = parsed["http"]["routers"]["pods-tacc-tacc-noderoute-myroute"]
    noauth = parsed["http"]["routers"]["pods-tacc-tacc-noderoute-myroute-noauth"]
    assert main["priority"] == 1
    assert noauth["priority"] == 100


def test_render_pod_and_route_coexist():
    http = {
        "pods-tacc-tacc-mypod": http_pod_entry(
            tapis_auth=True,
            auth_url="https://tacc.develop.tapis.io/v3/pods/mypod/auth",
            tapis_auth_response_headers={},
            tapis_auth_excluded_paths=[],
            tapis_auth_excluded_path_regex=[],
        ),
        "pods-tacc-tacc-noderoute-myroute": route_entry(),
    }
    _, parsed = render(http=http)
    routers = parsed["http"]["routers"]
    services = parsed["http"]["services"]
    assert "pods-tacc-tacc-mypod" in routers and "pods-tacc-tacc-noderoute-myroute" in routers
    assert services["pods-tacc-tacc-mypod"]["loadBalancer"]["servers"][0]["url"] == "http://pods-tacc-tacc-mypod:5000"
    assert services["pods-tacc-tacc-noderoute-myroute"]["loadBalancer"]["servers"][0]["url"] == "http://host.minikube.internal:8080"
    # only the authed pod grew an auth middleware
    assert "tapis-auth-pods-tacc-tacc-mypod" in parsed["http"]["middlewares"]
    assert "tapis-auth-pods-tacc-tacc-noderoute-myroute" not in parsed["http"]["middlewares"]
