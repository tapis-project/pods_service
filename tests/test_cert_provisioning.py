"""
Unit tests for the TLS certificate state machine in health_central.py.

Covers _evaluate_http_cert() (VISIBILITY ONLY — never affects routing):
- invalid -> provisioning -> ready transitions, each logged exactly once (de-dup)
- once ready, stays ready with no further probing/logging
- max-wait fallback flips 'provisioning' -> 'failed'
- 'unreachable' leaves state untouched (no false 'provisioning', no log, no divert)
- probe throttling (interval) skips re-probing
- cert_splash_enabled=False short-circuits

The probe itself (_probe_cert_status, a real TLS handshake) is monkeypatched to return
one of 'ready' | 'invalid' | 'unreachable'.
"""
import sys
import pytest

# Allows us to import pods's modules.
sys.path.append('/home/tapis/service')

import health_central


class FakeConf:
    """Stand-in for tapisservice's Config (whose attrs can't be delattr'd by monkeypatch).
    Replacing the module-level `conf` reference is clean and fully restorable."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


_DEFAULT_KNOBS = dict(
    cert_splash_enabled=True,
    cert_probe_interval_seconds=6,
    cert_probe_timeout_seconds=4,
    cert_provisioning_max_seconds=180,
)


class FakePod:
    """Minimal stand-in for a Pod row: records db_update log lines and networking writes."""
    def __init__(self, networking):
        self.pod_id = "certtestpod"
        self.tenant_id = "tacc"
        self.site_id = "tacc"
        self.networking = networking
        self.logs = []

    def db_update(self, log=None, tenant=None, site=None, user_update=True):
        self.logs.append(log)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Clear module-level throttle/state and force deterministic config knobs."""
    health_central._cert_probe_last.clear()
    health_central._cert_provision_started.clear()
    monkeypatch.setattr(health_central, "conf", FakeConf(**_DEFAULT_KNOBS))
    yield


def _net(url="certtestpod.pods.tacc.tapis.io", **extra):
    base = {"protocol": "http", "port": 5000, "url": url}
    base.update(extra)
    return base


def _set_probe(monkeypatch, result):
    """result is a status string ('ready'|'invalid'|'unreachable') or a list consumed per call."""
    if isinstance(result, list):
        seq = iter(result)
        monkeypatch.setattr(health_central, "_probe_cert_status", lambda host, timeout: next(seq))
    else:
        monkeypatch.setattr(health_central, "_probe_cert_status", lambda host, timeout: result)


def _eval(pod, t):
    return health_central._evaluate_http_cert(pod, "default", pod.networking["default"], t)


def test_provisioning_then_ready_logs_once_each(monkeypatch):
    pod = FakePod({"default": _net()})
    _set_probe(monkeypatch, "invalid")

    # t=0: connected but cert untrusted -> provisioning, log once
    assert _eval(pod, 0.0) is False
    assert pod.networking["default"]["cert_state"] == "provisioning"
    assert pod.networking["default"]["cert_ready"] is False
    assert pod.logs == ["TLS certificate provisioning for certtestpod.pods.tacc.tapis.io (Let's Encrypt)…"]

    # t=10 (> interval): still invalid -> no duplicate log
    assert _eval(pod, 10.0) is False
    assert len(pod.logs) == 1  # de-duped

    # t=20: cert now valid -> ready, log once
    _set_probe(monkeypatch, "ready")
    assert _eval(pod, 20.0) is True
    assert pod.networking["default"]["cert_state"] == "ready"
    assert pod.networking["default"]["cert_ready"] is True
    assert pod.logs[-1] == "TLS certificate ready for certtestpod.pods.tacc.tapis.io"
    assert len(pod.logs) == 2


def test_unreachable_leaves_state_untouched(monkeypatch):
    """The key fix: can't-reach must NOT be mislabeled 'provisioning' (no log, no divert)."""
    pod = FakePod({"default": _net()})
    _set_probe(monkeypatch, "unreachable")
    assert _eval(pod, 0.0) is False
    assert "cert_state" not in pod.networking["default"]  # untouched
    assert pod.logs == []                                  # nothing logged
    # A reachable-but-ready probe later still resolves cleanly to ready.
    _set_probe(monkeypatch, "ready")
    assert _eval(pod, 10.0) is True
    assert pod.networking["default"]["cert_state"] == "ready"
    assert pod.logs == ["TLS certificate ready for certtestpod.pods.tacc.tapis.io"]


def test_idempotent_already_good_cert_goes_straight_to_ready(monkeypatch):
    """Pre-existing pod whose cert already works: first reachable probe = ready, no 'provisioning'."""
    pod = FakePod({"default": _net()})
    _set_probe(monkeypatch, "ready")
    assert _eval(pod, 0.0) is True
    assert pod.networking["default"]["cert_state"] == "ready"
    assert pod.logs == ["TLS certificate ready for certtestpod.pods.tacc.tapis.io"]


def test_ready_short_circuits_without_probing(monkeypatch):
    pod = FakePod({"default": _net(cert_ready=True, cert_state="ready")})

    def _boom(host, timeout):
        raise AssertionError("should not probe an already-ready cert")

    monkeypatch.setattr(health_central, "_probe_cert_status", _boom)
    assert _eval(pod, 100.0) is True
    assert pod.logs == []


def test_max_wait_fallback_to_failed(monkeypatch):
    pod = FakePod({"default": _net()})
    _set_probe(monkeypatch, "invalid")

    _eval(pod, 0.0)
    assert pod.networking["default"]["cert_state"] == "provisioning"

    # Past max_wait (180s) and still invalid -> failed.
    assert _eval(pod, 200.0) is False
    assert pod.networking["default"]["cert_state"] == "failed"
    assert "still not verified" in pod.logs[-1]


def test_throttle_skips_reprobe(monkeypatch):
    pod = FakePod({"default": _net()})
    calls = {"n": 0}

    def _probe(host, timeout):
        calls["n"] += 1
        return "invalid"

    monkeypatch.setattr(health_central, "_probe_cert_status", _probe)

    _eval(pod, 0.0)
    assert calls["n"] == 1
    # Within the 6s interval -> no new probe
    assert _eval(pod, 3.0) is False
    assert calls["n"] == 1


def test_disabled_short_circuits(monkeypatch):
    monkeypatch.setattr(health_central, "conf", FakeConf(**{**_DEFAULT_KNOBS, "cert_splash_enabled": False}))
    pod = FakePod({"default": _net()})

    def _boom(host, timeout):
        raise AssertionError("should not probe when cert_splash_enabled is False")

    monkeypatch.setattr(health_central, "_probe_cert_status", _boom)
    assert _eval(pod, 0.0) is True
    assert pod.logs == []


# ── _prewarm_cert (BYOD custom-domain pre-warm) ────────────────────────────────

def test_prewarm_fires_then_throttles(monkeypatch):
    hits = []
    monkeypatch.setattr(health_central, "_probe_cert_status", lambda h, t: hits.append(h))
    health_central._prewarm_cert("pod1", "default:custom", "app.example.com", 1000.0)
    assert hits == ["app.example.com"]
    # within interval (6s) → throttled, no second handshake
    health_central._prewarm_cert("pod1", "default:custom", "app.example.com", 1003.0)
    assert hits == ["app.example.com"]
    # past interval → fires again
    health_central._prewarm_cert("pod1", "default:custom", "app.example.com", 1010.0)
    assert hits == ["app.example.com", "app.example.com"]


def test_prewarm_noops_on_empty_or_disabled(monkeypatch):
    hits = []
    monkeypatch.setattr(health_central, "_probe_cert_status", lambda h, t: hits.append(h))
    health_central._prewarm_cert("pod1", "k", "", 1000.0)            # empty hostname
    assert hits == []
    monkeypatch.setattr(health_central, "conf", FakeConf(**{**_DEFAULT_KNOBS, "cert_splash_enabled": False}))
    health_central._prewarm_cert("pod1", "k", "app.example.com", 1000.0)  # disabled
    assert hits == []
