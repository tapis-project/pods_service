"""
Unit tests for stack dependency-ordering logic:
- stack_runner pure gate functions (is_ready / gate_start / gate_stop / compute_stack_status)
- stack_utils.validate_stack_fields (ready_condition, existence, same-stack, cycle detection)

Run inside the container:
    pytest tests/test_stacks.py --disable-pytest-warnings -v

Pure unit tests — no live service, database, or Kubernetes. Heavy/unavailable deps are mocked
only long enough to import the modules under test, then sys.modules is restored so the shared
pytest session (make test runs every file together) is not polluted. Status constants are
imported from `codes` so comparisons stay consistent whether another test mocked `codes` or not.
"""
import os
import sys
import logging
import pytest
from types import SimpleNamespace, ModuleType

sys.path.append('/home/tapis/service')
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'service'))


# ── Fake models_pods.Pod backed by an in-test registry (drives db_get_with_pk) ──
_POD_REGISTRY = {}


class _FakePod:
    @classmethod
    def db_get_with_pk(cls, pk, tenant=None, site=None):
        return _POD_REGISTRY.get(pk)

    @staticmethod
    def get_site_tenant_session(tenant=None, site=None):
        return site, tenant, SimpleNamespace(run=lambda *a, **k: [])


def _mk(name, **attrs):
    m = ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


# Install mocks, import the modules under test, then restore sys.modules. `codes` is left
# alone (real, or whatever another test file already set) and referenced via imported constants.
_MOCK = {
    'stores': _mk('stores', pg_store={}),
    'tapisservice': _mk('tapisservice'),
    'tapisservice.logs': _mk('tapisservice.logs', get_logger=lambda n: logging.getLogger(n)),
    'models_pods': _mk('models_pods', Pod=_FakePod),
    'models_stacks': _mk('models_stacks', Stack=SimpleNamespace()),
    'sqlmodel': _mk('sqlmodel', select=lambda *a, **k: None),
}
_MOCK['tapisservice'].logs = _MOCK['tapisservice.logs']

_SENTINEL = object()
_saved = {name: sys.modules.get(name, _SENTINEL) for name in _MOCK}
sys.modules.update(_MOCK)
try:
    import stack_runner
    import stack_utils
finally:
    for name, prev in _saved.items():
        if prev is _SENTINEL:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = prev

from codes import AVAILABLE, ON, OFF, RESTART, STOPPED


# ── helpers ──────────────────────────────────────────────────────────────────

def _pod(**kw):
    base = dict(pod_id="p", status=AVAILABLE, status_requested=ON,
                ready_condition="available", status_container={},
                stack_id="web", depends_on=None, healthchecks=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _stack(restart_policy="ordered"):
    return SimpleNamespace(restart_policy=restart_policy)


def _register(*pods):
    _POD_REGISTRY.clear()
    for p in pods:
        _POD_REGISTRY[p.pod_id] = p


# ── is_ready ─────────────────────────────────────────────────────────────────

def test_is_ready_available_default():
    assert stack_runner.is_ready(_pod(status=AVAILABLE)) is True
    assert stack_runner.is_ready(_pod(status=STOPPED)) is False
    assert stack_runner.is_ready(None) is False


def test_is_ready_readiness_probe():
    ready = _pod(status=AVAILABLE, ready_condition="ready", status_container={"ready": True})
    not_ready = _pod(status=AVAILABLE, ready_condition="ready", status_container={"ready": False})
    no_container = _pod(status=AVAILABLE, ready_condition="ready", status_container={})
    assert stack_runner.is_ready(ready) is True
    assert stack_runner.is_ready(not_ready) is False
    assert stack_runner.is_ready(no_container) is False


# ── gate_start ───────────────────────────────────────────────────────────────

def test_gate_start_parallel_never_gates():
    deps = {"db": _pod(pod_id="db", status=STOPPED, status_requested=OFF)}
    assert stack_runner.gate_start(_stack("parallel"), deps) == (True, [])


def test_gate_start_passes_when_dep_on_and_ready():
    deps = {"db": _pod(pod_id="db", status=AVAILABLE, status_requested=ON)}
    assert stack_runner.gate_start(_stack(), deps) == (True, [])


def test_gate_start_blocks_on_restart_race():
    # Dep is still AVAILABLE but intended RESTART → must block (the restart race).
    deps = {"db": _pod(pod_id="db", status=AVAILABLE, status_requested=RESTART)}
    allow, blocking = stack_runner.gate_start(_stack(), deps)
    assert allow is False and blocking == ["db"]


def test_gate_start_blocks_on_stopped_or_off_or_missing():
    deps = {
        "db": _pod(pod_id="db", status=STOPPED, status_requested=ON),       # not ready
        "cache": _pod(pod_id="cache", status=AVAILABLE, status_requested=OFF),  # intended off
        "queue": None,  # missing
    }
    allow, blocking = stack_runner.gate_start(_stack(), deps)
    assert allow is False
    assert set(blocking) == {"db", "cache", "queue"}


# ── gate_stop ────────────────────────────────────────────────────────────────

def test_gate_stop_parallel_never_gates():
    dependents = [_pod(pod_id="app", status=AVAILABLE)]
    assert stack_runner.gate_stop(_stack("parallel"), dependents) == (True, [])


def test_gate_stop_holds_until_dependents_stopped():
    running = [_pod(pod_id="app", status=AVAILABLE)]
    stopped = [_pod(pod_id="app", status=STOPPED)]
    assert stack_runner.gate_stop(_stack(), running) == (False, ["app"])
    assert stack_runner.gate_stop(_stack(), stopped) == (True, [])


# ── compute_stack_status ─────────────────────────────────────────────────────

def test_compute_stack_status():
    assert stack_runner.compute_stack_status([]) == "empty"
    pods = [_pod(status=AVAILABLE), _pod(status=AVAILABLE), _pod(status=STOPPED)]
    assert stack_runner.compute_stack_status(pods) == "2/3 AVAILABLE"


# ── validate_stack_fields ────────────────────────────────────────────────────

def test_validate_ready_condition_requires_readiness_probe():
    pod = _pod(ready_condition="ready", healthchecks=None, depends_on=None)
    _register(pod)
    with pytest.raises(ValueError, match="readiness"):
        stack_utils.validate_stack_fields(pod, tenant="t", site="s")
    # With a readiness probe configured it passes.
    pod.healthchecks = {"readiness": {"http_get_path": "/health", "http_get_port": 5000}}
    stack_utils.validate_stack_fields(pod, tenant="t", site="s")


def test_validate_depends_on_requires_stack():
    pod = _pod(pod_id="app", stack_id="", depends_on=["db"])
    _register(pod)
    with pytest.raises(ValueError, match="belong to a stack"):
        stack_utils.validate_stack_fields(pod, tenant="t", site="s")


def test_validate_depends_on_existence_and_same_stack():
    app = _pod(pod_id="app", stack_id="web", depends_on=["db"])
    _register(app)  # missing dep
    with pytest.raises(ValueError, match="does not exist"):
        stack_utils.validate_stack_fields(app, tenant="t", site="s")
    db_other = _pod(pod_id="db", stack_id="other", depends_on=None)  # cross-stack dep
    _register(app, db_other)
    with pytest.raises(ValueError, match="same stack"):
        stack_utils.validate_stack_fields(app, tenant="t", site="s")


def test_validate_self_dependency():
    pod = _pod(pod_id="app", stack_id="web", depends_on=["app"])
    _register(pod)
    with pytest.raises(ValueError, match="cannot depend on itself"):
        stack_utils.validate_stack_fields(pod, tenant="t", site="s")


def test_validate_cycle_detection():
    app = _pod(pod_id="app", stack_id="web", depends_on=["db"])
    db = _pod(pod_id="db", stack_id="web", depends_on=["app"])  # app -> db -> app
    _register(app, db)
    with pytest.raises(ValueError, match="cycle"):
        stack_utils.validate_stack_fields(app, tenant="t", site="s")


def test_validate_three_node_cycle():
    a = _pod(pod_id="a", stack_id="web", depends_on=["b"])
    b = _pod(pod_id="b", stack_id="web", depends_on=["c"])
    c = _pod(pod_id="c", stack_id="web", depends_on=["a"])  # a -> b -> c -> a
    _register(a, b, c)
    with pytest.raises(ValueError, match="cycle"):
        stack_utils.validate_stack_fields(a, tenant="t", site="s")


def test_validate_valid_chain_passes():
    db = _pod(pod_id="db", stack_id="web", depends_on=None)
    app = _pod(pod_id="app", stack_id="web", depends_on=["db"])
    _register(db, app)
    stack_utils.validate_stack_fields(app, tenant="t", site="s")  # should not raise
