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

Scope (confirmed with product owner): read + lifecycle (start/stop/restart are
GETs) + deploy/update-from-template + permission set/delete. Creation of raw
pods/stacks/volumes/etc. is intentionally funneled through templates, so the
9/10-complexity bodies never reach the LLM.
"""
from fastmcp.server.providers.openapi import RouteMap, MCPType

EXCLUDE = MCPType.EXCLUDE
TOOL = MCPType.TOOL

# POST endpoints we DO expose (semantic capabilities, not per-resource creates):
#   apply a lifecycle action, and set a permission. Deploy/update-from-template
#   are handled by the deploy_from_template / update_from_template HELPER tools
#   (blank-validation + cleaner UX), so the raw reflected versions are dropped to
#   avoid a confusing duplicate. Matched before the blanket POST exclude.
_ALLOWED_POST = r"(/stacks/[^/]+/action$|/permissions$)"


def build_route_maps(verbose: bool = False) -> list[RouteMap]:
    """Curated tool surface.

    verbose=False (default): the tight ~35-tool driving surface.
    verbose=True  (MCP_VERBOSE=1): also expose the read-only DIAGNOSTIC surface —
        admin health/metrics, per-pod traffic/metrics/events/log-runs, usage
        rollups, and permission management for all resource types. Still keeps
        exec / delete / raw-creates / secret-values OUT — verbose is more
        VISIBILITY, not more destruction. Server-side (operator) flag; the LLM
        can't escalate into it.
    """
    # Always excluded: dangerous, streaming, secret values, infra plumbing.
    hard = [
        RouteMap(methods=["POST"], pattern=r"/exec$", mcp_type=EXCLUDE),          # arbitrary in-pod cmd = RCE
        RouteMap(pattern=r"/(upload|download|contents/|list_files)", mcp_type=EXCLUDE),  # file streaming
        RouteMap(pattern=r"/(volumes|snapshots)/[^/]+/list$", mcp_type=EXCLUDE),  # file listers
        RouteMap(pattern=r"/secrets/[^/]+/value$", mcp_type=EXCLUDE),             # raw secret VALUES
        RouteMap(pattern=r"/auth(/callback)?$", mcp_type=EXCLUDE),                # forwardAuth / oauth
        RouteMap(pattern=r"/jupyter/", mcp_type=EXCLUDE),                         # special-case pod mgmt
        RouteMap(pattern=r"/gallery/photos/", mcp_type=EXCLUDE),                  # binary image bytes
        RouteMap(pattern=r"^/(traefik-config|error-handler|pod-splash|pod-not-found|healthcheck)",
                 mcp_type=EXCLUDE),                                               # infra endpoints
    ]

    # Excluded from the DEFAULT surface, RE-INCLUDED in verbose (all reads / perms).
    diagnostic_excludes = [
        RouteMap(pattern=r"^/pods/admin", mcp_type=EXCLUDE),                       # admin observability
        RouteMap(pattern=r"/usage$", mcp_type=EXCLUDE),                            # usage rollups
        RouteMap(pattern=r"/(events|metrics|traffic|log-runs)(/|$)", mcp_type=EXCLUDE),  # per-pod obs
        RouteMap(pattern=r"/(volumes|snapshots|templates)/[^/]+/permissions", mcp_type=EXCLUDE),  # perms
    ]

    allowed = [
        RouteMap(methods=["POST"], pattern=_ALLOWED_POST, mcp_type=TOOL),         # action, set-permission
        RouteMap(methods=["DELETE"], pattern=r"/permissions/", mcp_type=TOOL),    # unshare
    ]

    # Blanket mutation excludes: raw creates/updates/resource-deletes dropped
    # (creation goes via deploy_from_template). Applies in verbose too.
    blanket = [
        RouteMap(methods=["POST"], mcp_type=EXCLUDE),
        RouteMap(methods=["PUT"], mcp_type=EXCLUDE),
        RouteMap(methods=["DELETE"], mcp_type=EXCLUDE),
    ]

    tail = [
        RouteMap(pattern=r"^/pods", mcp_type=TOOL),   # everything else under /pods (reads)
        RouteMap(mcp_type=EXCLUDE),                   # mandatory catch-all
    ]

    return hard + ([] if verbose else diagnostic_excludes) + allowed + blanket + tail
