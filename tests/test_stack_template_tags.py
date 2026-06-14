"""
Integration tests for kind='stack' template tags (Part D — stack templates).

Regression cover: a kind='stack' template tag used to be rejected with
"kind='stack' requires a non-empty stack_definition" even when stack_definition was provided. The
cause was a TemplateTag model_validator(mode="after") that cannot read JSON sa_column fields
(pod_definition / stack_definition) at table-model construction time — they read empty. The
kind/mutual-exclusion + structural validation now lives in the add_template_tag endpoint (on the
parsed NewTemplateTag), so a valid stack tag is accepted, stored, and round-trips.

Runs in-process via the FastAPI TestClient (no live HTTP) as part of `make test`.
"""
import sys
import json
import time
import pytest
from tests.test_utils import headers, basic_response_checks

# Allows us to import pods's modules (mirrors tests/test_templates.py).
sys.path.append('/home/tapis/service')
from api import api
from fastapi.testclient import TestClient

client = TestClient(
    api, base_url="https://dev.develop.tapis.io", raise_server_exceptions=False
)

TEMPLATE_ID = "teststacktmpltags"
STACK_ID = "teststackinst"


def _stack_tag_body(tag):
    """A minimal but realistic kind='stack' body: a ready-gated db + an app that depends on it,
    wired with a shared random password and a cross-member host reference."""
    return {
        "kind": "stack",
        "stack_definition": {
            "restart_policy": "ordered",
            "secret_map": {"PG_PASSWORD": "${pods:random:32}"},
            "members": [
                {
                    "name": "db",
                    "image": "postgres:16",
                    "ready_condition": "ready",
                    "networking": {"default": {"protocol": "postgres", "port": 5432}},
                    "healthchecks": {
                        "readiness": {"exec_command": ["pg_isready", "-U", "app"]}
                    },
                    "environment_variables": {
                        "POSTGRES_USER": "app",
                        "POSTGRES_DB": "app",
                        "POSTGRES_PASSWORD": "${stack:secrets:PG_PASSWORD}",
                    },
                },
                {
                    "name": "app",
                    "image": "notchristiangarcia/testserver:fastapi",
                    "depends_on": ["db"],
                    "networking": {"default": {"protocol": "http", "port": 8080}},
                    "environment_variables": {
                        "DB_HOST": "${stack:db:host}",
                        "DB_PASSWORD": "${stack:secrets:PG_PASSWORD}",
                    },
                },
            ],
        },
        "tag": tag,
        "commit_message": "stack tag test: db + app",
    }


def test_create_template_for_stack_tags(headers):
    # best-effort cleanup of a leftover template from a prior run, so the suite is idempotent
    client.delete(f"/pods/templates/{TEMPLATE_ID}?force=true", headers=headers)
    time.sleep(1)
    template_def = {
        "template_id": TEMPLATE_ID,
        "description": "Template holding kind=stack tags for testing",
        "metatags": ["test", "stack"],
    }
    rsp = client.post(
        "/pods/templates", data=json.dumps(template_def), headers=headers
    )
    result = basic_response_checks(rsp)
    assert result["template_id"] == TEMPLATE_ID
    time.sleep(2)


def test_create_valid_stack_tag(headers):
    # Regression: this used to 400 with "kind='stack' requires a non-empty stack_definition".
    rsp = client.post(
        f"/pods/templates/{TEMPLATE_ID}/tags",
        data=json.dumps(_stack_tag_body("stackv1")),
        headers=headers,
    )
    result = basic_response_checks(rsp)
    assert result.get("kind") == "stack"


def test_stack_tag_roundtrips(headers):
    # The stored tag must come back with its stack_definition + members intact.
    rsp = client.get(f"/pods/templates/{TEMPLATE_ID}/tags", headers=headers)
    result = basic_response_checks(rsp)
    stack_tags = [
        t
        for t in result
        if t.get("kind") == "stack" and t.get("tag") == "stackv1"
    ]
    assert stack_tags, "stack tag 'stackv1' not found in tag list"
    sd = stack_tags[0].get("stack_definition")
    assert sd, "stack_definition missing on the retrieved tag"
    names = sorted(m["name"] for m in sd["members"])
    assert names == ["app", "db"]
    # the cross-member + shared-secret references survive storage as-authored
    app = next(m for m in sd["members"] if m["name"] == "app")
    assert app["environment_variables"]["DB_HOST"] == "${stack:db:host}"


def test_create_stack_from_template(headers):
    # The headline flow: instantiate the kind='stack' tag into a Stack + member pods.
    client.delete(  # best-effort cleanup of a prior run
        f"/pods/stacks/{STACK_ID}?delete_pods=true&confirm={STACK_ID}", headers=headers
    )
    # Also force-delete the member pods directly: if a prior run left the stack row gone but a member
    # pod orphaned, the stack-cascade above can't reach it and the id-collision precheck would trip.
    for pid in (STACK_ID + "db", STACK_ID + "app"):
        client.delete(f"/pods/{pid}?force=true", headers=headers)
    time.sleep(1)
    body = {
        "template": f"{TEMPLATE_ID}:stackv1",
        "stack_id": STACK_ID,
        "pod_ids": {"app": STACK_ID + "app"},  # override the public member's id
        "description": "from-template integration test",
    }
    rsp = client.post(
        "/pods/stacks/from-template", data=json.dumps(body), headers=headers
    )
    result = basic_response_checks(rsp)
    assert result["stack_id"] == STACK_ID
    pod_ids = sorted(p["pod_id"] for p in (result.get("pods") or []))
    # db derives {stack_id}db; app overridden to {stack_id}app
    assert pod_ids == sorted([STACK_ID + "db", STACK_ID + "app"]), pod_ids


def test_from_template_stack_get(headers):
    # The stack instantiated above comes back with members in derived/overridden ids and depends_on
    # rewritten from member name -> pod_id.
    rsp = client.get(f"/pods/stacks/{STACK_ID}", headers=headers)
    result = basic_response_checks(rsp)
    assert result["stack_id"] == STACK_ID
    assert len(result.get("pods") or []) == 2
    app = next(p for p in result["pods"] if p["pod_id"] == STACK_ID + "app")
    assert app.get("depends_on") == [STACK_ID + "db"]


# NOTE: the save_as_template round-trip (snapshot a live stack -> kind='stack' tag) is covered by
# *pure* unit tests in test_stack_templates.py (sanitize_member_networking / placeholderize_secret_value
# / unbake_host_refs). A full live-cluster round-trip here proved flaky against the shared dev
# cluster's background health loop (a created stack intermittently not visible to a later, separate
# request), so the deterministic logic is unit-tested instead, keeping this integration file reliable.


def test_cleanup_from_template_stack(headers):
    rsp = client.delete(
        f"/pods/stacks/{STACK_ID}?delete_pods=true&confirm={STACK_ID}", headers=headers
    )
    assert rsp.status_code in [200, 201, 400, 404]


def test_stack_tag_requires_members(headers):
    body = {
        "kind": "stack",
        "stack_definition": {"restart_policy": "ordered", "members": []},
        "tag": "stacknomembers",
        "commit_message": "should fail",
    }
    rsp = client.post(
        f"/pods/templates/{TEMPLATE_ID}/tags",
        data=json.dumps(body),
        headers=headers,
    )
    assert rsp.status_code not in [200, 201], rsp.content


def test_pod_kind_rejects_stack_definition(headers):
    # kind defaults to 'pod'; providing a stack_definition there must be rejected (mutual exclusion).
    body = {
        "stack_definition": {
            "restart_policy": "ordered",
            "members": [{"name": "db", "image": "postgres:16"}],
        },
        "tag": "podwithstack",
        "commit_message": "should fail",
    }
    rsp = client.post(
        f"/pods/templates/{TEMPLATE_ID}/tags",
        data=json.dumps(body),
        headers=headers,
    )
    assert rsp.status_code not in [200, 201], rsp.content


def test_stack_tag_bad_cross_reference(headers):
    # ${stack:ghost:host} references a member that does not exist -> validate_stack_definition rejects.
    body = _stack_tag_body("stackbadref")
    body["stack_definition"]["members"][1]["environment_variables"][
        "DB_HOST"
    ] = "${stack:ghost:host}"
    rsp = client.post(
        f"/pods/templates/{TEMPLATE_ID}/tags",
        data=json.dumps(body),
        headers=headers,
    )
    assert rsp.status_code not in [200, 201], rsp.content


def test_cleanup_template(headers):
    rsp = client.delete(
        f"/pods/templates/{TEMPLATE_ID}?force=true", headers=headers
    )
    assert rsp.status_code in [200, 201, 400, 404]
