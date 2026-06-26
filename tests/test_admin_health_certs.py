"""
Unit tests for the cert/ACME aggregation in api_admin._check_certs().

Validates the ready/provisioning/failed/unknown rollup and the status severity rules
without touching a real database — Pod.db_get_all and SITE_TENANT_DICT are mocked.
"""
import sys
import pytest

sys.path.append('/home/tapis/service')

import api_admin
import models_pods
from codes import AVAILABLE, STOPPED


class FakeConf:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakePod:
    def __init__(self, pod_id, status, networking, creation_ts=None, start_instance_ts=None):
        self.pod_id = pod_id
        self.status = status
        self.networking = networking
        self.creation_ts = creation_ts
        self.start_instance_ts = start_instance_ts


def _http(url, **extra):
    base = {"protocol": "http", "port": 5000, "url": url}
    base.update(extra)
    return base


@pytest.fixture
def patch_env(monkeypatch):
    def _apply(pods, enabled=True):
        monkeypatch.setattr(api_admin, "SITE_TENANT_DICT", {"tacc": ["tacc"]})
        monkeypatch.setattr(api_admin, "conf", FakeConf(cert_splash_enabled=enabled))
        monkeypatch.setattr(models_pods.Pod, "db_get_all", staticmethod(lambda tenant, site: pods))
    return _apply


def test_acme_working_all_ready(patch_env):
    patch_env([
        FakePod("p1", AVAILABLE, {"default": _http("p1.pods.tacc.tapis.io", cert_ready=True, cert_state="ready")}),
        FakePod("p2", AVAILABLE, {"default": _http("p2.pods.tacc.tapis.io", cert_ready=True, cert_state="ready")}),
    ])
    r = api_admin._check_certs()
    assert r["status"] == "ok"
    assert r["acme_working"] is True
    assert r["counts"]["ready"] == 2


def test_local_no_acme_is_warning_not_error(patch_env):
    # Mimics local/dev: pods AVAILABLE but certs never provision.
    patch_env([
        FakePod("p1", AVAILABLE, {"default": _http("p1.pods.tacc.tapis.io", cert_ready=False, cert_state="provisioning")}),
        FakePod("p2", AVAILABLE, {"default": _http("p2.pods.tacc.tapis.io", cert_ready=False, cert_state="failed")}),
    ])
    r = api_admin._check_certs()
    assert r["status"] == "warning"          # never error for missing ACME
    assert r["acme_working"] is False
    assert r["counts"]["provisioning"] == 1
    assert r["counts"]["failed"] == 1


def test_partial_failure_when_some_ready(patch_env):
    patch_env([
        FakePod("p1", AVAILABLE, {"default": _http("p1.pods.tacc.tapis.io", cert_ready=True, cert_state="ready")}),
        FakePod("p2", AVAILABLE, {"default": _http("p2.pods.tacc.tapis.io", cert_ready=False, cert_state="failed")}),
    ])
    r = api_admin._check_certs()
    assert r["status"] == "warning"
    assert r["acme_working"] is True
    assert r["counts"]["failed"] == 1


def test_non_available_and_non_http_ignored(patch_env):
    patch_env([
        FakePod("stopped", STOPPED, {"default": _http("s.pods.tacc.tapis.io", cert_ready=False, cert_state="provisioning")}),
        FakePod("tcp", AVAILABLE, {"default": {"protocol": "tcp", "port": 5432, "url": "t.pods.tacc.tapis.io"}}),
    ])
    r = api_admin._check_certs()
    assert sum(r["counts"].values()) == 0
    assert r["status"] == "ok"  # nothing to check


def test_timings_computed_from_timestamps(patch_env):
    from datetime import datetime, timedelta
    created = datetime(2026, 6, 26, 12, 0, 0)
    available = created + timedelta(seconds=30)        # pod live 30s after create
    prov_started = available + timedelta(seconds=2)    # cert provisioning begins
    cert_ready = prov_started + timedelta(seconds=18)  # cert issued 18s later

    patch_env([
        FakePod(
            "p1", AVAILABLE,
            {"default": _http(
                "p1.pods.tacc.tapis.io", cert_ready=True, cert_state="ready",
                cert_provisioning_started_at=prov_started.isoformat(),
                cert_ready_at=cert_ready.isoformat(),
            )},
            creation_ts=created, start_instance_ts=available,
        ),
    ])
    r = api_admin._check_certs()
    s = r["sampled"][0]
    assert s["provisioning_seconds"] == 18.0          # prov_started → ready
    assert s["from_available_seconds"] == 20.0        # available → ready (2 + 18)
    assert s["from_create_seconds"] == 50.0           # create → ready (30 + 2 + 18)
    assert r["timings"]["provisioning_seconds"] == {"samples": 1, "min": 18.0, "avg": 18.0, "max": 18.0}
    assert r["timings"]["from_create_seconds"]["avg"] == 50.0
    assert "Avg issue 18.0s" in r["message"]


def test_disabled_short_circuits(patch_env):
    patch_env(
        [FakePod("p1", AVAILABLE, {"default": _http("p1.pods.tacc.tapis.io", cert_state="provisioning")})],
        enabled=False,
    )
    r = api_admin._check_certs()
    assert r["status"] == "ok"
    assert r["acme_working"] is None
    assert "disabled" in r["message"].lower()
