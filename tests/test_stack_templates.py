"""
Unit tests for stack-template pure helpers (stack_template_utils):
- derive_pod_id / resolve_member_pod_ids  (name -> external pod_id, precedence + validation)
- find_cycle / topo_order                 (dependency graph)
- validate_stack_definition               (depends_on, cycles, ready->probe, ${stack:...} refs)
- compile_member_stack_refs               (${stack:<member>:host} -> literal; ${stack:secrets:KEY} normalize)

Pure — no DB, no `codes`, no `g`, no Kubernetes. stack_template_utils imports only re/typing, so it
is imported directly (no module mocking needed).

Run inside the container:
    pytest tests/test_stack_templates.py --disable-pytest-warnings -v
"""
import os
import sys

sys.path.append('/home/tapis/service')
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'service'))

import stack_template_utils as stu


# ── derive_pod_id / resolve_member_pod_ids ──────────────────────────────────────

def test_derive_pod_id_concatenates():
    assert stu.derive_pod_id("myn8n", "db") == "myn8ndb"


def test_resolve_ids_default_derivation():
    mapping, errors = stu.resolve_member_pod_ids(["db", "redis", "main"], "myn8n")
    assert errors == []
    assert mapping == {"db": "myn8ndb", "redis": "myn8nredis", "main": "myn8nmain"}


def test_resolve_ids_override_wins():
    mapping, errors = stu.resolve_member_pod_ids(["db", "main"], "myn8n", {"main": "myn8n"})
    assert errors == []
    assert mapping == {"db": "myn8ndb", "main": "myn8n"}


def test_resolve_ids_unknown_override_errors():
    _, errors = stu.resolve_member_pod_ids(["db"], "myn8n", {"ghost": "x"})
    assert any("unknown member 'ghost'" in e for e in errors)


def test_resolve_ids_duplicate_errors():
    # two members forced to the same pod_id
    _, errors = stu.resolve_member_pod_ids(["a", "b"], "stk", {"a": "samepod", "b": "samepod"})
    assert any("shared by members" in e for e in errors)


def test_resolve_ids_too_long_errors():
    long_name = "x" * 70
    _, errors = stu.resolve_member_pod_ids([long_name], "mystack")
    assert any("length must be 3-64" in e for e in errors)


def test_resolve_ids_bad_charset_errors():
    _, errors = stu.resolve_member_pod_ids(["web"], "mystack", {"web": "Bad_Id"})
    assert any("lowercase alphanumeric" in e for e in errors)


# ── find_cycle ──────────────────────────────────────────────────────────────────

def test_find_cycle_none():
    assert stu.find_cycle({"a": ["b"], "b": ["c"], "c": []}) is None


def test_find_cycle_self():
    assert stu.find_cycle({"a": ["a"]}) is not None


def test_find_cycle_two():
    assert stu.find_cycle({"a": ["b"], "b": ["a"]}) is not None


def test_find_cycle_three():
    cyc = stu.find_cycle({"a": ["b"], "b": ["c"], "c": ["a"]})
    assert cyc is not None and cyc[0] == cyc[-1]


# ── topo_order ───────────────────────────────────────────────────────────────────

def test_topo_order_deps_first():
    members = [
        {"name": "worker", "depends_on": ["db", "redis", "main"]},
        {"name": "main", "depends_on": ["db", "redis"]},
        {"name": "db", "depends_on": []},
        {"name": "redis", "depends_on": []},
    ]
    order = [m["name"] for m in stu.topo_order(members)]
    # each dependency must appear before the member that depends on it
    assert order.index("db") < order.index("main")
    assert order.index("redis") < order.index("main")
    assert order.index("main") < order.index("worker")


# ── validate_stack_definition ────────────────────────────────────────────────────

def _n8n_members():
    return [
        {"name": "db", "depends_on": [], "ready_condition": "ready",
         "healthchecks": {"readiness": {"tcp_socket_port": 5432}},
         "environment_variables": {"POSTGRES_PASSWORD": "${stack:secrets:PG}"}, "secret_map": {}},
        {"name": "redis", "depends_on": [], "ready_condition": "available",
         "environment_variables": {}, "secret_map": {}},
        {"name": "main", "depends_on": ["db", "redis"], "ready_condition": "available",
         "environment_variables": {"DB_HOST": "${stack:db:host}"}, "secret_map": {}},
    ]


def test_validate_ok():
    assert stu.validate_stack_definition(_n8n_members(), ["PG"]) == []


def test_validate_unknown_depends_on():
    members = _n8n_members()
    members[2]["depends_on"] = ["db", "ghost"]
    errors = stu.validate_stack_definition(members, ["PG"])
    assert any("unknown member 'ghost'" in e for e in errors)


def test_validate_self_dependency():
    members = [{"name": "a", "depends_on": ["a"], "ready_condition": "available"}]
    errors = stu.validate_stack_definition(members, [])
    assert any("cannot depend on itself" in e for e in errors)


def test_validate_cycle():
    members = [
        {"name": "a", "depends_on": ["b"], "ready_condition": "available"},
        {"name": "b", "depends_on": ["a"], "ready_condition": "available"},
    ]
    errors = stu.validate_stack_definition(members, [])
    assert any("cycle" in e for e in errors)


def test_validate_ready_without_probe():
    members = [{"name": "db", "depends_on": [], "ready_condition": "ready", "healthchecks": {}}]
    errors = stu.validate_stack_definition(members, [])
    assert any("no healthchecks.readiness probe" in e for e in errors)


def test_validate_ready_with_probe_ok():
    members = [{"name": "db", "depends_on": [], "ready_condition": "ready",
                "healthchecks": {"readiness": {"exec_command": ["pg_isready"]}}}]
    assert stu.validate_stack_definition(members, []) == []


def test_validate_unknown_stack_secret_ref():
    members = [{"name": "a", "depends_on": [], "ready_condition": "available",
                "environment_variables": {"X": "${stack:secrets:NOPE}"}}]
    errors = stu.validate_stack_definition(members, [])  # no stack secret keys
    assert any("no key 'NOPE'" in e for e in errors)


def test_validate_unknown_member_ref():
    members = [{"name": "a", "depends_on": [], "ready_condition": "available",
                "environment_variables": {"X": "${stack:ghost:host}"}}]
    errors = stu.validate_stack_definition(members, [])
    assert any("no member 'ghost'" in e for e in errors)


# ── compile_member_stack_refs ────────────────────────────────────────────────────

def test_compile_host_ref_to_k8_literal():
    env = {"DB_HOST": "${stack:db:host}"}
    pod_id_by_name = {"db": "myn8ndb"}
    net = {"db": {"default": {"port": 5432, "protocol": "postgres"}}}
    out_env, out_sm = stu.compile_member_stack_refs(env, {}, pod_id_by_name, net, "tacc", "dev")
    assert out_env["DB_HOST"] == "pods-tacc-dev-myn8ndb"


def test_compile_secret_ref_normalized():
    env = {"PW": "${stack:secrets:PG}"}
    out_env, out_sm = stu.compile_member_stack_refs(env, {}, {}, {}, "tacc", "dev")
    # env switches to the conventional ${pods:secrets:KEY}; the stack reference moves to secret_map
    assert out_env["PW"] == "${pods:secrets:PG}"
    assert out_sm["PG"] == "${stack:secrets:PG}"


def test_compile_port_and_protocol():
    env = {"P": "${stack:db:port}", "PROTO": "${stack:db:protocol}"}
    net = {"db": {"default": {"port": 5432, "protocol": "postgres"}}}
    out_env, _ = stu.compile_member_stack_refs(env, {}, {"db": "myn8ndb"}, net, "tacc", "dev")
    assert out_env["P"] == "5432"
    assert out_env["PROTO"] == "postgres"


def test_compile_inline_url_in_connection_string():
    # references can be embedded inside a larger string
    env = {"DSN": "postgres://u:p@${stack:db:host}:5432/n8n"}
    out_env, _ = stu.compile_member_stack_refs(env, {}, {"db": "myn8ndb"}, {}, "tacc", "dev")
    assert out_env["DSN"] == "postgres://u:p@pods-tacc-dev-myn8ndb:5432/n8n"


# ── save_as_template snapshot helpers (sanitize / placeholderize / unbake) ─────────
# These back POST /pods/stacks/{id}/save_as_template. Tested purely (no cluster): the live
# round-trip is flaky against the shared dev cluster's health loop, but the transformation logic
# is deterministic and is where the real bugs live (e.g. a verbatim networking copy used to embed
# runtime-only keys the template model forbids).

_ALLOWED_NET = {"protocol", "port", "url", "ip_allow_list", "tapis_auth"}


def test_sanitize_member_networking_strips_runtime_keys():
    # custom_domain / custom_domain_verified are runtime-only — the template Networking model forbids
    # them, so a snapshot must drop them (and the generated url) while keeping authoring fields.
    live = {"default": {"protocol": "http", "port": 8080, "url": "x.pods.tacc",
                        "custom_domain": "n8n.example.com", "custom_domain_verified": True,
                        "tapis_auth": True}}
    out = stu.sanitize_member_networking(live, _ALLOWED_NET)
    assert out["default"] == {"protocol": "http", "port": 8080, "tapis_auth": True}
    assert "custom_domain" not in out["default"] and "url" not in out["default"]


def test_sanitize_member_networking_passthrough_non_dict():
    assert stu.sanitize_member_networking(None, _ALLOWED_NET) is None
    assert stu.sanitize_member_networking("nope", _ALLOWED_NET) == "nope"


def test_placeholderize_secret_value():
    # concrete values/refs become a required placeholder; stack refs + existing placeholders survive.
    assert stu.placeholderize_secret_value("${secret:alice:dbpw}").startswith("${:?")
    assert stu.placeholderize_secret_value("hunter2").startswith("${:?")
    assert stu.placeholderize_secret_value("${stack:secrets:PG}") == "${stack:secrets:PG}"
    assert stu.placeholderize_secret_value("${:?already}") == "${:?already}"
    assert stu.placeholderize_secret_value("${pods:default:5432}") == "${pods:default:5432}"
    assert stu.placeholderize_secret_value(12345) == 12345  # non-str passthrough


def test_unbake_host_refs_reverses_k8_names():
    # the inverse of compile's host baking: in-cluster k8 name -> portable ${stack:<name>:host}
    k8_to_ref = {"pods-tacc-dev-myn8ndb": "${stack:db:host}"}
    dsn = "postgres://u:p@pods-tacc-dev-myn8ndb:5432/n8n"
    assert stu.unbake_host_refs(dsn, k8_to_ref) == "postgres://u:p@${stack:db:host}:5432/n8n"
    assert stu.unbake_host_refs("plain", k8_to_ref) == "plain"
    assert stu.unbake_host_refs(None, k8_to_ref) is None


def test_compile_then_unbake_roundtrip():
    # compile bakes ${stack:db:host} -> k8 name; unbake restores it. The two must be inverses so a
    # save_as_template snapshot of a from-template stack reproduces the original authoring reference.
    pod_id_by_name = {"db": "myn8ndb"}
    out_env, _ = stu.compile_member_stack_refs(
        {"DB_HOST": "${stack:db:host}"}, {}, pod_id_by_name, {}, "tacc", "dev")
    k8_to_ref = {stu.k8_name("tacc", "dev", "myn8ndb"): "${stack:db:host}"}
    assert stu.unbake_host_refs(out_env["DB_HOST"], k8_to_ref) == "${stack:db:host}"


# ── compute_stack_member_plan (L2 reviewed-update diff) ───────────────────────────


def test_plan_unchanged():
    a = [{"name": "db", "image": "x:1"}, {"name": "web", "image": "w:1"}]
    plan = stu.compute_stack_member_plan(a, list(a))
    assert all(p["kind"] == "unchanged" for p in plan)


def test_plan_add_member():
    plan = stu.compute_stack_member_plan([{"name": "db"}], [{"name": "db"}, {"name": "ml"}])
    ml = [p for p in plan if p["name"] == "ml"][0]
    assert ml["kind"] == "add" and ml["destructive"] is False


def test_plan_remove_member_is_destructive():
    plan = stu.compute_stack_member_plan([{"name": "db"}, {"name": "web"}], [{"name": "db"}])
    web = [p for p in plan if p["name"] == "web"][0]
    assert web["kind"] == "remove" and web["destructive"] is True


def test_plan_image_change_is_recreate():
    plan = stu.compute_stack_member_plan(
        [{"name": "db", "image": "postgres:16"}], [{"name": "db", "image": "postgres:17"}])
    assert plan[0]["kind"] == "recreate" and "image" in plan[0]["changed_fields"]
    assert plan[0]["destructive"] is False


def test_plan_env_change_is_patch():
    plan = stu.compute_stack_member_plan(
        [{"name": "web", "environment_variables": {"A": "1"}}],
        [{"name": "web", "environment_variables": {"A": "1", "B": "2"}}])
    assert plan[0]["kind"] == "patch" and plan[0]["changed_fields"] == ["environment_variables"]


def test_plan_ephemeral_config_change_is_nondestructive_recreate():
    # config_content lives in volume_mounts but carries no persistent data.
    plan = stu.compute_stack_member_plan(
        [{"name": "app", "volume_mounts": {"/c": {"type": "ephemeral", "config_content": "a"}}}],
        [{"name": "app", "volume_mounts": {"/c": {"type": "ephemeral", "config_content": "b"}}}])
    assert plan[0]["kind"] == "recreate"        # volume_mounts changed -> still a recreate
    assert plan[0]["destructive"] is False       # ...but no data at stake


def test_plan_tapisvolume_repoint_is_destructive():
    plan = stu.compute_stack_member_plan(
        [{"name": "db", "volume_mounts": {"/d": {"type": "tapisvolume", "source_id": "vol-a"}}}],
        [{"name": "db", "volume_mounts": {"/d": {"type": "tapisvolume", "source_id": "vol-b"}}}])
    assert plan[0]["kind"] == "recreate" and plan[0]["destructive"] is True


def test_plan_tapisvolume_added_is_destructive():
    plan = stu.compute_stack_member_plan(
        [{"name": "db"}],
        [{"name": "db", "volume_mounts": {"/d": {"type": "tapisvolume", "source_id": "vol-a"}}}])
    assert plan[0]["destructive"] is True


def test_plan_ignores_dict_key_order():
    plan = stu.compute_stack_member_plan(
        [{"name": "w", "environment_variables": {"A": "1", "B": "2"}}],
        [{"name": "w", "environment_variables": {"B": "2", "A": "1"}}])
    assert plan[0]["kind"] == "unchanged"


# ── snapshot minimization (save_as_template hygiene) ──────────────────────────────

_NET_DEFAULTS = {
    "protocol": "http", "port": 5000, "tapis_auth": False,
    "tapis_auth_allowed_users": ["AUTHORIZED_USERS"], "tapis_auth_return_path": "/",
    "cors_allow_origins": [], "proxy_compression": False,
    "proxy_compression_encodings": ["zstd", "br", "gzip"],
}


def test_minimize_networking_resets_leaked_users_to_sentinel():
    entry = {"protocol": "http", "port": 4000, "tapis_auth": True,
             "tapis_auth_allowed_users": ["cgarcia", "jstubbs"]}
    out = stu.minimize_member_networking({"default": entry}, _NET_DEFAULTS)["default"]
    # baked usernames reset to the sentinel (== default) → pruned entirely
    assert "tapis_auth_allowed_users" not in out
    assert out["protocol"] == "http" and out["port"] == 4000
    assert out["tapis_auth"] is True  # non-default kept


def test_minimize_networking_strips_default_fields_keeps_protocol_port():
    entry = {"protocol": "http", "port": 5000, "cors_allow_origins": [],
             "proxy_compression_encodings": ["zstd", "br", "gzip"], "proxy_compression": True}
    out = stu.minimize_member_networking({"default": entry}, _NET_DEFAULTS)["default"]
    assert out == {"protocol": "http", "port": 5000, "proxy_compression": True}


def test_strip_defaults_never_prunes_a_reference():
    out = stu.strip_defaults(
        {"url": "${stack:db:host}", "tapis_auth": False},
        {"url": "", "tapis_auth": False},
    )
    assert out == {"url": "${stack:db:host}"}  # ref kept, default bool dropped


def test_minimize_resources_keeps_positive_ints_only():
    r = stu.minimize_resources(
        {"cpu_request": 250, "cpu_limit": 1000, "ephemeral_storage_request": -1,
         "gpus": 0, "mem_request": None}
    )
    assert r == {"cpu_request": 250, "cpu_limit": 1000}


# ── match_live_member_pod_ids (update reverse-map, id-drift tolerant) ────────────

class _P:
    """Minimal stand-in for a live member Pod (only pod_id + image are read)."""
    def __init__(self, pod_id, image=""):
        self.pod_id = pod_id
        self.image = image


def test_match_exact_convention():
    live = [_P("vartbdb", "postgres:17"), _P("vartbapp", "gatus:v5")]
    out = stu.match_live_member_pod_ids(
        "vartb", {"db": "postgres:17", "app": "gatus:v5"},
        {"db": "unchanged", "app": "patch"}, live)
    assert out == {"db": "vartbdb", "app": "vartbapp"}


def test_match_bare_stack_id_drift_reuses_pod_by_image():
    # The real bug: the 'app' member's pod_id is the bare stack_id 'vartb', so the
    # prefix-strip can't recover its name. It must still map app -> vartb (not a fresh
    # 'vartbapp'), so update reuses the live pod instead of orphaning it.
    live = [_P("vartbdb", "postgres:17"), _P("vartb", "gatus:v5")]
    out = stu.match_live_member_pod_ids(
        "vartb", {"db": "postgres:17", "app": "gatus:v5"},
        {"db": "unchanged", "app": "recreate"}, live)
    assert out == {"db": "vartbdb", "app": "vartb"}


def test_match_single_leftover_taken_even_without_image_match():
    live = [_P("vartbdb", "postgres:17"), _P("weirdname", "someimage")]
    out = stu.match_live_member_pod_ids(
        "vartb", {"db": "postgres:17", "app": "gatus:v5"},
        {"db": "unchanged", "app": "patch"}, live)
    assert out == {"db": "vartbdb", "app": "weirdname"}


def test_match_add_member_never_leftover_matched():
    # 'web' is a brand-new member (kind add): it must NOT steal the unclaimed live pod.
    live = [_P("vartbdb", "postgres:17"), _P("vartb", "gatus:v5")]
    out = stu.match_live_member_pod_ids(
        "vartb", {"db": "postgres:17", "app": "gatus:v5", "web": "nginx"},
        {"db": "unchanged", "app": "patch", "web": "add"}, live)
    assert out == {"db": "vartbdb", "app": "vartb"}
    assert "web" not in out  # add member gets a fresh id downstream


def test_match_removed_member_pod_is_found():
    # A removed member's live pod must still be located so update can delete it.
    live = [_P("vartbdb", "postgres:17"), _P("vartbold", "gatus:v5")]
    out = stu.match_live_member_pod_ids(
        "vartb", {"db": "postgres:17", "old": "gatus:v5"},
        {"db": "unchanged", "old": "remove"}, live)
    assert out == {"db": "vartbdb", "old": "vartbold"}


def test_match_ambiguous_leftovers_left_unmatched():
    # Two unclaimed pods, two drifted members, no image match -> conservative: no guess.
    live = [_P("aaa", "img-x"), _P("bbb", "img-y")]
    out = stu.match_live_member_pod_ids(
        "vartb", {"app": "gatus:v5", "api": "flask:1"},
        {"app": "patch", "api": "patch"}, live)
    assert out == {}  # neither forced; downstream derives fresh ids (surfaced, not silently wrong)
