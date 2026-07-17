"""
Unit tests for metrics_backend — pluggable cpu/mem metrics with a hard
"degrade-never-crash" contract.

Covers: provider selection from config (none/metrics-server/grafana/unknown,
plus missing-config and config-error fallbacks), the safe wrappers that must never raise
(pod_usage_dict/bulk_usage_dict), PodMetrics.to_dict unit derivation, and backend_health.
No network or real config is touched — conf is mocked.
"""
import sys
from unittest.mock import patch

sys.path.append('/home/tapis/service')

import metrics_backend as mb


class FakeConf:
    def __init__(self, **kw):
        self._d = kw

    def get(self, key, default=None):
        return self._d.get(key, default)


def _build_with(**conf):
    # _build_provider_from_conf does `from tapisservice.config import conf` internally,
    # so patch the module attribute it re-reads on each call.
    with patch("tapisservice.config.conf", FakeConf(**conf)):
        return mb._build_provider_from_conf()


# ---- provider selection --------------------------------------------------

def test_default_is_none_provider():
    assert isinstance(_build_with(), mb.NoneProvider)
    assert isinstance(_build_with(metrics_backend="none"), mb.NoneProvider)

def test_metrics_server_is_stub_provider():
    assert isinstance(_build_with(metrics_backend="metrics-server"), mb.MetricsServerProvider)

def test_grafana_missing_config_degrades_to_misconfigured():
    p = _build_with(metrics_backend="grafana")
    assert isinstance(p, mb.NoneProvider)          # _misconfigured returns a NoneProvider
    assert "missing config" in p._MSG

def test_grafana_full_config_builds_grafana_provider():
    p = _build_with(metrics_backend="grafana", grafana_url="http://g", grafana_token="t",
                    grafana_prom_ds_uid="uid", metrics_k8s_namespace="ns")
    assert isinstance(p, mb.GrafanaProvider)

def test_removed_prometheus_backend_degrades_not_crashes():
    # direct-Prometheus was removed; configs still naming it must degrade cleanly.
    p = _build_with(metrics_backend="prometheus", prometheus_url="http://p", metrics_k8s_namespace="ns")
    assert isinstance(p, mb.NoneProvider)
    assert "unknown metrics_backend" in p._MSG

def test_unknown_backend_degrades():
    p = _build_with(metrics_backend="bogus")
    assert isinstance(p, mb.NoneProvider)
    assert "unknown metrics_backend" in p._MSG

def test_build_never_raises_on_config_error():
    class BoomConf:
        def get(self, *a, **k):
            raise RuntimeError("config exploded")
    with patch("tapisservice.config.conf", BoomConf()):
        p = mb._build_provider_from_conf()
    assert isinstance(p, mb.NoneProvider)
    assert "config error" in p._MSG


# ---- degrade-never-crash safe wrappers -----------------------------------

def test_pod_usage_dict_none_provider_is_unavailable_not_raise():
    d = mb.pod_usage_dict("pods-x", provider=mb.NoneProvider())
    assert "unavailable" in d
    assert "metrics_backend=none" in d["unavailable"]

def test_pod_usage_dict_swallows_unexpected_exception():
    class BoomProvider(mb.NoneProvider):
        def get_pod(self, k8_name):
            raise ValueError("kaboom")
    d = mb.pod_usage_dict("pods-x", provider=BoomProvider())
    assert "unavailable" in d
    assert "kaboom" in d["unavailable"]

def test_bulk_usage_dict_none_provider_is_unavailable():
    d = mb.bulk_usage_dict("pods-.*", provider=mb.NoneProvider())
    assert "unavailable" in d


# ---- PodMetrics.to_dict --------------------------------------------------

def test_pod_metrics_to_dict_derives_friendly_units():
    pm = mb.PodMetrics(cpu_cores=0.5, mem_bytes=1024 * 1024 * 100, source="grafana", ts=123.0)
    d = pm.to_dict()
    assert d["cpu_cores"] == 0.5
    assert d["cpu_m"] == 500.0        # cores * 1000
    assert d["mem_mb"] == 100.0       # bytes / MiB
    assert d["source"] == "grafana" and d["ts"] == 123.0

def test_pod_metrics_to_dict_handles_none():
    d = mb.PodMetrics(cpu_cores=None, mem_bytes=None).to_dict()
    assert d["cpu_cores"] is None and d["cpu_m"] is None
    assert d["mem_bytes"] is None and d["mem_mb"] is None


# ---- backend_health ------------------------------------------------------

def test_backend_health_none_provider_is_configured_false():
    h = mb.backend_health(provider=mb.NoneProvider())
    assert h["backend"] == "none"
    assert h["configured"] is False
    assert h["reachable"] is False
