"""
Tapis Pods — spec-reflecting MCP server.

The tool catalog *is* the live OpenAPI spec. We fetch ``/v3/openapi.json`` at
startup and let FastMCP turn every permitted operation into a tool — so new Pods
endpoints appear as tools on restart with zero code here. The only hand-written
pieces are (1) the curation/scope policy in ``route_maps.py`` and (2) three
generic, template-aware helper tools below. None of it grows with the endpoint
count.

Run (stdio, v1):
    TAPIS_BASE_URL=https://tacc.develop.tapis.io \
    TAPIS_TOKEN=<your X-Tapis-Token> \
    fastmcp run pods_mcp.py

Env:
    TAPIS_BASE_URL    required, tenant base, e.g. https://tacc.develop.tapis.io
                      (where tool CALLS go: {BASE}/v3/pods...)
    TAPIS_TOKEN       your X-Tapis-Token (stdio v1). The server acts *as you*;
                      the service enforces per-user object permissions.
    PODS_SPEC         where to READ the OpenAPI spec — a file path or an http(s)
                      URL, JSON or YAML. Default: the service's own committed
                      dump at ../docs/openapi_v3-pods.yml (includes stacks;
                      refreshed by the service, so new endpoints appear on
                      restart). The public gateway does NOT serve /openapi.json,
                      so a laptop stdio run reads the file; an in-cluster v2 run
                      can point this at the internal service URL.
"""
import json
import os
import sys

import httpx
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import RouteMap  # noqa: F401 (re-exported use)

from route_maps import build_route_maps
import placeholders as ph
import recipes as rx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE = (os.environ.get("TAPIS_BASE_URL") or "").rstrip("/")
if not BASE:
    sys.exit("TAPIS_BASE_URL is required, e.g. https://tacc.develop.tapis.io")

TOKEN = os.environ.get("TAPIS_TOKEN", "")
# Opt-in diagnostic surface: admin health/metrics, per-pod traffic/events, usage,
# all-resource permissions. Off by default; operator sets it, LLM can't escalate.
VERBOSE = os.environ.get("MCP_VERBOSE", "").lower() in ("1", "true", "yes", "on")
_DEFAULT_SPEC = os.path.join(os.path.dirname(__file__), "..", "docs", "openapi_v3-pods.yml")
SPEC_SRC = os.environ.get("PODS_SPEC", _DEFAULT_SPEC)
API_BASE = f"{BASE}/v3"  # reflected paths are "/pods..." (no /v3) -> "{BASE}/v3/pods..."

def _is_write(name: str) -> bool:
    """Classify a tool as write vs read for the catalog TOC (get_* / list_* are reads)."""
    return name.startswith(("set_", "delete_", "deploy_", "update_")) or name == "stack_action"


def _headers() -> dict:
    return {"X-Tapis-Token": TOKEN} if TOKEN else {}


# ---------------------------------------------------------------------------
# Fetch the live spec and build the reflected server
# ---------------------------------------------------------------------------
def _parse_spec(text: str) -> dict:
    text = text.lstrip()
    if text.startswith("{"):
        return json.loads(text)
    import yaml  # lazy: only needed for YAML specs
    return yaml.safe_load(text)


def _fetch_spec() -> dict:
    try:
        if SPEC_SRC.startswith(("http://", "https://")):
            resp = httpx.get(SPEC_SRC, headers=_headers(), timeout=30.0,
                             follow_redirects=True)
            resp.raise_for_status()
            return _parse_spec(resp.text)
        with open(SPEC_SRC, "r") as f:
            return _parse_spec(f.read())
    except Exception as e:  # noqa: BLE001 — startup: fail loudly and usefully
        sys.exit(f"Failed to load OpenAPI spec from {SPEC_SRC}: {e}\n"
                 f"Set PODS_SPEC to a spec file path or http(s) URL "
                 f"(the public gateway does not serve /openapi.json).")


spec = _fetch_spec()

# One async client, shared by the reflected tools AND the helpers below.
client = httpx.AsyncClient(base_url=API_BASE, headers=_headers(), timeout=60.0)

mcp = FastMCP.from_openapi(
    openapi_spec=spec,
    client=client,
    name="Tapis Pods",
    instructions=(
        "Audit convention: EVERY tool call may (and should) include two reserved "
        "arguments that are stripped before execution and recorded in the "
        "human-visible audit trail: `intent` — one short sentence saying WHY you "
        "are making this call; `agent` — who is calling (your model/agent name, "
        "e.g. 'fable-5 orchestrator', 'haiku subagent'). They are not part of any "
        "tool's schema; the server removes them before validation."
    ),
    route_maps=build_route_maps(verbose=VERBOSE),
    # The Tapis response envelope ({status,message,result,...}) can drift from the
    # declared response_model on minor fields; don't fail a proxy call over that.
    validate_output=False,
)


# ---------------------------------------------------------------------------
# Helper: fetch a template tag's full definition and compute its blanks
# ---------------------------------------------------------------------------
def _result(payload):
    """Unwrap the Tapis {result: ...} envelope if present."""
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


async def _get_json(path: str, **kwargs):
    r = await client.get(path, **kwargs)
    r.raise_for_status()
    return r.json()


def _blanks_for(tag_obj: dict) -> dict:
    """Compute deploy blanks from a tag object that carries a stack/pod definition."""
    kind = tag_obj.get("kind")
    if kind == "stack" or tag_obj.get("stack_definition"):
        return {"kind": "stack", **ph.extract_stack_blanks(tag_obj.get("stack_definition"))}
    return {"kind": "pod", **ph.extract_pod_blanks(tag_obj.get("pod_definition"))}


# ---------------------------------------------------------------------------
# TOC-style discovery tool
# ---------------------------------------------------------------------------
@mcp.tool(
    name="catalog",
    description="Table of contents for this server: resources grouped by tag with "
                "each tool's one-line purpose and whether it reads or writes. Call "
                "this FIRST to orient before picking a tool.",
    tags={"meta"},
)
async def catalog() -> dict:
    tools = await mcp.list_tools()
    groups: dict[str, list] = {}
    for t in tools:
        name = getattr(t, "name", "?")
        desc = (getattr(t, "description", "") or "").strip().splitlines()
        summary = desc[0] if desc else ""
        tags = sorted(getattr(t, "tags", None) or [])
        group = tags[0] if tags else "other"
        kind = "write" if _is_write(name) else "read"
        groups.setdefault(group, []).append(
            {"tool": name, "kind": kind, "summary": summary[:140]}
        )
    for g in groups.values():
        g.sort(key=lambda x: (x["kind"] != "read", x["tool"]))
    return {
        "server": "Tapis Pods MCP",
        "how_to_deploy": "Simple case: list_deployable_templates → "
                         "deploy_from_template(template, deploy_id, answers). Multi-step "
                         "deploys that a template can't express (post-boot exec, injected "
                         "creds, imagePullSecret wiring — e.g. a Gitea registry): call "
                         "recipes() then recipe(id) for a copy-pasteable playbook.",
        "recipes": rx.list_recipes(),
        "resources": dict(sorted(groups.items())),
    }


# ---------------------------------------------------------------------------
# Deploy recipes — playbooks for multi-step deploys templates can't express
# ---------------------------------------------------------------------------
@mcp.tool(
    name="recipes",
    description="List deploy 'recipes' — ordered playbooks for multi-step deployments "
                "a single template tag can't express (post-boot exec, secret_map-injected "
                "credentials, Kubernetes imagePullSecret wiring). Returns id + title + "
                "when_to_use; pass an id to recipe() for the full steps.",
    tags={"meta"},
)
async def recipes() -> list:
    return rx.list_recipes()


@mcp.tool(
    name="recipe",
    description="Full playbook for one deploy recipe: prereqs, ordered steps (each with "
                "the concrete API call + a copy-pasteable example body), gotchas, and "
                "deliverables. Get ids from recipes(). NOTE: steps marked 'exec' use "
                "POST /pods/{id}/exec, which is intentionally NOT an MCP tool (RCE) — run "
                "those via the service API directly, with confirmation.",
    tags={"meta"},
)
async def recipe(recipe_id: str) -> dict:
    r = rx.get_recipe(recipe_id)
    if r is None:
        return {"error": "unknown_recipe", "recipe_id": recipe_id,
                "available": [x["id"] for x in rx.list_recipes()]}
    return r


# ---------------------------------------------------------------------------
# Generic template helpers — the "fill only the blanks" creation path
# ---------------------------------------------------------------------------
@mcp.tool(
    name="list_deployable_templates",
    description="List template tags you can deploy, each with its required 'blanks' "
                "(the ${:?...} placeholders you must fill) and optional ones. Use the "
                "returned 'template' + 'required' keys with deploy_from_template.",
    tags={"Templates"},
)
async def list_deployable_templates() -> list:
    # /pods/templates/tags returns a dict keyed by template_id; each template
    # carries an inline "tags" list, and each tag carries its full
    # pod_definition/stack_definition inline — so no per-tag fetch is needed.
    listing = _result(await _get_json("/pods/templates/tags"))
    templates = listing.values() if isinstance(listing, dict) else (listing or [])
    out, seen = [], set()
    for tmpl in templates:
        if not isinstance(tmpl, dict):
            continue
        tmpl_desc = tmpl.get("description") or ""
        for tag in tmpl.get("tags") or []:
            if not isinstance(tag, dict):
                continue
            ref = f"{tag.get('template_id')}:{tag.get('tag')}"
            if ref in seen:  # the API returns every historical version; show each tag once
                continue
            seen.add(ref)
            blanks = _blanks_for(tag)
            out.append({
                "template": ref,
                "kind": blanks.get("kind"),
                "description": tag.get("commit_message") or tmpl_desc,
                "required": [b for b in blanks.get("secrets", []) if b.get("required")],
                "optional": [b for b in blanks.get("secrets", []) if not b.get("required")],
                "member_overrides": blanks.get("member_overrides", []),
            })
    return out


async def _tag_detail(template: str) -> dict | None:
    """Fetch a tag's full definition. get_template_tag returns a LIST (tag
    versions); take the newest/first. Returns None if unreadable."""
    tid, _, tag = template.partition(":")
    try:
        res = _result(await _get_json(f"/pods/templates/{tid}/tags/{tag}"))
        if isinstance(res, list):
            return res[0] if res else None
        return res if isinstance(res, dict) else None
    except Exception:  # noqa: BLE001
        return None


@mcp.tool(
    name="deploy_from_template",
    description="Deploy a pod OR stack from a template tag by filling only its blanks. "
                "'deploy_id' is the pod_id (pod templates) or stack_id (stack "
                "templates). 'answers' maps each required placeholder KEY to a value "
                "(from list_deployable_templates). 'overrides' supplies extra field "
                "overrides — for pods a template_overrides body (e.g. "
                "{'volume_mounts': {'/data': {'source_id': 'vol-x'}}}); for stacks a "
                "per-member override map. 'pod_ids' (stacks only) overrides member ids.",
    tags={"Stacks"},
)
async def deploy_from_template(
    template: str,
    deploy_id: str,
    answers: dict | None = None,
    overrides: dict | None = None,
    pod_ids: dict | None = None,
) -> dict:
    answers = answers or {}
    detail = await _tag_detail(template)
    kind = "stack"
    if detail is not None:
        blanks = _blanks_for(detail)
        kind = blanks.get("kind", "stack")
        # Self-correcting guard: refuse (without calling the API) if a required
        # blank is unfilled, and tell the model exactly which.
        missing = [b["key"] for b in blanks.get("secrets", [])
                   if b.get("required") and b["key"] not in answers]
        if missing:
            return {"error": "missing_required_answers", "missing": missing,
                    "hint": "Call list_deployable_templates to see each key's description."}

    if kind == "pod":
        # Pod templates instantiate via POST /pods; placeholder answers override the
        # template's secret_map through template_overrides.
        t_over: dict = {"secret_map": answers} if answers else {}
        if overrides:
            t_over.update(overrides)
        body: dict = {"pod_id": deploy_id, "template": template}
        if t_over:
            body["template_overrides"] = t_over
        r = await client.post("/pods", json=body)
    else:
        body = {"template": template, "stack_id": deploy_id, "secrets": answers}
        if pod_ids:
            body["pod_ids"] = pod_ids
        if overrides:
            body["overrides"] = overrides
        r = await client.post("/pods/stacks/from-template", json=body)
    r.raise_for_status()
    return r.json()


@mcp.tool(
    name="update_from_template",
    description="Re-derive an existing stack from a newer template tag. Defaults to "
                "dry_run=True so you can preview the change plan before applying; set "
                "dry_run=False to apply. 'answers' fills any NEW required placeholders "
                "the target tag introduces.",
    tags={"Stacks"},
)
async def update_from_template(
    stack_id: str,
    template: str | None = None,
    answers: dict | None = None,
    dry_run: bool = True,
) -> dict:
    body: dict = {"dry_run": dry_run}
    if template:
        body["template"] = template
    if answers:
        body["secrets"] = answers
    r = await client.post(f"/pods/stacks/{stack_id}/update", json=body)
    r.raise_for_status()
    return r.json()


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
