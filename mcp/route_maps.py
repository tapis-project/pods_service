"""
The ONLY policy file. Curation + write-scope live here as declarative FastMCP
RouteMaps — never as per-endpoint code. Adjusting what the LLM can see/do means
editing this list; endpoint logic is never touched, and new Pods endpoints are
picked up automatically on restart.

Matching rules (verified against fastmcp 3.4.2 routing.py):
  * First match wins; iterated top-to-bottom.
  * ``pattern`` is ``re.search``-ed against the route path (paths have NO ``/v3``
    prefix in the raw spec, e.g. ``/pods``, ``/pods/{pod_id}/exec``).
  * ``methods`` is ``"*"`` or a list of upper-case verbs.
  * ``tags`` uses AND (subset) semantics — so we curate by PATH, not tag, to stay
    independent of exact OpenAPI tag strings. Tags still ride along on each tool
    for the ``catalog()`` table-of-contents.
  * IMPORTANT: unmatched routes default to TOOL in fastmcp — so the list MUST end
    with a catch-all EXCLUDE, or everything leaks in.

Scope (confirmed with product owner): the FULL create/read/update surface —
reads + lifecycle + in-pod exec + in-pod/volume/snapshot file reads + secret
creation AND value use + raw creation/update of every resource (pods, volumes,
snapshots, secrets, images, stacks, templates/tags) + permissions. The only
things held back:
  * DESTRUCTIVE deletes of resources (pods/volumes/snapshots/secrets/…). Permission
    deletes (unshare) ARE kept. Flip `EXPOSE_DELETES` to arm resource deletes.
  * multipart uploads (the reflected httpx client can't marshal form-data);
  * oauth/forwardAuth, jupyter special-casing, binary gallery bytes, infra plumbing;
  * the two raw from-template/update ops that the deploy_from_template /
    update_from_template HELPERS already own (kept out to avoid a duplicate tool);
  * (non-verbose only) admin/usage/per-pod observability + non-core permissions.
Every enabled call is audited by the bridge (source=claude-code) — a wide surface
is also what keeps the audit history/heatmap complete instead of leaving
direct-curl blind spots.
"""
from fastmcp.server.providers.openapi import RouteMap, MCPType

EXCLUDE = MCPType.EXCLUDE
TOOL = MCPType.TOOL

# Resource deletes are irreversible; off by default. Set True (or wire an env in
# pods_mcp.py) to reflect delete_* as tools. Permission deletes are kept either way.
EXPOSE_DELETES = False


def build_route_maps(verbose: bool = False) -> list[RouteMap]:
    """Curated tool surface.

    verbose=False (default): the full create/read/update driving surface.
    verbose=True  (MCP_VERBOSE=1): additionally expose the read-only DIAGNOSTIC
        surface — admin health/metrics, per-pod traffic/metrics/events/log-runs,
        usage rollups, and permission management for all resource types. Verbose
        is more VISIBILITY, not more destruction: resource deletes stay gated by
        EXPOSE_DELETES regardless. Server-side (operator) flag; the LLM can't
        escalate into it.
    """
    # Always excluded: multipart uploads, oauth, binary bytes, infra plumbing.
    # (Secret VALUES, exec, and the file readers are intentionally IN now.)
    hard = [
        RouteMap(pattern=r"/upload", mcp_type=EXCLUDE),                           # upload_to_pod / volume upload (multipart)
        RouteMap(pattern=r"/auth(/callback)?$", mcp_type=EXCLUDE),                # forwardAuth / oauth
        RouteMap(pattern=r"/jupyter/", mcp_type=EXCLUDE),                         # special-case pod mgmt
        RouteMap(pattern=r"/gallery/photos/", mcp_type=EXCLUDE),                  # binary image bytes
        RouteMap(pattern=r"^/(traefik-config|error-handler|pod-splash|pod-not-found|healthcheck)",
                 mcp_type=EXCLUDE),                                               # infra endpoints
    ]

    # Helper-owned raw ops: deploy_from_template / update_from_template give a
    # blank-validated, cleaner path for these, so drop the raw reflected versions
    # to avoid a confusing duplicate tool. (Applies in verbose too.)
    helper_owned = [
        RouteMap(methods=["POST"], pattern=r"/stacks/from-template$", mcp_type=EXCLUDE),  # -> deploy_from_template
        RouteMap(methods=["POST"], pattern=r"/stacks/[^/]+/update$", mcp_type=EXCLUDE),   # -> update_from_template
        # The pod file readers take the path as an awkward URL segment ({url_path},
        # no leading slash) and 400 if ?path= is also sent. The list_pod_files /
        # read_pod_file helpers wrap the clean query form, so drop the raw ones.
        # (Volume/snapshot readers use a normal {path} param — they stay.)
        RouteMap(pattern=r"/(list_files|download_from_pod)\{url_path\}", mcp_type=EXCLUDE),
    ]

    # Excluded from the DEFAULT surface, RE-INCLUDED in verbose (all reads / perms).
    diagnostic_excludes = [
        RouteMap(pattern=r"^/pods/admin", mcp_type=EXCLUDE),                       # admin observability
        RouteMap(pattern=r"/usage$", mcp_type=EXCLUDE),                            # usage rollups
        RouteMap(pattern=r"/(events|metrics|traffic|log-runs)(/|$)", mcp_type=EXCLUDE),  # per-pod obs
        RouteMap(pattern=r"/(volumes|snapshots|templates)/[^/]+/permissions", mcp_type=EXCLUDE),  # perms
    ]

    # Permission deletes (unshare) are kept even while resource deletes are gated —
    # matched before the resource-delete exclude below.
    keep = [
        RouteMap(methods=["DELETE"], pattern=r"/permissions/", mcp_type=TOOL),     # unshare
    ]

    # Resource deletes: irreversible, off unless EXPOSE_DELETES.
    deletes = [] if EXPOSE_DELETES else [
        RouteMap(methods=["DELETE"], mcp_type=EXCLUDE),
    ]

    # Everything else under /pods becomes a tool: reads, lifecycle GETs, exec,
    # file readers, secret values, and ALL create/update POST+PUTs (create_pod,
    # create_volume, create_snapshot, create_secret, add_image, add_template,
    # add_template_tag, create_stack, update_*, save_*_as_template, …).
    tail = [
        RouteMap(pattern=r"^/pods", mcp_type=TOOL),
        RouteMap(mcp_type=EXCLUDE),                    # mandatory catch-all
    ]

    return (hard + helper_owned
            + ([] if verbose else diagnostic_excludes)
            + keep + deletes + tail)
