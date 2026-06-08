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
