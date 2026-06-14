"""
Pure helpers for stack templates (kind='stack' template tags).

Kept dependency-light (only re/typing) so they unit-test in isolation — no DB, no `codes`,
no `g`. The DB-touching instantiation flow lives in api_stacks.py and calls these.

Two reference families a member may use, both compiled away at instantiation:
  - ${stack:secrets:KEY}            shared, stack-scoped secret (stays a *reference* on the pod;
                                    resolved at pod start by walking pod.stack_id -> stack.secret_map)
  - ${stack:<member>:<field>}       another member's address; field in
                                    {host, url, port, protocol, pod_id}. Non-secret + stable, so
                                    resolved to a literal at instantiation.
"""
import re
from typing import List, Dict, Any, Tuple, Optional

# ${stack:secrets:KEY}
STACK_SECRET_REF = re.compile(r'\$\{stack:secrets:([a-zA-Z0-9_-]+)\}')
# ${stack:<member>:<field>}
STACK_MEMBER_REF = re.compile(r'\$\{stack:([a-z][a-z0-9]+):(host|url|port|protocol|pod_id)\}')

VALID_MEMBER_FIELDS = {"host", "url", "port", "protocol", "pod_id"}
POD_ID_RE = re.compile(r'[a-z][a-z0-9]+')


def derive_pod_id(stack_id: str, member_name: str) -> str:
    """Default external pod_id for a member: flat `{stack_id}{name}` (collision-free per tenant)."""
    return f"{stack_id}{member_name}"


def k8_name(site_id: str, tenant_id: str, pod_id: str) -> str:
    """In-cluster DNS name for a pod (mirrors models_pods set_k8_name_and_networking_urls)."""
    return f"pods-{site_id}-{tenant_id}-{pod_id}"


def resolve_member_pod_ids(
    member_names: List[str],
    stack_id: str,
    overrides: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, str], List[str]]:
    """Resolve each member name -> external pod_id.

    Precedence: overrides[name] > `{stack_id}{name}` (templates are name-only, so no member literal).
    Validates charset, length (3-64), and that resolved pod_ids are unique across the batch. Does NOT
    check tenant existence — that's a DB concern done at instantiation.

    Returns (name->pod_id mapping, errors). errors == [] means valid.
    """
    overrides = overrides or {}
    errors = []

    for name in overrides:
        if name not in member_names:
            errors.append(f"pod_ids override references unknown member '{name}'.")

    mapping: Dict[str, str] = {}
    for name in member_names:
        pid = overrides.get(name) or derive_pod_id(stack_id, name)
        if not POD_ID_RE.fullmatch(pid):
            errors.append(
                f"member '{name}': resolved pod_id '{pid}' must be lowercase alphanumeric, first char alpha."
            )
        if len(pid) < 3 or len(pid) > 64:
            errors.append(
                f"member '{name}': resolved pod_id '{pid}' length must be 3-64 (got {len(pid)}). "
                f"Set pod_ids['{name}'] to a shorter explicit id."
            )
        mapping[name] = pid

    seen: Dict[str, List[str]] = {}
    for name, pid in mapping.items():
        seen.setdefault(pid, []).append(name)
    for pid, names in seen.items():
        if len(names) > 1:
            errors.append(
                f"resolved pod_id '{pid}' is shared by members {names}; override pod_ids to disambiguate."
            )

    return mapping, errors


def find_cycle(dep_graph: Dict[str, List[str]]) -> Optional[List[str]]:
    """Return a cycle path (e.g. ['a','b','a']) if the name->deps graph has one, else None."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in dep_graph}
    path: List[str] = []

    def dfs(n: str) -> Optional[List[str]]:
        color[n] = GRAY
        path.append(n)
        for d in dep_graph.get(n, []):
            if d not in color:
                continue
            if color[d] == GRAY:
                return path[path.index(d):] + [d]
            if color[d] == WHITE:
                res = dfs(d)
                if res:
                    return res
        path.pop()
        color[n] = BLACK
        return None

    for n in list(dep_graph):
        if color[n] == WHITE:
            res = dfs(n)
            if res:
                return res
    return None


def topo_order(members: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Order members so each comes after the members it depends_on (deps first).

    Assumes the graph is acyclic (validate_stack_definition first). Preserves input order within
    an independent tier. Used so that when from-template creates pods one by one, a dependency
    already exists (and is same-stack) before its dependent is validated/created.
    """
    by_name = {m.get("name"): m for m in members}
    visited = set()
    order: List[Dict[str, Any]] = []

    def visit(name, ancestry=()):  # ancestry guards against a stray cycle so we never infinite-loop
        if name in visited or name not in by_name:
            return
        for dep in (by_name[name].get("depends_on") or []):
            if dep in by_name and dep not in ancestry:
                visit(dep, ancestry + (name,))
        if name not in visited:
            visited.add(name)
            order.append(by_name[name])

    for m in members:
        visit(m.get("name"))
    return order


def validate_stack_definition(
    members: List[Dict[str, Any]],
    stack_secret_keys: Optional[List[str]] = None,
) -> List[str]:
    """Structural validation independent of instantiation context.

    `members` is a list of plain dicts each with (at least) name, depends_on, ready_condition,
    environment_variables, secret_map, healthchecks. Checks: depends_on targets exist + no self-dep
    + no cycle; ready_condition=='ready' requires a readiness probe; ${stack:secrets:KEY} refs resolve
    to a stack secret key; ${stack:<member>:...} refs resolve to a real member.

    Returns a list of error strings ([] == valid).
    """
    errors: List[str] = []
    names = [m.get("name") for m in members]
    nameset = set(names)
    secret_keys = set(stack_secret_keys or [])

    for m in members:
        name = m.get("name")
        for dep in (m.get("depends_on") or []):
            if dep == name:
                errors.append(f"member '{name}' cannot depend on itself.")
            elif dep not in nameset:
                errors.append(f"member '{name}' depends_on unknown member '{dep}'.")

        if m.get("ready_condition") == "ready":
            hc = m.get("healthchecks") or {}
            if not (isinstance(hc, dict) and hc.get("readiness")):
                errors.append(
                    f"member '{name}' has ready_condition='ready' but no healthchecks.readiness probe."
                )

        blobs: List[Any] = []
        blobs += list((m.get("environment_variables") or {}).values())
        blobs += list((m.get("secret_map") or {}).values())
        for val in blobs:
            if not isinstance(val, str):
                continue
            for key in STACK_SECRET_REF.findall(val):
                if key not in secret_keys:
                    errors.append(
                        f"member '{name}' references ${{stack:secrets:{key}}} but stack_definition.secret_map "
                        f"has no key '{key}'."
                    )
            for ref_name, field in STACK_MEMBER_REF.findall(val):
                if ref_name not in nameset:
                    errors.append(
                        f"member '{name}' references ${{stack:{ref_name}:{field}}} but there is no member "
                        f"'{ref_name}'."
                    )

    dep_graph = {
        m.get("name"): [d for d in (m.get("depends_on") or []) if d in nameset]
        for m in members
    }
    cycle = find_cycle(dep_graph)
    if cycle:
        errors.append(f"depends_on forms a cycle: {' -> '.join(cycle)}.")

    return errors


def compile_member_stack_refs(
    env: Optional[Dict[str, str]],
    secret_map: Optional[Dict[str, str]],
    pod_id_by_name: Dict[str, str],
    member_net_by_name: Dict[str, Dict[str, Any]],
    site_id: str,
    tenant_id: str,
    url_by_pod: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Lower one member's ${stack:...} references into ordinary pod primitives.

    - ${stack:<member>:host|url|port|protocol|pod_id} -> literal (host = in-cluster k8 name).
    - ${stack:secrets:KEY} appearing in env is normalized: the reference is moved into the member's
      secret_map (secret_map[KEY] = "${stack:secrets:KEY}") and env switched to ${pods:secrets:KEY},
      so the resulting pod uses only conventional primitives. The pod thus stores a *reference*,
      never the resolved secret value.

    Returns (new_env, new_secret_map).
    """
    env = dict(env or {})
    secret_map = dict(secret_map or {})
    url_by_pod = url_by_pod or {}

    def sub_member_refs(s: str) -> str:
        def repl(match: "re.Match") -> str:
            ref_name, field = match.group(1), match.group(2)
            pid = pod_id_by_name.get(ref_name)
            if pid is None:
                return match.group(0)
            net = member_net_by_name.get(ref_name) or {}
            default = (net.get("default") or {}) if isinstance(net, dict) else {}
            if field == "host":
                return k8_name(site_id, tenant_id, pid)
            if field == "pod_id":
                return pid
            if field == "url":
                return url_by_pod.get(pid, pid)
            if field == "port":
                return str(default.get("port", ""))
            if field == "protocol":
                return str(default.get("protocol", ""))
            return match.group(0)
        return STACK_MEMBER_REF.sub(repl, s)

    for k, v in list(env.items()):
        if isinstance(v, str):
            env[k] = sub_member_refs(v)
    for k, v in list(secret_map.items()):
        if isinstance(v, str):
            secret_map[k] = sub_member_refs(v)

    for k, v in list(env.items()):
        if not isinstance(v, str):
            continue
        for key in STACK_SECRET_REF.findall(v):
            secret_map.setdefault(key, "${stack:secrets:" + key + "}")
            v = v.replace("${stack:secrets:" + key + "}", "${pods:secrets:" + key + "}")
        env[k] = v

    return env, secret_map
