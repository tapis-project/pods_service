"""Stack Action Runner — controller-layer dependency ordering.

The ordering logic is a set of pure, level-triggered gate functions the health loop calls
each tick. Desired state lives on the pods (status_requested, depends_on, ready_condition)
and the stack (restart_policy); ordering *emerges* from re-evaluating these gates — the same
model as `kubectl wait --for=condition`, centralized in the controller. Non-blocking and
crash-safe (no per-run state to recover).

The pure functions (is_ready / gate_start / gate_stop / compute_stack_status) take plain
objects so they unit-test without a database. The evaluate_* wrappers do the DB loading.
"""
from codes import AVAILABLE, ON, STOPPED
from models_pods import Pod
from models_stacks import Stack
from tapisservice.logs import get_logger
logger = get_logger(__name__)


def is_ready(dep) -> bool:
    """Whether a dependency pod counts as 'up' for dependents, per its ready_condition.

    'available' (default) → status is AVAILABLE.
    'ready'               → status is AVAILABLE and the readiness probe is passing.
    """
    if dep is None:
        return False
    if getattr(dep, "ready_condition", "available") == "ready":
        return dep.status == AVAILABLE and bool((getattr(dep, "status_container", None) or {}).get("ready"))
    return dep.status == AVAILABLE


def gate_start(stack, deps_by_id):
    """Return (allow_start, blocking_ids).

    A dependency blocks the start unless it is *intended on* (status_requested == ON) AND ready.
    Blocking on `status_requested != ON` (not just OFF) is what makes restart ordering correct:
    a dep mid-RESTART is still AVAILABLE in the window before teardown, and we must not let a
    dependent start against it. restart_policy == 'parallel' disables all gating.
    """
    if stack is None or getattr(stack, "restart_policy", "ordered") == "parallel":
        return True, []
    blocking = [dep_id for dep_id, dep in deps_by_id.items()
                if dep is None or dep.status_requested != ON or not is_ready(dep)]
    return (len(blocking) == 0), blocking


def gate_stop(stack, dependents):
    """Return (allow_stop, waiting_ids).

    Reverse-order teardown: a pod may only stop once everything that depends on it is STOPPED
    (app before db). restart_policy == 'parallel' disables gating (tear down simultaneously).
    """
    if stack is None or getattr(stack, "restart_policy", "ordered") == "parallel":
        return True, []
    waiting = [d.pod_id for d in dependents if d.status != STOPPED]
    return (len(waiting) == 0), waiting


def compute_stack_status(member_pods) -> str:
    """Human-scannable aggregate, e.g. '2/3 AVAILABLE' (or 'empty')."""
    total = len(member_pods)
    if not total:
        return "empty"
    available = sum(1 for p in member_pods if p.status == AVAILABLE)
    return f"{available}/{total} AVAILABLE"


# ---- DB-loading wrappers called by the health loop -------------------------------------------

def evaluate_start_gate(pod, tenant, site):
    """Load the pod's stack + deps and decide whether it may start now. (allow, blocking_ids)."""
    if not pod.depends_on or not pod.stack_id:
        return True, []
    stack = Stack.db_get_with_pk(pod.stack_id, tenant=tenant, site=site)
    if stack is None or stack.restart_policy == "parallel":
        return True, []
    deps_by_id = {dep_id: Pod.db_get_with_pk(dep_id, tenant=tenant, site=site) for dep_id in pod.depends_on}
    return gate_start(stack, deps_by_id)


def evaluate_stop_gate(pod, tenant, site):
    """Reverse-order teardown gate for ordered stacks. (allow, waiting_dependent_ids)."""
    if not pod.stack_id:
        return True, []
    stack = Stack.db_get_with_pk(pod.stack_id, tenant=tenant, site=site)
    if stack is None or stack.restart_policy == "parallel":
        return True, []
    from stack_utils import find_dependents
    dependents = find_dependents(pod.pod_id, tenant=tenant, site=site)
    return gate_stop(stack, dependents)
