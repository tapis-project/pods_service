"""
Unit tests for healthcheck models (HealthcheckProbe, PodHealthchecks) and
the _build_k8_probe() helper in kubernetes_utils.

Run inside the container:
    pytest tests/test_healthcheck_models.py --disable-pytest-warnings -v

These are pure unit tests — no live service, database, or Kubernetes required.
"""
import sys
import pytest
from unittest.mock import patch, MagicMock

sys.path.append('/home/tapis/service')


# ── module-level patches so service-level imports don't need a live environment ──

# Patch out every module that touches DB, K8s, or RabbitMQ before importing ours.
_mocks = {
    'stores': MagicMock(),
    'tapisservice': MagicMock(),
    'tapisservice.logs': MagicMock(),
    'tapisservice.tapisfastapi': MagicMock(),
    'tapisservice.tapisfastapi.utils': MagicMock(),
    'tapisservice.config': MagicMock(),
    '__init__': MagicMock(),
    'codes': MagicMock(),
}
# Apply patches before any service module is imported.
for mod, mock in _mocks.items():
    sys.modules.setdefault(mod, mock)

# Mock get_logger to return a standard logger so it doesn't blow up.
import logging
sys.modules['tapisservice.logs'].get_logger = lambda name: logging.getLogger(name)


# ── HealthcheckProbe / PodHealthchecks ────────────────────────────────────────

class TestHealthcheckProbe:
    """Pydantic validation for HealthcheckProbe."""

    def _probe_class(self):
        from models_base import HealthcheckProbe
        return HealthcheckProbe

    def test_http_probe_minimal(self):
        HP = self._probe_class()
        p = HP(http_get_path='/health', http_get_port=5000)
        assert p.http_get_path == '/health'
        assert p.http_get_port == 5000
        assert p.http_get_scheme == 'HTTP'
        assert p.exec_command is None
        assert p.tcp_socket_port is None

    def test_http_probe_https_scheme(self):
        HP = self._probe_class()
        p = HP(http_get_path='/ready', http_get_port=443, http_get_scheme='HTTPS')
        assert p.http_get_scheme == 'HTTPS'

    def test_exec_probe(self):
        HP = self._probe_class()
        p = HP(exec_command=['curl', '-f', 'http://localhost/health'])
        assert p.exec_command == ['curl', '-f', 'http://localhost/health']
        assert p.http_get_path is None

    def test_tcp_probe(self):
        HP = self._probe_class()
        p = HP(tcp_socket_port=5432)
        assert p.tcp_socket_port == 5432
        assert p.exec_command is None
        assert p.http_get_path is None

    def test_timing_defaults(self):
        HP = self._probe_class()
        p = HP(http_get_path='/health', http_get_port=5000)
        assert p.initial_delay_seconds == 10
        assert p.period_seconds == 10
        assert p.timeout_seconds == 5
        assert p.failure_threshold == 3
        assert p.success_threshold == 1

    def test_custom_timing(self):
        HP = self._probe_class()
        p = HP(
            http_get_path='/health',
            http_get_port=8080,
            initial_delay_seconds=30,
            period_seconds=5,
            timeout_seconds=2,
            failure_threshold=5,
            success_threshold=2,
        )
        assert p.initial_delay_seconds == 30
        assert p.period_seconds == 5
        assert p.timeout_seconds == 2
        assert p.failure_threshold == 5
        assert p.success_threshold == 2

    def test_extra_fields_rejected(self):
        """extra='forbid' means unknown fields raise ValidationError."""
        from pydantic import ValidationError
        HP = self._probe_class()
        with pytest.raises((ValidationError, TypeError)):
            HP(http_get_path='/health', http_get_port=5000, unknown_field='bad')


class TestPodHealthchecks:
    """Pydantic validation for PodHealthchecks."""

    def _hc_class(self):
        from models_base import PodHealthchecks
        return PodHealthchecks

    def _probe_class(self):
        from models_base import HealthcheckProbe
        return HealthcheckProbe

    def test_empty_healthchecks(self):
        PHC = self._hc_class()
        hc = PHC()
        assert hc.liveness is None
        assert hc.readiness is None
        assert hc.startup is None
        assert hc.networking_requires_ready is True

    def test_networking_requires_ready_default_true(self):
        PHC = self._hc_class()
        hc = PHC()
        assert hc.networking_requires_ready is True

    def test_networking_requires_ready_false(self):
        PHC = self._hc_class()
        hc = PHC(networking_requires_ready=False)
        assert hc.networking_requires_ready is False

    def test_with_readiness_probe(self):
        HP = self._probe_class()
        PHC = self._hc_class()
        probe = HP(http_get_path='/ready', http_get_port=5000)
        hc = PHC(readiness=probe)
        assert hc.readiness is not None
        assert hc.readiness.http_get_path == '/ready'
        assert hc.liveness is None
        assert hc.startup is None

    def test_all_three_probes(self):
        HP = self._probe_class()
        PHC = self._hc_class()
        hc = PHC(
            liveness=HP(http_get_path='/health', http_get_port=5000),
            readiness=HP(http_get_path='/ready', http_get_port=5000),
            startup=HP(exec_command=['cat', '/tmp/started']),
        )
        assert hc.liveness is not None
        assert hc.readiness is not None
        assert hc.startup is not None
        assert hc.startup.exec_command == ['cat', '/tmp/started']

    def test_from_dict_readiness_only(self):
        PHC = self._hc_class()
        hc = PHC(**{
            'readiness': {'http_get_path': '/health', 'http_get_port': 8080},
            'networking_requires_ready': True,
        })
        assert hc.readiness.http_get_port == 8080


# ── _build_k8_probe ───────────────────────────────────────────────────────────

class TestBuildK8Probe:
    """Unit tests for the _build_k8_probe() helper in kubernetes_utils."""

    @pytest.fixture(autouse=True)
    def mock_k8_client(self):
        """Patch the kubernetes client so no cluster connection is needed."""
        with patch.dict(sys.modules, {
            'kubernetes': MagicMock(),
            'kubernetes.client': MagicMock(),
            'kubernetes.config': MagicMock(),
            'kubernetes.stream': MagicMock(),
        }):
            # kubernetes.client.V1Probe, V1HTTPGetAction etc. need to return
            # real objects (not mocks) so we can assert on their args.
            import kubernetes.client as kc
            # Use a simple namespace to capture constructor args.
            class Probe:
                def __init__(self, **kw): self.__dict__.update(kw)
            class HTTPAction:
                def __init__(self, **kw): self.__dict__.update(kw)
            class ExecAction:
                def __init__(self, **kw): self.__dict__.update(kw)
            class TCPAction:
                def __init__(self, **kw): self.__dict__.update(kw)

            kc.V1Probe = Probe
            kc.V1HTTPGetAction = HTTPAction
            kc.V1ExecAction = ExecAction
            kc.V1TCPSocketAction = TCPAction
            yield

    def _build(self, probe_dict):
        from models_base import HealthcheckProbe
        from kubernetes_utils import _build_k8_probe
        return _build_k8_probe(HealthcheckProbe(**probe_dict))

    def test_http_probe_builds_http_get_action(self):
        result = self._build({'http_get_path': '/health', 'http_get_port': 5000})
        assert hasattr(result, 'http_get')
        assert result.http_get.path == '/health'
        assert result.http_get.port == 5000
        assert result.http_get.scheme == 'HTTP'

    def test_http_probe_defaults_port_to_5000_when_none(self):
        result = self._build({'http_get_path': '/ready', 'http_get_port': None})
        assert result.http_get.port == 5000

    def test_exec_probe_builds_exec_action(self):
        result = self._build({'exec_command': ['curl', '-f', 'http://localhost']})
        assert hasattr(result, '_exec')
        assert result._exec.command == ['curl', '-f', 'http://localhost']

    def test_tcp_probe_builds_tcp_socket_action(self):
        result = self._build({'tcp_socket_port': 5432})
        assert hasattr(result, 'tcp_socket')
        assert result.tcp_socket.port == 5432

    def test_timing_fields_propagated(self):
        result = self._build({
            'http_get_path': '/health',
            'http_get_port': 5000,
            'initial_delay_seconds': 20,
            'period_seconds': 15,
            'timeout_seconds': 3,
            'failure_threshold': 5,
            'success_threshold': 2,
        })
        assert result.initial_delay_seconds == 20
        assert result.period_seconds == 15
        assert result.timeout_seconds == 3
        assert result.failure_threshold == 5
        # k8s rejects success_threshold != 1 for liveness/startup probes, so the
        # builder forces 1 unless is_readiness=True — only readiness may propagate it.
        assert result.success_threshold == 1

    def test_success_threshold_propagates_for_readiness_only(self):
        from models_base import HealthcheckProbe
        from kubernetes_utils import _build_k8_probe
        probe = HealthcheckProbe(http_get_path='/health', http_get_port=5000, success_threshold=2)
        assert _build_k8_probe(probe, is_readiness=True).success_threshold == 2
        assert _build_k8_probe(probe, is_readiness=False).success_threshold == 1

    def test_no_action_raises_value_error(self):
        from models_base import HealthcheckProbe
        from kubernetes_utils import _build_k8_probe
        probe = HealthcheckProbe()  # all action fields None
        with pytest.raises(ValueError, match="must have one of"):
            _build_k8_probe(probe)
