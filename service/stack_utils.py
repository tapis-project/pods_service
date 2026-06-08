"""Helpers for stack membership / dependency-ordering fields on pods.

Kept separate from models_pods so the cross-pod queries (existence, same-stack, cycle
detection, dependents lookup) live in one testable place and avoid import cycles.
"""
from sqlmodel import select
from models_pods import Pod
from tapisservice.logs import get_logger
logger = get_logger(__name__)


def find_dependents(pod_id, tenant, site):
    """Return pods (same tenant/site) whose depends_on array contains pod_id."""
    site, tenant, store = Pod.get_site_tenant_session(tenant=tenant, site=site)
    stmt = select(Pod).where(Pod.depends_on.contains([pod_id]))
    return store.run("execute", stmt, scalars=True, all=True)


def _readiness_configured(pod) -> bool:
    """True if the pod defines a readiness probe (healthchecks may be a dict or a PodHealthchecks)."""
    hc = getattr(pod, "healthchecks", None)
    if hc is None:
        return False
    if isinstance(hc, dict):
        return bool(hc.get("readiness"))
    return getattr(hc, "readiness", None) is not None


def validate_stack_fields(pod, tenant, site):
    """Validate a pod's ready_condition + depends_on. Raises ValueError on any violation.

    - ready_condition == 'ready' requires a configured readiness probe.
    - depends_on requires the pod to be in a stack; every dep must exist, be in the same stack,
      and not form a dependency cycle.
    """
    # ready_condition value + cross-check (update_pod applies fields via setattr, bypassing the model validator)
    ready_condition = getattr(pod, "ready_condition", "available")
    if ready_condition not in (None, "available", "ready"):
        raise ValueError(f"ready_condition must be 'available' or 'ready'. Got '{ready_condition}'.")
    if ready_condition == "ready" and not _readiness_configured(pod):
        raise ValueError("ready_condition='ready' requires healthchecks.readiness to be configured on this pod.")

    deps = pod.depends_on or []
    if not deps:
        return

    if not pod.stack_id:
        raise ValueError("depends_on requires the pod to belong to a stack (set stack_id / join a stack first).")
    if pod.pod_id in deps:
        raise ValueError(f"Pod '{pod.pod_id}' cannot depend on itself.")

    # Existence + same-stack
    for dep_id in deps:
        dep = Pod.db_get_with_pk(dep_id, tenant=tenant, site=site)
        if not dep:
            raise ValueError(f"depends_on references pod '{dep_id}' which does not exist.")
        if (dep.stack_id or "") != pod.stack_id:
            raise ValueError(f"depends_on pod '{dep_id}' must be in the same stack ('{pod.stack_id}').")

    # Cycle detection: DFS following depends_on edges; reaching pod.pod_id means a cycle.
    target = pod.pod_id
    visited = set()

    def _dfs(edges):
        for nxt in edges or []:
            if nxt == target:
                raise ValueError(f"depends_on would create a dependency cycle back to '{target}'.")
            if nxt in visited:
                continue
            visited.add(nxt)
            dep = Pod.db_get_with_pk(nxt, tenant=tenant, site=site)
            if dep:
                _dfs(dep.depends_on)

    _dfs(pod.depends_on)
