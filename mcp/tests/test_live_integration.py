"""Live integration tests against a real tenant.

Skipped unless TAPIS_BASE_URL (http...) + TAPIS_TOKEN are set. The deploy
round-trip additionally requires MCP_LIVE_DEPLOY=1 (it creates and then deletes a
real pod), so plain `pytest` never mutates a tenant.

    TAPIS_BASE_URL=https://tacc.develop.tapis.io TAPIS_TOKEN=... \
    MCP_LIVE_DEPLOY=1 pytest tests/test_live_integration.py -v
"""
import asyncio
import os

import httpx
import pytest

TOKEN = os.environ.get("TAPIS_TOKEN")
BASE = os.environ.get("TAPIS_BASE_URL", "")

live = pytest.mark.skipif(not (TOKEN and BASE.startswith("http")),
                          reason="set TAPIS_BASE_URL + TAPIS_TOKEN for live tests")
deploy = pytest.mark.skipif(os.environ.get("MCP_LIVE_DEPLOY") != "1",
                            reason="set MCP_LIVE_DEPLOY=1 to run create/cleanup round-trips")


async def _call(mcp, name, args=None):
    r = await mcp.call_tool(name, args or {})
    d = getattr(r, "structured_content", None)
    if isinstance(d, dict) and set(d.keys()) == {"result"}:
        d = d["result"]
    return d


@live
def test_catalog_live(full_mcp, run):
    cat = run(_call(full_mcp, "catalog"))
    assert "resources" in cat and cat["resources"]


@live
def test_list_deployable_and_blanks(full_mcp, run):
    dep = run(_call(full_mcp, "list_deployable_templates"))
    assert isinstance(dep, list) and dep
    # every entry has a template ref and kind; blanks are structured
    for d in dep:
        assert ":" in d["template"] and d["kind"] in ("pod", "stack")
        for b in d["required"]:
            assert "key" in b and b["required"] is True


@live
def test_deploy_guard_refuses_incomplete(full_mcp, run):
    """A template with required blanks + empty answers must refuse WITHOUT deploying."""
    dep = run(_call(full_mcp, "list_deployable_templates"))
    withreq = next((d for d in dep if d["required"]), None)
    if not withreq:
        pytest.skip("no template with required blanks on this tenant")
    res = run(_call(full_mcp, "deploy_from_template",
                            {"template": withreq["template"], "deploy_id": "mcp-guard-probe",
                             "answers": {}}))
    assert res.get("error") == "missing_required_answers"
    assert set(res["missing"]) == {b["key"] for b in withreq["required"]}


@live
@deploy
def test_pod_round_trip(full_mcp, run):
    """Deploy a no-blank pod template, confirm it exists, then clean up via raw API."""
    pod_id = "mcprtpod"
    dep = run(_call(full_mcp, "list_deployable_templates"))
    tmpl = next((d for d in dep if d["kind"] == "pod" and not d["required"]), None)
    assert tmpl, "need a no-blank pod template"
    client = httpx.Client(base_url=f"{BASE}/v3", headers={"X-Tapis-Token": TOKEN}, timeout=90)
    try:
        res = run(_call(full_mcp, "deploy_from_template",
                                {"template": tmpl["template"], "deploy_id": pod_id}))
        assert "error" not in res, res
        got = client.get(f"/pods/{pod_id}").json()["result"]
        assert got["pod_id"] == pod_id
    finally:
        client.delete(f"/pods/{pod_id}")  # cleanup regardless
