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


def match_live_member_pod_ids(
    stack_id: str,
    member_images: Dict[str, str],
    kind_by_name: Dict[str, str],
    live: List[Any],
) -> Dict[str, str]:
    """Map member_name -> the EXISTING live pod_id, robust to pod-id drift.

    The naive approach (strip the `{stack_id}` prefix off each live pod_id to recover
    its member name) fails whenever a member's pod_id doesn't follow the
    `{stack_id}{name}` convention — most notably a legacy member whose pod_id is the
    *bare* stack_id. When that member can't be matched, update derives a fresh
    `{stack_id}{name}` pod_id for it, creating a DUPLICATE and orphaning the real pod.
    This function repairs that so update reuses the live pod.

    Args:
        member_images: {name: image} for every member across the old + new tags.
        kind_by_name:  {name: plan_kind} from compute_stack_member_plan
                       ('add' | 'remove' | 'recreate' | 'patch' | 'unchanged').
        live:          member pods (objects exposing ``.pod_id`` and ``.image``).

    Pass 1 claims pods whose id follows the convention. Pass 2 reconciles leftovers:
    a member that PERSISTS or is REMOVED (kind != 'add') but whose conventional pod is
    absent is matched to an unclaimed live pod, preferring an equal image; a single
    remaining candidate is taken outright. 'add' members are never leftover-matched —
    they legitimately have no live pod yet.
    """
    names = set(member_images)
    name_to_pid: Dict[str, str] = {}
    claimed = set()

    # Pass 1: exact `{stack_id}{name}` convention.
    for pod in live:
        pid = pod.pod_id
        nm = pid[len(stack_id):] if (pid.startswith(stack_id) and len(pid) > len(stack_id)) else None
        if nm and nm in names and nm not in name_to_pid:
            name_to_pid[nm] = pid
            claimed.add(pid)

    # Pass 2: reconcile id-drift for members that should have a live pod.
    for nm in [n for n in names if n not in name_to_pid and kind_by_name.get(n) != "add"]:
        cands = [p for p in live if p.pod_id not in claimed]
        if not cands:
            break
        want = member_images.get(nm) or ""
        match = next((p for p in cands if (getattr(p, "image", "") or "") == want), None)
        if match is None and len(cands) == 1:
            match = cands[0]
        if match is not None:
            name_to_pid[nm] = match.pod_id
            claimed.add(match.pod_id)

    return name_to_pid


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
        # config_content supports the same ${stack:...} refs (lowered at instantiation
        # by compile_refs_in_volume_mounts) — validate them here too.
        for mount in (m.get("volume_mounts") or {}).values():
            if isinstance(mount, dict) and isinstance(mount.get("config_content"), str):
                blobs.append(mount["config_content"])
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


def sanitize_member_networking(
    net: Optional[Dict[str, Any]],
    allowed_fields,
    drop=("url",),
) -> Optional[Dict[str, Any]]:
    """Strip runtime-only keys from a live pod's networking so it validates as *template* networking.

    A live pod's networking entries carry service-managed keys (custom_domain, custom_domain_verified,
    a generated url, ...) that the template Networking model forbids (extra=forbid). When snapshotting
    a live stack into a kind='stack' tag we keep only `allowed_fields` per entry, and also drop
    anything in `drop` (the generated `url` is re-derived per pod at instantiation).

    `allowed_fields` is the set of template-valid networking field names (passed in by the caller so
    this stays a pure, import-light function). Returns a new dict; non-dict input is returned as-is.
    """
    if not isinstance(net, dict):
        return net
    allowed = set(allowed_fields)
    drop = set(drop or ())
    cleaned: Dict[str, Any] = {}
    for entry_name, spec in net.items():
        if isinstance(spec, dict):
            spec = {k: v for k, v in spec.items() if k in allowed and k not in drop}
        cleaned[entry_name] = spec
    return cleaned


def placeholderize_secret_value(val: Any) -> Any:
    """For snapshots: keep shared-stack references and existing placeholders; replace any concrete
    secret reference/value with a required placeholder so a template never embeds a real secret."""
    if not isinstance(val, str):
        return val
    if val.startswith("${stack:secrets:") or val.startswith("${:?") or val.startswith("${pods:default:"):
        return val
    return "${:?provide this secret value}"


def unbake_host_refs(s: Any, k8_to_ref: Dict[str, str]) -> Any:
    """Reverse a stack's member k8 hostnames back into portable ${stack:<name>:host} references.

    `k8_to_ref` maps each member's in-cluster k8 name -> its ${stack:<name>:host} reference. Used when
    snapshotting a live stack so cross-member wiring becomes template-portable again. Non-str in,
    non-str out unchanged.
    """
    if not isinstance(s, str):
        return s
    for k8, ref in k8_to_ref.items():
        s = s.replace(k8, ref)
    return s


def make_member_ref_substituter(
    pod_id_by_name: Dict[str, str],
    member_net_by_name: Dict[str, Dict[str, Any]],
    site_id: str,
    tenant_id: str,
    url_by_pod: Optional[Dict[str, str]] = None,
):
    """Build the ${stack:<member>:field} -> literal substituter shared by env,
    secret_map, and volume_mounts config_content lowering."""
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

    return sub_member_refs


def compile_refs_in_volume_mounts(
    volume_mounts: Optional[Dict[str, Any]],
    secret_map: Dict[str, str],
    pod_id_by_name: Dict[str, str],
    member_net_by_name: Dict[str, Dict[str, Any]],
    site_id: str,
    tenant_id: str,
    url_by_pod: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, str]]:
    """Lower ${stack:...} references inside volume_mounts config_content.

    Same semantics as env lowering in compile_member_stack_refs:
    - ${stack:<member>:host|url|port|protocol|pod_id} -> literal at instantiation
      (refs are stable once pod_ids are resolved, exactly like env values).
    - ${stack:secrets:KEY} -> ${pods:secrets:KEY} + the reference moves into the
      member's secret_map, so the start-time config interpolation (which only
      understands ${pods:secrets:...}) resolves it. The stored definition keeps
      a reference, never a resolved secret value.

    Returns (new_volume_mounts, new_secret_map); volume_mounts passes through
    unchanged (same object) when there is nothing to lower.
    """
    if not isinstance(volume_mounts, dict):
        return volume_mounts, secret_map
    sub = make_member_ref_substituter(
        pod_id_by_name, member_net_by_name, site_id, tenant_id, url_by_pod
    )
    out: Dict[str, Any] = {}
    changed = False
    for mount_path, mount in volume_mounts.items():
        content = mount.get("config_content") if isinstance(mount, dict) else None
        if isinstance(content, str) and ("${stack:" in content):
            content = sub(content)
            for key in STACK_SECRET_REF.findall(content):
                secret_map.setdefault(key, "${stack:secrets:" + key + "}")
                content = content.replace(
                    "${stack:secrets:" + key + "}", "${pods:secrets:" + key + "}"
                )
            mount = {**mount, "config_content": content}
            changed = True
        out[mount_path] = mount
    return (out if changed else volume_mounts), secret_map


def find_residual_host_refs(
    member_defs: List[Dict[str, Any]],
    site_id: str,
    tenant_id: str,
) -> List[str]:
    """Scan template member defs for in-cluster hostnames that survived un-baking.

    unbake_host_refs only rewrites hostnames attributable to a CURRENT member;
    anything else that still matches pods-<site>-<tenant>-* (another stack's
    service, a pasted host, an ex-member) is being saved literally and will only
    resolve where those exact services exist. Returns one human-readable line per
    affected member naming the fields, for response messages/metadata.
    """
    residual_prefix = f"pods-{site_id}-{tenant_id}-"
    warnings: List[str] = []
    for member in member_defs:
        hits = []
        fields = [
            (f"environment_variables.{k}", v)
            for k, v in (member.get("environment_variables") or {}).items()
        ] + [
            (f"secret_map.{k}", v)
            for k, v in (member.get("secret_map") or {}).items()
        ] + [
            (f"volume_mounts['{mp}'].config_content", mnt.get("config_content"))
            for mp, mnt in (member.get("volume_mounts") or {}).items()
            if isinstance(mnt, dict)
        ]
        for where, val in fields:
            if isinstance(val, str) and residual_prefix in val:
                hits.append(where)
        if hits:
            warnings.append(f"member '{member.get('name')}': {', '.join(sorted(hits))}")
    return warnings


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
    sub_member_refs = make_member_ref_substituter(
        pod_id_by_name, member_net_by_name, site_id, tenant_id, url_by_pod
    )

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


# ── L2 reviewed-update plan (pure diff between two member sets) ──────────────────
# Mirrors the tapis-ui computeStackUpdatePlan so the dry_run plan the API returns matches
# what the UI previews. See tapis-ui src/app/Pods/STACK_UPDATE_MODEL.md §3.

# A change to one of these forces recreating the member pod (vs an in-place patch).
PLAN_RECREATE_FIELDS = ["image", "template", "networking", "volume_mounts"]
# A change to one of these can be applied with a patch + restart.
PLAN_PATCH_FIELDS = [
    "environment_variables", "secret_map", "command", "arguments",
    "resources", "depends_on", "ready_condition", "healthchecks",
]


def _stable(v: Any) -> str:
    """Order-insensitive serialization so field equality ignores dict key order."""
    import json
    return json.dumps(v, sort_keys=True, default=str)


def compute_stack_member_plan(
    old_members: List[Dict[str, Any]],
    new_members: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Diff two stack-template member sets (pinned tag vs newer tag) by member `name`.

    Returns a list of {name, kind, changed_fields, destructive} where kind is one of
    add | remove | recreate | patch | unchanged. Destructive == a member is removed, or a
    PERSISTENT volume (tapisvolume/tapissnapshot) is added/removed/repointed. An
    ephemeral/config_content mount carries no data, so changing it is a non-destructive
    recreate. Pure — no DB, no side effects.
    """
    old_by = {m["name"]: m for m in (old_members or []) if m.get("name")}
    new_by = {m["name"]: m for m in (new_members or []) if m.get("name")}
    names = sorted(set(old_by) | set(new_by))
    fields = list(dict.fromkeys(PLAN_RECREATE_FIELDS + PLAN_PATCH_FIELDS))

    plan: List[Dict[str, Any]] = []
    for name in names:
        o, n = old_by.get(name), new_by.get(name)
        if o and not n:
            plan.append({"name": name, "kind": "remove", "changed_fields": [], "destructive": True})
            continue
        if n and not o:
            plan.append({"name": name, "kind": "add", "changed_fields": [], "destructive": False})
            continue
        changed = [f for f in fields if _stable(o.get(f)) != _stable(n.get(f))]
        if not changed:
            plan.append({"name": name, "kind": "unchanged", "changed_fields": [], "destructive": False})
            continue
        recreate = any(f in PLAN_RECREATE_FIELDS for f in changed)
        plan.append({
            "name": name,
            "kind": "recreate" if recreate else "patch",
            "changed_fields": changed,
            # Only a persistent-volume topology change risks data — not a config edit.
            "destructive": _persistent_volume_changed(o.get("volume_mounts"), n.get("volume_mounts")),
        })
    return plan


# A mount holds real data only if it's a tapisvolume/tapissnapshot. Ephemeral/config
# mounts (config_content) carry no data — changing them is a safe recreate.
_PERSISTENT_MOUNT_TYPES = {"tapisvolume", "tapissnapshot"}


def _is_persistent_mount(vm: Any) -> bool:
    return isinstance(vm, dict) and str(vm.get("type") or "").lower() in _PERSISTENT_MOUNT_TYPES


def _persistent_volume_changed(old_vm: Any, new_vm: Any) -> bool:
    """True only when a persistent volume is added, removed, or repointed (source_id/sub_path)
    between two member versions. A config_content / ephemeral-mount change returns False."""
    old_vm = old_vm if isinstance(old_vm, dict) else {}
    new_vm = new_vm if isinstance(new_vm, dict) else {}
    for path in set(old_vm) | set(new_vm):
        o, n = old_vm.get(path), new_vm.get(path)
        o_persist, n_persist = _is_persistent_mount(o), _is_persistent_mount(n)
        if o_persist != n_persist:
            return True  # persistent added / removed / type-flipped
        if o_persist and n_persist:
            o_id = (str(o.get("source_id") or ""), str(o.get("sub_path") or ""))
            n_id = (str(n.get("source_id") or ""), str(n.get("sub_path") or ""))
            if o_id != n_id:
                return True  # repointed to different data
    return False


# ── snapshot minimization (save_as_template hygiene) ────────────────────────────
# Prune a live-pod snapshot down to an authored-looking definition: drop fields equal to service
# defaults, but KEEP every ${...} reference / secret_map / placeholder. Also reset baked
# tapis_auth_allowed_users to the AUTHORIZED_USERS sentinel so a template never leaks real usernames.
# See tapis-ui src/app/Pods/STACK_UPDATE_MODEL.md (snapshot = authored layer, not resolved blob).

AUTHORIZED_USERS_SENTINEL = ["AUTHORIZED_USERS"]


def _has_ref(v: Any) -> bool:
    """True if a value (or anything nested) carries a ${...} reference — those are never pruned."""
    if isinstance(v, str):
        return "${" in v
    if isinstance(v, dict):
        return any(_has_ref(x) for x in v.values())
    if isinstance(v, list):
        return any(_has_ref(x) for x in v)
    return False


def strip_defaults(
    d: Dict[str, Any],
    defaults: Dict[str, Any],
    always_keep: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    """Drop top-level keys whose value equals defaults[key] or is empty; keep always_keep keys and
    any value carrying a ${...} reference. Pure."""
    out: Dict[str, Any] = {}
    for k, v in (d or {}).items():
        if k in always_keep or _has_ref(v):
            out[k] = v
            continue
        if k in defaults and _stable(v) == _stable(defaults[k]):
            continue
        if v in (None, {}, [], ""):
            continue
        out[k] = v
    return out


def minimize_member_networking(
    networking: Dict[str, Any], entry_defaults: Dict[str, Any]
) -> Dict[str, Any]:
    """Per networking entry: reset tapis_auth_allowed_users to the AUTHORIZED_USERS sentinel (so baked
    usernames never persist into a reusable template), then strip fields equal to model defaults
    (always keep protocol/port and any ${...} ref)."""
    out: Dict[str, Any] = {}
    for name, entry in (networking or {}).items():
        if not isinstance(entry, dict):
            out[name] = entry
            continue
        e = dict(entry)
        if "tapis_auth_allowed_users" in e:
            e["tapis_auth_allowed_users"] = list(AUTHORIZED_USERS_SENTINEL)
        out[name] = strip_defaults(e, entry_defaults, always_keep=("protocol", "port"))
    return out


def minimize_resources(resources: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only meaningfully-set resource fields (positive ints); drop None / -1 (unset) / 0."""
    return {k: v for k, v in (resources or {}).items() if isinstance(v, int) and v > 0}
