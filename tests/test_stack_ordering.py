"""
Pure-function tests for stack start ordering + the pod↔k8s identity match.

Regression guard for the "stack member stuck at REQUESTED" bug: a substring match
('myapp' in 'myappapi') aliased a member onto a sibling's running pod, so a
member stranded at REQUESTED was never recovered to STOPPED and never re-spawned.
"""
import sys
from types import SimpleNamespace

sys.path.append('/home/tapis/service')
from health import pod_matches_k8_pod
from stack_runner import is_ready, gate_start
from codes import AVAILABLE, ON, OFF, STOPPED


def _pod(pid, tenant="dev", site="tacc"):
    return SimpleNamespace(pod_id=pid, tenant_id=tenant, site_id=site)


def _k8(pid, tenant="dev", site="tacc"):
    return {"pod_id": pid, "tenant_id": tenant, "site_id": site}


# ── pod ↔ k8s identity match (the bug) ────────────────────────────────────────
def test_exact_match_is_a_match():
    assert pod_matches_k8_pod(_pod("myapp"), _k8("myapp")) is True


def test_stack_member_does_not_alias_onto_sibling():
    # 'myapp' is a substring of both siblings — it must NOT match their pods.
    assert pod_matches_k8_pod(_pod("myapp"), _k8("myappapi")) is False
    assert pod_matches_k8_pod(_pod("myapp"), _k8("myappdb")) is False


def test_match_is_tenant_and_site_scoped():
    assert pod_matches_k8_pod(_pod("foo", tenant="dev"), _k8("foo", tenant="tacc")) is False
    assert pod_matches_k8_pod(_pod("foo", site="tacc"), _k8("foo", site="east")) is False


# ── ordered start gate ─────────────────────────────────────────────────────────
def _stack(policy="ordered"):
    return SimpleNamespace(restart_policy=policy)


def _dep(status=AVAILABLE, requested=ON, ready_condition="available", ready=True):
    return SimpleNamespace(status=status, status_requested=requested,
                           ready_condition=ready_condition,
                           status_container={"ready": ready})


def test_gate_blocks_until_dependency_available_and_on():
    allow, blocking = gate_start(_stack(), {"dep": _dep(status=STOPPED)})
    assert allow is False and blocking == ["dep"]
    allow, _ = gate_start(_stack(), {"dep": _dep(status=AVAILABLE, requested=ON)})
    assert allow is True


def test_gate_blocks_dep_not_intended_on():
    # a dep mid-restart is AVAILABLE but status_requested != ON → still blocks.
    allow, blocking = gate_start(_stack(), {"dep": _dep(status=AVAILABLE, requested=OFF)})
    assert allow is False and blocking == ["dep"]


def test_ready_condition_requires_probe():
    assert is_ready(_dep(ready_condition="ready", ready=True)) is True
    assert is_ready(_dep(ready_condition="ready", ready=False)) is False


def test_parallel_policy_disables_gating():
    allow, blocking = gate_start(_stack("parallel"), {"dep": _dep(status=STOPPED)})
    assert allow is True and blocking == []
