"""
Unit tests for the status-aware pod landing page (api_misc._render_pod_status).

Verifies hostname parsing, status->kind mapping, and that a stopped/errored/finished pod
renders the right page (not a misleading "starting up…"). The DB lookup is mocked.
"""
import sys
import pytest

sys.path.append('/home/tapis/service')

import api_misc
import models_pods


class FakeReq:
    def __init__(self, host):
        self.headers = {"x-forwarded-host": host}


class FakePod:
    def __init__(self, status):
        self.status = status


def _mock_lookup(monkeypatch, by_id):
    """by_id: dict pod_id -> status (or missing => None)."""
    def _get(pk_id, tenant=None, site=None):
        st = by_id.get(pk_id)
        return FakePod(st) if st else None
    monkeypatch.setattr(models_pods.Pod, "db_get_with_pk", staticmethod(_get))


def test_kind_mapping():
    k = api_misc._kind_for_pod_status
    assert k(None) == "notfound"
    assert k("AVAILABLE") == "securing"
    assert k("STOPPED") == "stopped"
    assert k("OFF") == "stopped"
    assert k("ERROR") == "error"
    assert k("COMPLETE") == "complete"
    assert k("DELETING") == "deleting"
    assert k("REQUESTED") == "starting"
    assert k("SPAWNER SETUP") == "starting"
    assert k("CREATING") == "starting"
    assert k("anything-weird") == "starting"


def test_host_parsing(monkeypatch):
    _mock_lookup(monkeypatch, {"mypod": "STOPPED"})
    # plain host
    assert api_misc._pod_status_from_host("mypod.pods.tacc.develop.tapis.io") == "STOPPED"
    # with networking suffix
    assert api_misc._pod_status_from_host("mypod-net2.pods.tacc.develop.tapis.io") == "STOPPED"
    # with port
    assert api_misc._pod_status_from_host("mypod.pods.tacc.develop.tapis.io:443") == "STOPPED"
    # unknown pod
    assert api_misc._pod_status_from_host("ghost.pods.tacc.develop.tapis.io") is None
    # non-pods host / unparseable
    assert api_misc._pod_status_from_host("custom.example.com") is None
    assert api_misc._pod_status_from_host("") is None


def test_lookup_swallows_db_errors(monkeypatch):
    def _boom(pk_id, tenant=None, site=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(models_pods.Pod, "db_get_with_pk", staticmethod(_boom))
    assert api_misc._pod_status_from_host("mypod.pods.tacc.develop.tapis.io") is None


@pytest.mark.parametrize("status,code,title_contains,refreshes", [
    ("STOPPED", 503, "stopped", False),
    ("ERROR", 503, "failed to start", False),
    ("COMPLETE", 503, "finished", False),
    ("AVAILABLE", 503, "Finishing startup", True),
    ("CREATING", 503, "starting up", True),
    (None, 404, "No pod at this address", False),
])
def test_render_reflects_status(monkeypatch, status, code, title_contains, refreshes):
    _mock_lookup(monkeypatch, {"mypod": status} if status else {})
    resp = api_misc._render_pod_status(FakeReq("mypod.pods.tacc.develop.tapis.io"))
    assert resp.status_code == code
    body = resp.body.decode()
    assert title_contains in body
    assert ("http-equiv=\"refresh\"" in body) == refreshes
    assert ("Retry-After" in resp.headers) == refreshes
    # never leaks the pod id into the page (anonymized)
    assert "mypod" not in body
