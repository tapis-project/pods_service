import asyncio
import datetime
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import socket
import time

import json
from typing import Any, Dict, Optional

import requests
from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import Response

from errors import ResourceError
from models_node import (
    CHECKIN_INTERVAL_SECONDS,
    Node,
    NewNode,
    NodeJoinRequest,
    NodeCheckinRequest,
    NodesResponse,
    NodeResponse,
    NodeCreateResponse,
    NodeJoinResponse,
    NodeCheckinResponse,
    NodeCommandsResponse,
    NodeDeleteResponse,
    NodeLedgerResponse,
)
from models_routes import (
    Route,
    NewRoute,
    RoutesResponse,
    RouteResponse,
    RouteDeleteResponse,
    RouteProbeResponse,
)
from models_node_telemetry import (
    NodeLog,
    NodeMetric,
    NodeLogIngestResponse,
    NodeLogsResponse,
    NodeMetricsResponse,
)
from node_telemetry_utils import (
    decode_payload,
    sanitize_agent_settings,
    diff_settings,
    parse_json_payload,
    parse_ts,
    normalize_log_entries,
    normalize_metric_samples,
    clamp_window_step,
    downsample_samples,
    latest_extras_caps,
    sanitize_bench_settings,
    supported_encodings,
    METRIC_FIELDS,
)
from models_node_commands import (
    NodeCommand,
    NodeCommandResultIn,
    NodeBenchRequest,
    NodeCommandResponse,
    NodeCommandsListResponse,
    COMMAND_RESULT_MAX_BYTES,
)
from models_pods import Pod
from sqlalchemy import delete as sa_delete, select as sa_select, update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from tapis_auth_utils import TapisAuthEntity, run_tapis_auth_check, run_tapis_auth_callback
from tapisservice.tapisfastapi.utils import g, ok
from tapisservice.logs import get_logger

logger = get_logger(__name__)

router = APIRouter()

# Agent contract tunables — env-overridable, defaults are sane for develop.
# CHECKIN_INTERVAL_SECONDS lives in models_node (shared with liveness derivation).
CLAIM_TTL_HOURS = int(os.environ.get("NODES_CLAIM_TTL_HOURS", "4"))
COMMANDS_POLL_AFTER_SECONDS = int(os.environ.get("NODES_COMMANDS_POLL_AFTER", "25"))
DEFAULT_LOGIN_SERVER = os.environ.get("TS_LOGIN_SERVER", "https://headscale.pods.tacc.develop.tapis.io")
# Extra permitted login servers, comma-separated. The default is always allowed.
LOGIN_SERVER_ALLOWLIST = os.environ.get("TS_LOGIN_SERVER_ALLOWLIST", "")


def _check_login_server(value: str):
    """Exact-match against the operator-controlled allowlist. Join sends the
    headscale ADMIN key (TS_API_KEY) as a bearer to this URL, so it must never
    be attacker-nameable (R2). Checked at create AND at every use, so legacy
    rows with a bad value fail loudly instead of leaking the key. Allowlisted
    values are operator-approved by definition — a non-https entry (local dev
    headscale) warns rather than blocks."""
    v = (value or "").rstrip("/")
    allowed = {s.strip().rstrip("/") for s in LOGIN_SERVER_ALLOWLIST.split(",") if s.strip()}
    allowed.add(DEFAULT_LOGIN_SERVER.rstrip("/"))
    if v not in allowed:
        raise ResourceError(
            f"login_server '{value}' is not an allowed control plane. Allowed: "
            f"{sorted(allowed)} (extend via TS_LOGIN_SERVER_ALLOWLIST).", 400)
    if not v.startswith("https://"):
        logger.warning(f"login_server '{v}' is not https — the headscale admin key rides this connection in the clear.")

# Telemetry quotas/retention (Phase 3) — per-node hard caps enforced at ingest so one
# hot edge can never flood central (transport discipline #4). Caps are advertised in
# ingest responses so agents can size their offline buffers to what will be kept.
NODES_LOGS_MAX_BODY_BYTES = int(os.environ.get("NODES_LOGS_MAX_BODY_BYTES", str(8 * 1024 * 1024)))  # decompressed
NODES_LOGS_MAX_BATCH = int(os.environ.get("NODES_LOGS_MAX_BATCH", "5000"))
NODES_LOGS_MAX_LINE_CHARS = int(os.environ.get("NODES_LOGS_MAX_LINE_CHARS", "8192"))
NODES_LOGS_MAX_ROWS = int(os.environ.get("NODES_LOGS_MAX_ROWS", "100000"))          # per node
NODES_LOGS_MAX_AGE_DAYS = int(os.environ.get("NODES_LOGS_MAX_AGE_DAYS", "7"))
NODES_METRICS_MAX_BATCH = int(os.environ.get("NODES_METRICS_MAX_BATCH", "1500"))    # ~25 h @ 60 s
NODES_METRICS_MAX_ROWS = int(os.environ.get("NODES_METRICS_MAX_ROWS", "100000"))    # per node
NODES_METRICS_MAX_AGE_DAYS = int(os.environ.get("NODES_METRICS_MAX_AGE_DAYS", "30"))

# Checkin blob caps — status and inventory are agent-controlled JSON written
# verbatim to the node row; a compromised agent (it holds a valid token) could
# otherwise ship a giant blob to OOM central at parse or bloat the row unbounded.
# These are generous vs. a real agent's sub-KB status / modest inventory.
NODES_STATUS_MAX_BYTES = int(os.environ.get("NODES_STATUS_MAX_BYTES", str(256 * 1024)))
NODES_INVENTORY_MAX_BYTES = int(os.environ.get("NODES_INVENTORY_MAX_BYTES", str(1024 * 1024)))
NODES_CAPABILITIES_MAX = int(os.environ.get("NODES_CAPABILITIES_MAX", "128"))
NODES_ACTION_LOG_MAX = int(os.environ.get("NODES_ACTION_LOG_MAX", "500"))  # ring cap

# Agents authenticate with this header on checkin/commands — NOT Authorization, so the
# Tapis token middleware never tries to parse it as a JWT.
AGENT_TOKEN_HEADER = "X-Pods-Node-Token"

# Long-poll (transport discipline #3): the agent's ordinary GET /commands is
# answered SLOWLY on purpose — held up to commands_wait seconds, returning
# EARLY the moment a command is queued or the settings overlay changes. The
# direction never changes (edges are behind NAT; the agent always initiates);
# central just takes its time saying "nothing yet". Correctness rests on a
# ~1 s DB re-check inside the hold (replica-safe); the in-process wake below is
# purely a latency optimization for the common single-worker case.
COMMANDS_MAX_WAIT = int(os.environ.get("NODES_COMMANDS_MAX_WAIT", "20"))

_COMMAND_WAKES: Dict[str, "asyncio.Event"] = {}


def _wake_key(node) -> str:
    return f"{node.site_id}:{node.tenant_id}:{node.node_id}"


def wake_commands_poll(node):
    """Wake a held commands poll for this node (best effort — same-process
    only; other workers/replicas catch up on their next 1 s DB re-check)."""
    try:
        ev = _COMMAND_WAKES.get(_wake_key(node))
        if ev:
            ev.set()
    except Exception:
        pass


# Token helpers ---------------------------------------------------------------
# Raw tokens are returned exactly once (create/regenerate → claim, join → agent token);
# only SHA-256 hashes are stored. Prefixes make leaked tokens greppable/identifiable:
# pnc_ = pods node claim, pna_ = pods node agent.

def _mint_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _token_matches(token, stored_hash) -> bool:
    if not token or not stored_hash:
        return False
    return hmac.compare_digest(_hash_token(token), stored_hash)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


# Request helpers -------------------------------------------------------------

def _get_node_or_404(node_id: str) -> Node:
    node = Node.db_get_with_pk(node_id, tenant=g.request_tenant_id, site=getattr(g, 'site_id', None))
    if not node:
        raise ResourceError(f"Node with node_id: '{node_id}' not found.", 404)
    return node


def _require_agent(request: Request, node: Node):
    """Authenticate an agent request via the node-scoped bearer token minted at join.

    During a rotation BOTH the active token and the pending (newly minted, not yet
    confirmed) token authenticate — that overlap is what makes rotation zero-downtime.
    Returns True when the caller used the PENDING token (the confirmation signal)."""
    token = request.headers.get(AGENT_TOKEN_HEADER)
    if _token_matches(token, node.agent_token_hash):
        used_pending = False
    elif node.pending_agent_token_hash and _token_matches(token, node.pending_agent_token_hash):
        used_pending = True
    else:
        raise ResourceError(f"Invalid or missing {AGENT_TOKEN_HEADER} for node '{node.node_id}'.", 403)
    # Agent requests carry no Tapis token, so g.username is unset — give db writes an actor.
    if not getattr(g, 'username', None):
        g.username = f"_node_agent_{node.node_id}"
    return used_pending


def _central_base_url(request: Request) -> str:
    # Host header is the tenant base URL when fronted by Traefik/nginx; good enough for
    # the join one-liner and the checkin endpoints payload (refreshed every heartbeat).
    # X-Forwarded-Proto keeps local/dev deployments on http instead of a forced https.
    scheme = request.headers.get('x-forwarded-proto') or getattr(request.url, 'scheme', None) or 'https'
    host = request.headers.get('host') or str(request.url.hostname or '')
    return f"{scheme}://{host}/v3"


def _login_server_for(node: Node) -> str:
    ls = node.login_server or DEFAULT_LOGIN_SERVER
    _check_login_server(ls)
    return ls


def _agent_endpoints(request: Request, node: Node) -> dict:
    """Config-as-data: current central endpoints, republished on every checkin so agents
    never rely on stale cached values (decision 5 in ROADMAP_EDGE_REMOTE)."""
    base = _central_base_url(request)
    endpoints = {
        "api_base": f"{base}/pods",
        "login_server": _login_server_for(node),
        # Phase 3 log shipping target — also usable by a Vector HTTP sink later
        # (same endpoint, same X-Pods-Node-Token header, config-only opt-in).
        "log_ingest": f"{base}/pods/nodes/{node.node_id}/logs",
        "log_encodings": ",".join(supported_encodings()),
        # Long-poll capability: agents that understand it hold GET /commands
        # open this many seconds between heartbeats; older agents ignore the
        # key and keep classic polling — config-as-data, no flag day.
        "commands_wait": COMMANDS_MAX_WAIT,
    }
    # Agent self-update: advertise the source central can serve, with the exact
    # sha256 the agent must verify before exec'ing it. Keys are simply absent
    # when central has no agent copy (image built without agent/, no dev mount).
    info = _agent_source_info()
    if info:
        endpoints["agent_source"] = f"{base}/pods/nodes/{node.node_id}/agent-source"
        endpoints["agent_source_sha256"] = info["sha256"]
        endpoints["agent_source_version"] = info["version"]
    return endpoints


# Agent source serving (self-update) ------------------------------------------
# Central serves agent/pods_agent.py itself: the agent is ONE stdlib-only file
# and its state dir is persistent, so an edge can fetch + hash-verify + exec the
# new copy — no image rebuild, no rm/rerun. mtime-keyed cache keeps dev edits
# live; PODS_AGENT_SOURCE overrides the search path (also how in-container tests
# point at a fixture).

_AGENT_SOURCE_CACHE: Dict[str, Any] = {}


def _agent_source_candidates():
    return [
        os.environ.get("PODS_AGENT_SOURCE") or "",
        "/home/tapis/agent/pods_agent.py",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "agent", "pods_agent.py"),
    ]


def _agent_source_info() -> Optional[Dict[str, Any]]:
    """{"bytes", "sha256", "version", "path"} of the servable agent source, or
    None when central has no copy. Re-resolved per call (env + mtime) so dev
    edits and test fixtures are picked up without restarts."""
    path = next((c for c in _agent_source_candidates() if c and os.path.isfile(c)), None)
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
        c = _AGENT_SOURCE_CACHE
        if c.get("path") != path or c.get("mtime") != mtime:
            with open(path, "rb") as f:
                raw = f.read()
            m = re.search(rb'^AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', raw, re.M)
            c.clear()
            c.update({
                "path": path, "mtime": mtime, "bytes": raw,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "version": m.group(1).decode() if m else "unknown",
            })
        return c
    except OSError as e:
        logger.warning(f"agent source at {path} unreadable: {e}")
        return None


# Headscale provisioning ------------------------------------------------------

def provision_tailscale_preauthkey(node: Node):
    """Create a headscale preauth key for the node via the headscale API.
    Returns the key string, or None when no TS_API_KEY is configured (dev setups may
    pre-join nodes to the tailnet manually)."""
    login_server = _login_server_for(node).rstrip("/")
    api_key = os.environ.get("TS_API_KEY")
    if not api_key:
        logger.warning(f"TS_API_KEY not set; skipping preauth key provisioning for node {node.node_id}.")
        return None

    ts_user = os.environ.get("TS_USER", "pods")
    expiration_hours = int(os.environ.get("TS_KEY_EXPIRATION_HOURS", "4"))
    expiration = _utcnow() + datetime.timedelta(hours=expiration_hours)

    # headscale >= 0.26 removed name-strings from the preauthkey API — `user`
    # must be the numeric user ID (a name 400s). TS_USER accepts either form:
    # digits pass through, names resolve via GET /api/v1/user.
    if not str(ts_user).isdigit():
        try:
            uresp = requests.get(
                f"{login_server}/api/v1/user",
                headers={"Authorization": f"Bearer {api_key}"},
                params={"name": ts_user},
                timeout=15,
            )
        except requests.RequestException as e:
            logger.error(f"headscale user lookup failed for node {node.node_id}: {e}")
            raise ResourceError(f"Could not reach headscale at {login_server} to resolve user '{ts_user}'.", 502)
        users = (uresp.json() or {}).get("users", []) if uresp.status_code == 200 else []
        match = next((u for u in users if u.get("name") == ts_user), None)
        if not match or not match.get("id"):
            logger.error(
                f"headscale user '{ts_user}' not found for node {node.node_id}: "
                f"{uresp.status_code} {uresp.text[:300]}")
            raise ResourceError(
                f"headscale user '{ts_user}' does not exist — create it "
                f"(`headscale users create {ts_user}`) or set TS_USER to its numeric ID.", 502)
        ts_user = str(match["id"])

    try:
        resp = requests.post(
            f"{login_server}/api/v1/preauthkey",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "user": int(ts_user),
                "reusable": False,
                "ephemeral": False,
                "expiration": expiration.isoformat() + "Z",
            },
            timeout=15,
        )
    except requests.RequestException as e:
        logger.error(f"headscale preauth request failed for node {node.node_id}: {e}")
        raise ResourceError(f"Could not reach headscale at {login_server} to provision a preauth key.", 502)
    if resp.status_code != 200:
        logger.error(f"headscale preauth provisioning failed for node {node.node_id}: {resp.status_code} {resp.text[:300]}")
        raise ResourceError(f"headscale preauth key provisioning failed ({resp.status_code}).", 502)

    key_info = resp.json().get("preAuthKey", {})
    node.ts_preauthkey_id = key_info.get("id")
    node.ts_preauthkey_expires = expiration
    return key_info.get("key")


# User-facing endpoints (Tapis JWT auth) --------------------------------------

@router.get(
    "/pods/nodes",
    tags=["Nodes"],
    summary="list_nodes",
    operation_id="list_nodes",
    response_model=NodesResponse)
async def list_nodes():
    """List nodes the requesting user has READ access to."""
    logger.info("GET /pods/nodes - Top of list_nodes.")
    nodes = Node.db_get_all_with_permission(user=g.username, level='READ', tenant=g.request_tenant_id, site=g.site_id)
    return ok(result=[node.display() for node in nodes], msg="Nodes retrieved successfully.")


@router.post(
    "/pods/nodes",
    tags=["Nodes"],
    summary="create_node",
    operation_id="create_node",
    response_model=NodeCreateResponse)
async def create_node(new_node: NewNode, request: Request):
    """Create a node and mint its single-use claim token.

    The claim token (and the join one-liner embedding it) is returned exactly once here;
    only its hash is stored. Run the join command on the node before the token expires.
    """
    logger.info("POST /pods/nodes - Top of create_node.")

    # Node creation is admin-only for now. Two reasons, one practical and one security:
    #   1. There are no /nodes/{id}/permissions endpoints yet, so a node is creator-only
    #      and cannot be shared — an open create gives a regular user surface they cannot
    #      actually use with anyone else.
    #   2. NewNode carries login_server — the URL join sends the headscale ADMIN api
    #      key to. It is now allowlist-validated (_check_login_server), so the key-
    #      exfiltration angle is closed even once creation reopens (R28).
    # The route stays codes.NONE because no object exists yet for an object-level check.
    # Roadmap: admin mints a node ON BEHALF OF a user (owner field + claim token handed
    # over), which is what should replace this gate — not handing out admin.
    if not getattr(g, 'admin', False):
        raise ResourceError(
            "Creating nodes currently requires admin. Ask an admin to create the node "
            "and hand you its claim token — the join command is all an operator needs.",
            403)

    if new_node.login_server:
        _check_login_server(new_node.login_server)

    node = Node(**new_node.dict())

    claim_token = _mint_token("pnc")
    node.claim_token_hash = _hash_token(claim_token)
    node.claim_token_expires = _utcnow() + datetime.timedelta(hours=CLAIM_TTL_HOURS)

    node.db_create()
    logger.debug(f"Node saved in db. node_id: {node.node_id}; tenant: {g.request_tenant_id}.")

    join_command = f"pods-agent join --url {_central_base_url(request)}/pods --node {node.node_id} --token {claim_token} --tenant {g.request_tenant_id}"
    return ok(
        result={
            "node": node.display(),
            "claim_token": claim_token,
            "claim_token_expires": node.claim_token_expires,
            "join_command": join_command,
        },
        msg="Node created. Run the join command on the node before the claim token expires — the token is shown only once.")


@router.get(
    "/pods/nodes/{node_id}",
    tags=["Nodes"],
    summary="get_node",
    operation_id="get_node",
    response_model=NodeResponse)
async def get_node(node_id: str):
    """Get a node's details."""
    logger.info(f"GET /pods/nodes/{node_id} - Top of get_node.")
    node = _get_node_or_404(node_id)
    return ok(result=node.display(), msg="Node retrieved successfully.")


DECOMMISSION_TIMEOUT_MINUTES = lambda: int(os.environ.get("NODES_DECOMMISSION_TIMEOUT_MINUTES", "60"))


def _hard_delete_node(node: Node):
    """The full cascade: routes, telemetry, commands, then the row. The agent's
    token stops working the instant the row is gone."""
    for route in Route.db_get_for_node(node.node_id, tenant=g.request_tenant_id, site=g.site_id):
        route.db_delete()
    store = _telemetry_store(NodeLog)
    store.run("execute", sa_delete(NodeLog).where(NodeLog.node_id == node.node_id))
    store.run("execute", sa_delete(NodeMetric).where(NodeMetric.node_id == node.node_id))
    store.run("execute", sa_delete(NodeCommand).where(NodeCommand.node_id == node.node_id))
    node.db_delete()


@router.delete(
    "/pods/nodes/{node_id}",
    tags=["Nodes"],
    summary="delete_node",
    operation_id="delete_node",
    response_model=NodeDeleteResponse)
async def delete_node(
    node_id: str,
    decommission: bool = Query(False, description="Instead of deleting immediately, ask the agent to remove itself first: the row enters a decommissioning state, the agent picks up a decommission command on its next poll, confirms, and the row hard-deletes on that confirmation (or after the timeout). Plain delete always works as the force path."),
):
    """Delete a node (node ADMIN).

    Plain delete: the row + routes + telemetry go now; the agent's token dies with
    it, and the agent on the box parks itself within a few heartbeats (it is NOT
    removed — the box owner removes it, and the agent logs the exact commands).

    ?decommission=true: the polite ordering — the agent is asked to remove itself
    FIRST (wipe its state/token, remove its own container when it can), and the
    row deletes on the agent's confirmation or after the timeout. Plain delete
    remains available the whole time as the immediate force path.
    """
    logger.info(f"DELETE /pods/nodes/{node_id} (decommission={decommission}) - Top of delete_node.")
    node = _get_node_or_404(node_id)
    # TODO Phase 2: expire the headscale node/preauth key so the machine leaves the tailnet.

    if decommission:
        if not node.agent_token_hash:
            # Never claimed — there is no agent to ask; instant delete is the
            # honest interpretation, and the message says which path ran.
            _hard_delete_node(node)
            return ok(result="", msg="Node deleted immediately — it was never claimed, so there was no agent to decommission.")
        if node.decommission_ts:
            raise ResourceError(
                f"Already decommissioning (since {node.decommission_ts}). The agent has "
                f"until the {DECOMMISSION_TIMEOUT_MINUTES()}-minute timeout to confirm; "
                f"a plain delete (no ?decommission) force-removes the row right now.", 409)
        _expire_or_reject_active(node, "decommission", "decommission")
        # A decommission supersedes any pending restart/update — mark them, don't 409.
        store = _telemetry_store(NodeCommand)
        store.run("execute",
                  sa_update(NodeCommand)
                  .where(NodeCommand.node_id == node.node_id,
                         NodeCommand.type.in_(["restart", "update"]),
                         NodeCommand.status.in_(["queued", "delivered"]))
                  .values(status="error",
                          result={"error": "superseded by decommission"},
                          completed_ts=_utcnow()))
        cmd = _queue_lifecycle_command(node, "decommission", {})
        node.decommission_ts = _utcnow()
        node.log_action(
            f"decommission requested by '{getattr(g, 'username', '?')}' ({cmd.command_id[:14]}…) — "
            f"agent will remove itself; row deletes on its confirmation or after "
            f"{DECOMMISSION_TIMEOUT_MINUTES()} minutes")
        node.db_update(user_update=False)
        return ok(result="",
                  msg=f"Decommission queued — the agent picks it up on its next poll (within one "
                      f"checkin interval), removes what it can of itself (full removal bare-host and "
                      f"docker-with-socket; a socket-less container still needs one docker rm), and "
                      f"this node then deletes itself on the confirmation — or automatically after "
                      f"{DECOMMISSION_TIMEOUT_MINUTES()} minutes. If you already stopped the agent "
                      f"yourself it can never confirm: use plain delete to force-remove now.")

    _hard_delete_node(node)
    return ok(result="", msg="Node deleted successfully.")


@router.post(
    "/pods/nodes/{node_id}/regenerate",
    tags=["Nodes"],
    summary="regenerate_node_claim",
    operation_id="regenerate_node_claim",
    response_model=NodeCreateResponse)
async def regenerate_node_claim(node_id: str, request: Request):
    """Re-key a node: mint a fresh single-use claim token and revoke the current agent token.

    This is the ONLY way to re-issue credentials — bootstrap material is never readable
    after creation. The running agent (if any) is cut off until it re-joins.
    """
    logger.info(f"POST /pods/nodes/{node_id}/regenerate - Top of regenerate_node_claim.")
    node = _get_node_or_404(node_id)

    claim_token = _mint_token("pnc")
    node.claim_token_hash = _hash_token(claim_token)
    node.claim_token_expires = _utcnow() + datetime.timedelta(hours=CLAIM_TTL_HOURS)
    node.agent_token_hash = None
    node.claimed_at = None
    node.log_action(f"claim token regenerated + agent token revoked by '{g.username}'")
    node.db_update()

    join_command = f"pods-agent join --url {_central_base_url(request)}/pods --node {node.node_id} --token {claim_token} --tenant {g.request_tenant_id}"
    return ok(
        result={
            "node": node.display(),
            "claim_token": claim_token,
            "claim_token_expires": node.claim_token_expires,
            "join_command": join_command,
        },
        msg="Claim token regenerated and agent token revoked. Re-run the join command on the node.")


# Agent endpoints (no Tapis token — see NO_TOKEN_ROUTES in auth.py) ------------

@router.post(
    "/pods/nodes/{node_id}/join",
    tags=["Nodes"],
    summary="join_node",
    operation_id="join_node",
    response_model=NodeJoinResponse)
async def join_node(node_id: str, join_req: NodeJoinRequest, request: Request):
    """Agent claim-token exchange. Authenticated by the claim token itself; consumed on success.

    Returns (exactly once) the node-scoped agent bearer token, a headscale preauth key,
    and the config bundle the agent needs for its checkin loop.
    """
    logger.info(f"POST /pods/nodes/{node_id}/join - Top of join_node.")
    node = _get_node_or_404(node_id)

    if not node.claim_token_hash:
        raise ResourceError("No outstanding claim token for this node — ask an admin to POST /regenerate.", 403)
    if node.claim_token_expires and node.claim_token_expires < _utcnow():
        raise ResourceError("Claim token expired — ask an admin to POST /regenerate.", 403)
    if not _token_matches(join_req.claim_token, node.claim_token_hash):
        raise ResourceError("Invalid claim token.", 403)

    # Agent requests carry no Tapis token — give db writes an actor before any update.
    if not getattr(g, 'username', None):
        g.username = f"_node_agent_{node.node_id}"

    agent_token = _mint_token("pna")
    node.agent_token_hash = _hash_token(agent_token)
    node.claim_token_hash = None
    node.claim_token_expires = None
    node.claimed_at = _utcnow()
    node.last_checkin_ts = _utcnow()
    if join_req.agent_version:
        node.agent_version = join_req.agent_version
    if join_req.capabilities:
        node.capabilities = join_req.capabilities

    ts_preauthkey = provision_tailscale_preauthkey(node)

    node.log_action(f"claimed by agent (version: {join_req.agent_version or 'unknown'})")
    node.db_update()

    return ok(
        result={
            "node_id": node.node_id,
            "node_token": agent_token,
            "login_server": _login_server_for(node),
            "ts_preauthkey": ts_preauthkey,
            "central_base_url": f"{_central_base_url(request)}/pods",
            "checkin_interval_seconds": CHECKIN_INTERVAL_SECONDS,
            "namespace": node.namespace,
        },
        msg="Node joined — store node_token securely; it is shown only once.")


@router.post(
    "/pods/nodes/{node_id}/checkin",
    tags=["Nodes"],
    summary="checkin_node",
    operation_id="checkin_node",
    response_model=NodeCheckinResponse)
async def checkin_node(node_id: str, checkin: NodeCheckinRequest, request: Request):
    """Agent heartbeat: capabilities + status + hash-gated inventory.

    Inventory protocol: the agent always sends inventory_hash; it includes the full
    inventory only when the hash changed. If central has no matching inventory stored,
    the response sets resync=true and the agent sends the full inventory next checkin.
    """
    node = _get_node_or_404(node_id)
    _require_agent(request, node)

    # Agent-controlled JSON is bounded before it touches the row — a valid token
    # is not licence to OOM/bloat central. Oversize is a 413, not a silent trim,
    # so a misbehaving agent is visible rather than quietly truncated.
    if checkin.status is not None and len(json.dumps(checkin.status)) > NODES_STATUS_MAX_BYTES:
        raise ResourceError(f"status exceeds {NODES_STATUS_MAX_BYTES} bytes.", 413)
    if checkin.inventory is not None and len(json.dumps(checkin.inventory)) > NODES_INVENTORY_MAX_BYTES:
        raise ResourceError(f"inventory exceeds {NODES_INVENTORY_MAX_BYTES} bytes.", 413)
    if checkin.capabilities and len(checkin.capabilities) > NODES_CAPABILITIES_MAX:
        raise ResourceError(f"capabilities list exceeds {NODES_CAPABILITIES_MAX} entries.", 413)

    node.last_checkin_ts = _utcnow()
    # Expire an unconfirmed rotation: the agent is demonstrably alive on some
    # token, so a pending that never got confirmed is dead weight — drop it and
    # leave the active token exactly as it was.
    if node.pending_token_ts and node.pending_token_ts < _utcnow() - datetime.timedelta(
            minutes=PENDING_TOKEN_TTL_MINUTES):
        node.pending_agent_token_hash = None
        node.pending_token_ts = None
        node.log_action(
            f"pending rotation token expired after {PENDING_TOKEN_TTL_MINUTES} minutes "
            f"without confirmation — current token unchanged and still active")
    if checkin.agent_version:
        node.agent_version = checkin.agent_version
    if checkin.capabilities:
        node.capabilities = checkin.capabilities
    if checkin.status:
        # Adoption ledger: when the agent's reported applied_settings change,
        # record it — the audit trail closes the loop on settings pushes.
        new_applied = checkin.status.get("applied_settings")
        old_applied = (node.status or {}).get("applied_settings")
        if new_applied is not None and new_applied != old_applied:
            node.log_action(f"agent adopted settings: {json.dumps(new_applied, sort_keys=True)[:400]}")
        # Storage-watch ledger: EDGE-triggered only — a path crossing into warn
        # and a warn clearing both get one entry; steady states never ledger.
        new_watches = checkin.status.get("watches")
        old_watches = (node.status or {}).get("watches") or {}
        if isinstance(new_watches, dict):
            for path, w in new_watches.items():
                if not isinstance(w, dict):
                    continue
                old_w = old_watches.get(path) if isinstance(old_watches.get(path), dict) else {}
                new_state, old_state = w.get("state"), old_w.get("state")
                if new_state == "warn" and old_state != "warn":
                    node.log_action(
                        f"storage watch WARN: {path} at {w.get('pct', '?')}% "
                        f"({w.get('used_h', '?')} used, threshold {w.get('threshold', '?')})")
                elif new_state == "ok" and old_state == "warn":
                    node.log_action(
                        f"storage watch cleared: {path} back under threshold "
                        f"({w.get('pct', '?')}%, {w.get('used_h', '?')} used)")
        node.status = checkin.status

    resync = False
    if checkin.inventory is not None:
        node.inventory = checkin.inventory
        node.inventory_hash = checkin.inventory_hash
    elif checkin.inventory_hash and checkin.inventory_hash != node.inventory_hash:
        resync = True

    # Phase 3: metrics samples ride the heartbeat (30-60 s agent cadence, batched —
    # an offline agent flushes its whole buffer on reconnect). (node_id, ts) dedupe
    # makes lost-ack resends harmless; a 200 tells the agent to clear its buffer.
    if checkin.metrics_samples:
        try:
            m_rows, m_dropped = normalize_metric_samples(
                checkin.metrics_samples, _utcnow(), NODES_METRICS_MAX_BATCH)
        except ValueError as e:
            logger.warning(f"checkin metrics_samples rejected for node {node.node_id}: {e}")
            m_rows, m_dropped = [], len(checkin.metrics_samples)
        if m_dropped:
            logger.info(f"checkin metrics for node {node.node_id}: dropped {m_dropped} sample(s).")
        if m_rows:
            values = [{**r, "node_id": node.node_id,
                       "tenant_id": node.tenant_id, "site_id": node.site_id} for r in m_rows]
            store = _telemetry_store(NodeMetric)
            store.run("execute",
                      pg_insert(NodeMetric).values(values).on_conflict_do_nothing(
                          index_elements=["node_id", "ts"]))
            _prune_telemetry(store, NodeMetric, node.node_id,
                             NODES_METRICS_MAX_ROWS, NODES_METRICS_MAX_AGE_DAYS)

    node.db_update(user_update=False)

    return ok(
        result={
            "endpoints": _agent_endpoints(request, node),
            "resync": resync,
            "poll_after_seconds": CHECKIN_INTERVAL_SECONDS,
            "desired": {},
            "settings": node.agent_settings or {},
        },
        msg="Checkin recorded.")


@router.get(
    "/pods/nodes/{node_id}/commands",
    tags=["Nodes"],
    summary="get_node_commands",
    operation_id="get_node_commands",
    response_model=NodeCommandsResponse)
async def get_node_commands(
    node_id: str,
    request: Request,
    wait: int = Query(0, description="Long-poll hold in seconds (0 = classic immediate poll; clamped to the advertised commands_wait). While held, the server re-checks ~1/s and answers EARLY on a new command or a settings change — in-process triggers wake it instantly."),
):
    """Pending one-shot commands for this node (dispatcher v1 — first consumer: bench).

    Queued commands are handed over EXACTLY ONCE (status -> delivered here); the agent
    reports completion via POST .../commands/{command_id}/result. No redelivery in v1 —
    a delivered-but-never-completed command stays visible in the run history as such.

    With ?wait=N this becomes a LONG POLL: an empty queue holds the request open up to
    N seconds, so a standing "call me when you have something" line exists made purely
    of agent-initiated GETs — queue semantics unchanged, only WHEN the dequeue attempt
    happens moves. The response always piggybacks the current settings overlay, and a
    settings change ends the hold early — command delivery AND settings adoption both
    drop to sub-second on long-polling agents.
    """
    node = _get_node_or_404(node_id)
    _require_agent(request, node)
    wait = max(0, min(int(wait), COMMANDS_MAX_WAIT))
    deadline = time.monotonic() + wait
    settings_at_hold = json.dumps(node.agent_settings or {}, sort_keys=True)

    store = _telemetry_store(NodeCommand)
    while True:
        stmt = (
            sa_select(NodeCommand)
            .where(NodeCommand.node_id == node.node_id, NodeCommand.status == "queued")
            .order_by(NodeCommand.created_ts.asc()))
        queued = store.run("execute", stmt, scalars=True, all=True)
        if queued:
            store.run("execute",
                      sa_update(NodeCommand)
                      .where(NodeCommand.command_id.in_([c.command_id for c in queued]))
                      .values(status="delivered", delivered_ts=_utcnow()))
            # A rotate's params carry the RAW new token, needed exactly once — in
            # this response (built from the in-memory rows above). Scrub the DB
            # copy at handoff so the only durable copy is the hash on the node row.
            rotate_ids = [c.command_id for c in queued if c.type == "rotate"]
            if rotate_ids:
                store.run("execute",
                          sa_update(NodeCommand)
                          .where(NodeCommand.command_id.in_(rotate_ids))
                          .values(params={}))
        settings_changed = (
            json.dumps(node.agent_settings or {}, sort_keys=True) != settings_at_hold)
        if queued or settings_changed or time.monotonic() >= deadline:
            return ok(
                result={
                    "commands": [
                        {"command_id": c.command_id, "type": c.type, "params": c.params or {}}
                        for c in queued
                    ],
                    "poll_after_seconds": COMMANDS_POLL_AFTER_SECONDS,
                    "settings": node.agent_settings or {},
                },
                msg=(f"{len(queued)} pending command(s)." if queued
                     else "Settings changed while polling." if settings_changed
                     else "No pending commands."))

        # Hold: an in-process wake (instant) or the 1 s tick (replica-safe floor).
        # The sleep is async — held requests cost no threads and ~zero CPU.
        key = _wake_key(node)
        ev = _COMMAND_WAKES.get(key)
        if ev is None or ev.is_set():
            ev = asyncio.Event()
            _COMMAND_WAKES[key] = ev
        try:
            await asyncio.wait_for(ev.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        # Re-resolve from the DB each tick: cross-worker settings changes are
        # seen here, and a node deleted mid-hold ends the poll with the 404
        # the agent's park logic already understands.
        node = _get_node_or_404(node_id)


@router.post(
    "/pods/nodes/{node_id}/commands/{command_id}/result",
    tags=["Nodes"],
    summary="post_node_command_result",
    operation_id="post_node_command_result",
    response_model=NodeCommandResponse)
async def post_node_command_result(node_id: str, command_id: str, body: NodeCommandResultIn, request: Request):
    """Agent completion report for a delivered command (X-Pods-Node-Token auth)."""
    node = _get_node_or_404(node_id)
    used_pending = _require_agent(request, node)

    if body.status not in ("done", "error"):
        raise ResourceError(f"Command result status must be done|error, got '{body.status}'.", 400)
    result = body.result or {}
    if len(json.dumps(result)) > COMMAND_RESULT_MAX_BYTES:
        raise ResourceError(f"Command result exceeds {COMMAND_RESULT_MAX_BYTES} bytes.", 400)

    store = _telemetry_store(NodeCommand)
    cmd = store.run(
        "execute",
        sa_select(NodeCommand).where(
            NodeCommand.command_id == command_id, NodeCommand.node_id == node.node_id),
        scalars=True, first=True)
    if not cmd or cmd.status not in ("queued", "delivered"):
        raise ResourceError(f"Command '{command_id}' not found or already completed.", 404)
    completion = dict(status=body.status, result=result, completed_ts=_utcnow())
    if cmd.type == "rotate":
        # Invariant: a rotate row past `queued` never holds the raw token
        # (delivery already scrubbed it; this covers rows queued pre-scrub).
        completion["params"] = {}
    store.run("execute",
              sa_update(NodeCommand)
              .where(NodeCommand.command_id == command_id)
              .values(**completion))
    # Lifecycle commands close their loop in the ledger — the trigger entry said
    # "watch the ledger", so the completion (or failure detail) must land there.
    if cmd.type in ("restart", "update"):
        detail = result.get("detail") or result.get("error") or ""
        node.log_action(
            f"agent {cmd.type} {'completed' if body.status == 'done' else 'FAILED'}"
            f" ({command_id[:14]}…){': ' + str(detail)[:200] if detail else ''}")
        node.db_update(user_update=False)
    elif cmd.type == "rotate":
        # Promotion requires the agent to have used the NEW token for this very
        # request — that IS the proof it persisted the token successfully.
        # Every rotate outcome entry carries the command-id prefix — the UI's
        # banner watcher matches completions by it (same contract as restart/update).
        if body.status == "done" and used_pending and node.pending_agent_token_hash:
            node.agent_token_hash = node.pending_agent_token_hash
            node.pending_agent_token_hash = None
            node.pending_token_ts = None
            node.log_action(
                f"token rotation CONFIRMED ({command_id[:14]}…) with the new token — "
                f"old token revoked (the token itself is never shown anywhere: it exists "
                f"only on the box; this confirmation is the proof it landed)")
        elif body.status == "done":
            node.log_action(
                f"token rotation NOT PROMOTED ({command_id[:14]}…) — the agent reported done "
                f"but confirmed with the OLD token; the pending token expires and the "
                f"current one stays active")
        else:
            node.pending_agent_token_hash = None
            node.pending_token_ts = None
            node.log_action(
                f"token rotation FAILED ({command_id[:14]}…): "
                f"{str(result.get('error', ''))[:200]} — pending token discarded, "
                f"current token still active")
        node.db_update(user_update=False)
    elif cmd.type == "shell":
        exit_code = result.get("exit_code")
        node.log_action(
            f"shell {'completed' if body.status == 'done' else 'FAILED'} ({command_id[:14]}…"
            f"{f', exit {exit_code}' if exit_code is not None else ''}): "
            f"{str((cmd.params or {}).get('command', ''))[:200]}")
        node.db_update(user_update=False)
    elif cmd.type == "decommission":
        if body.status == "done":
            # The agent confirmed its removal plan — this ack is its last authed
            # request by design (state/token are wiped right after posting it).
            # The row and every dependent go now.
            logger.info(
                f"node {node.node_id}: agent confirmed decommission "
                f"({result.get('outcome', '?')}: {str(result.get('detail', ''))[:200]}) — deleting row.")
            _hard_delete_node(node)
            return ok(result={"command_id": command_id, "status": body.status},
                      msg="Decommission confirmed — node deleted.")
        # error = the agent refused/failed; keep the row so the timeout or a
        # force delete resolves it, and say why in the ledger.
        node.log_action(
            f"agent decommission FAILED ({command_id[:14]}…): "
            f"{str(result.get('error', ''))[:200]} — row kept; force delete or the "
            f"timeout will remove it")
        node.db_update(user_update=False)
    return ok(result={"command_id": command_id, "status": body.status},
              msg="Command result recorded.")


# Bench — the dispatcher's first consumer (user-triggered edge benchmark) ------

@router.post(
    "/pods/nodes/{node_id}/bench",
    tags=["Nodes"],
    summary="trigger_node_bench",
    operation_id="trigger_node_bench",
    response_model=NodeCommandResponse)
async def trigger_node_bench(node_id: str, bench: NodeBenchRequest):
    """Queue a benchmark run on this node (node USER permission).

    Settings are sanitized/clamped server-side; the agent picks the command up on its
    next command poll (within one checkin interval) and posts the report back. One
    bench at a time per node — a still-active run rejects new triggers.
    """
    node = _get_node_or_404(node_id)

    store = _telemetry_store(NodeCommand)
    active = store.run(
        "execute",
        sa_select(NodeCommand).where(
            NodeCommand.node_id == node.node_id,
            NodeCommand.type == "bench",
            NodeCommand.status.in_(["queued", "delivered"])),
        scalars=True, all=True)
    # Stale actives (agent died mid-run / never picked up) auto-expire so one bad
    # run can't lock benching forever.
    fresh_cutoff = _utcnow() - datetime.timedelta(minutes=15)
    if any(c.created_ts and c.created_ts > fresh_cutoff for c in active):
        raise ResourceError("A benchmark is already queued or running on this node — wait for it to finish (or up to 15 minutes for a dead run to expire).", 409)
    if active:
        store.run("execute",
                  sa_update(NodeCommand)
                  .where(NodeCommand.command_id.in_([c.command_id for c in active]))
                  .values(status="error",
                          result={"error": "expired — never completed within 15 minutes"},
                          completed_ts=_utcnow()))

    cmd = NodeCommand(
        command_id=_mint_token("nc"),
        node_id=node.node_id,
        type="bench",
        params=sanitize_bench_settings(bench.dict(exclude_unset=True)),
        status="queued",
        requested_by=getattr(g, 'username', '') or '',
        created_ts=_utcnow(),
        tenant_id=node.tenant_id,
        site_id=node.site_id,
    )
    cmd.db_create()
    wake_commands_poll(node)  # a held long-poll delivers this sub-second
    node.log_action(f"bench queued by '{getattr(g, 'username', '?')}' ({cmd.command_id[:14]}…, dry_run={cmd.params.get('dry_run', True)})")
    node.db_update(user_update=False)
    return ok(result=cmd.display(),
              msg="Benchmark queued — the agent picks it up on its next command poll (within one checkin interval).")


# Agent lifecycle — restart + self-update over the dispatcher -----------------
# The primitives that dissolve the rm/rerun dance: restart re-execs the agent in
# place (works identically bare-host and in-container — os.execv keeps PID 1
# alive, no restart policy required), update fetches central's own advertised
# source, sha256-verifies, compile-checks, writes atomically to the persistent
# state dir and execs onto it. Every step is ledgered and verbose.

def _expire_or_reject_active(node: Node, cmd_type: str, verb: str):
    """One active command of a type at a time; dead runs auto-expire after 15 min
    (same discipline as bench)."""
    store = _telemetry_store(NodeCommand)
    active = store.run(
        "execute",
        sa_select(NodeCommand).where(
            NodeCommand.node_id == node.node_id,
            NodeCommand.type == cmd_type,
            NodeCommand.status.in_(["queued", "delivered"])),
        scalars=True, all=True)
    fresh_cutoff = _utcnow() - datetime.timedelta(minutes=15)
    if any(c.created_ts and c.created_ts > fresh_cutoff for c in active):
        raise ResourceError(
            f"A {verb} is already queued or in progress on this node — wait for it "
            f"to complete (or up to 15 minutes for a dead one to expire).", 409)
    if active:
        values = dict(status="error",
                      result={"error": "expired — never completed within 15 minutes"},
                      completed_ts=_utcnow())
        if cmd_type == "rotate":
            # Never let a raw token linger at rest on a dead rotate. (Other types
            # keep params — shell/bench history shows what was asked.)
            values["params"] = {}
        store.run("execute",
                  sa_update(NodeCommand)
                  .where(NodeCommand.command_id.in_([c.command_id for c in active]))
                  .values(**values))


def _queue_lifecycle_command(node: Node, cmd_type: str, params: Dict[str, Any]) -> NodeCommand:
    cmd = NodeCommand(
        command_id=_mint_token("nc"),
        node_id=node.node_id,
        type=cmd_type,
        params=params,
        status="queued",
        requested_by=getattr(g, 'username', '') or '',
        created_ts=_utcnow(),
        tenant_id=node.tenant_id,
        site_id=node.site_id,
    )
    cmd.db_create()
    wake_commands_poll(node)  # a held long-poll delivers this sub-second
    return cmd


@router.get(
    "/pods/nodes/{node_id}/agent-source",
    tags=["Nodes"],
    summary="get_node_agent_source",
    operation_id="get_node_agent_source")
async def get_node_agent_source(node_id: str, request: Request):
    """The agent source central serves for self-update (agent-token authed —
    the same X-Pods-Node-Token channel as checkin). Raw python text; the
    version and sha256 ride response headers AND the checkin endpoints dict, so
    the agent verifies the bytes against a value from a separate request."""
    node = _get_node_or_404(node_id)
    _require_agent(request, node)
    info = _agent_source_info()
    if not info:
        raise ResourceError(
            "Central has no agent source to serve (service image built without "
            "agent/, and no PODS_AGENT_SOURCE override) — self-update unavailable.", 503)
    return Response(
        content=info["bytes"],
        media_type="text/x-python",
        headers={
            "X-Agent-Version": info["version"],
            "X-Agent-Sha256": info["sha256"],
        })


@router.post(
    "/pods/nodes/{node_id}/restart",
    tags=["Nodes"],
    summary="trigger_node_restart",
    operation_id="trigger_node_restart",
    response_model=NodeCommandResponse)
async def trigger_node_restart(node_id: str):
    """Queue an agent restart (node ADMIN). The agent acks the command, then
    re-execs itself in place — same process slot, so it works for bare-host AND
    containerized agents without any restart policy. Expect one missed heartbeat
    and a fresh startup-milestone waterfall; state (token, cursors, adopted
    settings) persists across the restart."""
    node = _get_node_or_404(node_id)
    _expire_or_reject_active(node, "restart", "restart")
    _expire_or_reject_active(node, "update", "self-update")
    cmd = _queue_lifecycle_command(node, "restart", {})
    node.log_action(f"agent restart queued by '{getattr(g, 'username', '?')}' ({cmd.command_id[:14]}…)")
    node.db_update(user_update=False)
    return ok(result=cmd.display(),
              msg="Restart queued — a long-polling (0.5.0+) agent picks it up within "
                  "seconds, older agents within one checkin interval; it acks, then "
                  "re-execs in place.")


@router.post(
    "/pods/nodes/{node_id}/update",
    tags=["Nodes"],
    summary="trigger_node_update",
    operation_id="trigger_node_update",
    response_model=NodeCommandResponse)
async def trigger_node_update(node_id: str):
    """Queue an agent self-update (node ADMIN). The agent fetches central's
    advertised source, verifies its sha256 against the checkin-advertised value,
    compile-checks it, writes it atomically to the persistent state dir (keeping
    the previous copy as a fallback), and execs onto it. Requires the node's
    effective `allow_self_update` setting to be on — checked here for a clear
    error instead of a silent agent-side refusal, and enforced again by the
    agent itself."""
    node = _get_node_or_404(node_id)

    info = _agent_source_info()
    if not info:
        raise ResourceError(
            "Central has no agent source to serve (service image built without "
            "agent/, and no PODS_AGENT_SOURCE override) — self-update unavailable.", 503)

    # Effective setting precheck: what the agent REPORTS applying wins (it may be
    # env-pinned); fall back to the stored central overlay before first report.
    applied = ((node.status or {}).get("applied_settings") or {})
    effective = applied.get("allow_self_update",
                            (node.agent_settings or {}).get("allow_self_update", False))
    if not effective:
        raise ResourceError(
            "Self-update is disabled on this node (allow_self_update is off). Enable it "
            "in the node's Options (settings channel — the agent adopts it within one "
            "heartbeat) or set PODS_AGENT_ALLOW_SELF_UPDATE=true on the box, then retry.", 403)

    running = node.agent_version or "unknown"
    if running == info["version"]:
        # Same version is allowed (dev iterates without bumping), but say so.
        note = f" (agent already reports {running} — same-version refresh)"
    else:
        note = ""

    _expire_or_reject_active(node, "update", "self-update")
    _expire_or_reject_active(node, "restart", "restart")
    cmd = _queue_lifecycle_command(node, "update", {
        "to_version": info["version"],
        "sha256": info["sha256"],
    })
    node.log_action(
        f"agent self-update queued by '{getattr(g, 'username', '?')}': "
        f"{running} -> {info['version']} (sha {info['sha256'][:12]}…, {cmd.command_id[:14]}…)")
    node.db_update(user_update=False)
    return ok(result=cmd.display(),
              msg=f"Self-update to {info['version']} queued{note} — picked up within seconds "
                  f"by long-polling (0.5.0+) agents, within one checkin interval otherwise. "
                  f"The agent verifies the sha256, compile-checks, keeps the previous copy as "
                  f"fallback, and re-execs. Watch the ledger for the completion entry.")


SHELL_TIMEOUT_DEFAULT = int(os.environ.get("NODES_SHELL_TIMEOUT_DEFAULT", "60"))
SHELL_TIMEOUT_MAX = int(os.environ.get("NODES_SHELL_TIMEOUT_MAX", "300"))
SHELL_COMMAND_MAX_CHARS = 4096


@router.post(
    "/pods/nodes/{node_id}/shell",
    tags=["Nodes"],
    summary="trigger_node_shell",
    operation_id="trigger_node_shell",
    response_model=NodeCommandResponse)
async def trigger_node_shell(node_id: str, body: Dict[str, Any] = Body(...)):
    """Queue a shell command on this node (node ADMIN).

    The strictest capability in the system, so its enable is the strictest too:
    the box must set PODS_AGENT_ALLOW_SHELL=true — central CANNOT turn this on
    through the settings channel (unlike self-update). The agent reports the
    effective value in its status; this endpoint refuses early when it is off so
    the operator gets a real explanation instead of a silent agent-side refusal.

    Body: {"command": "df -h /scratch", "timeout": 60}. The command is recorded
    verbatim in the ledger with the requester, and its exit code is recorded on
    completion — every shell run is auditable after the fact.
    """
    node = _get_node_or_404(node_id)

    command = (body or {}).get("command")
    if not isinstance(command, str) or not command.strip():
        raise ResourceError("A non-empty 'command' string is required.", 400)
    command = command.strip()
    if len(command) > SHELL_COMMAND_MAX_CHARS:
        raise ResourceError(f"Command exceeds {SHELL_COMMAND_MAX_CHARS} characters.", 400)
    timeout = (body or {}).get("timeout", SHELL_TIMEOUT_DEFAULT)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        timeout = SHELL_TIMEOUT_DEFAULT
    timeout = max(1, min(int(timeout), SHELL_TIMEOUT_MAX))

    status = node.status or {}
    applied = status.get("applied_settings") or {}
    if not applied.get("allow_shell"):
        raise ResourceError(
            "Shell commands are disabled on this node. This one is env-only by design: "
            "set PODS_AGENT_ALLOW_SHELL=true on the box itself and restart the agent — "
            "central cannot enable it remotely. (If the agent predates shell support, "
            "update it first.)", 403)

    _expire_or_reject_active(node, "shell", "shell command")
    cmd = _queue_lifecycle_command(node, "shell", {"command": command, "timeout": timeout})
    node.log_action(
        f"shell queued by '{getattr(g, 'username', '?')}' ({cmd.command_id[:14]}…, "
        f"timeout {timeout}s): {command[:300]}")
    node.db_update(user_update=False)
    return ok(result=cmd.display(),
              msg=f"Shell command queued (timeout {timeout}s) — picked up within seconds by "
                  f"long-polling agents. Output and exit code come back in the run history; "
                  f"the command and its exit code are recorded in this node's ledger.")


@router.get(
    "/pods/nodes/{node_id}/shell",
    tags=["Nodes"],
    summary="list_node_shell_runs",
    operation_id="list_node_shell_runs",
    response_model=NodeCommandsListResponse)
async def list_node_shell_runs(node_id: str):
    """Shell run history for a node (node READ permission), newest first."""
    node = _get_node_or_404(node_id)
    store = _telemetry_store(NodeCommand)
    runs = store.run(
        "execute",
        sa_select(NodeCommand)
        .where(NodeCommand.node_id == node.node_id, NodeCommand.type == "shell")
        .order_by(NodeCommand.created_ts.desc())
        .limit(20),
        scalars=True, all=True)
    return ok(result=[c.display() for c in runs], msg=f"Retrieved {len(runs)} shell run(s).")


PENDING_TOKEN_TTL_MINUTES = int(os.environ.get("NODES_PENDING_TOKEN_TTL_MINUTES", "60"))


@router.post(
    "/pods/nodes/{node_id}/rotate",
    tags=["Nodes"],
    summary="trigger_node_rotate",
    operation_id="trigger_node_rotate",
    response_model=NodeCommandResponse)
async def trigger_node_rotate(node_id: str):
    """Rotate this node's agent token with NO downtime (node ADMIN).

    Unlike /regenerate — which revokes immediately and parks the agent until a
    human re-runs a join on the box — this is a two-phase handshake over the
    already-authed channel:

      1. central mints a new token and stores it as PENDING (both tokens work),
      2. the new token rides a `rotate` command to the agent,
      3. the agent persists it and CONFIRMS using the new token,
      4. that confirmation promotes pending -> active and revokes the old one.

    A rotate that never lands (agent offline, crash mid-swap) simply expires —
    the running agent keeps working on its existing token the whole time.
    """
    node = _get_node_or_404(node_id)
    if not node.agent_token_hash:
        raise ResourceError(
            "This node has never been claimed — there is no agent token to rotate. "
            "Use the join command from Add node (or Regenerate for a fresh claim token).", 409)
    _expire_or_reject_active(node, "rotate", "token rotation")

    new_token = _mint_token("pna")
    node.pending_agent_token_hash = _hash_token(new_token)
    node.pending_token_ts = _utcnow()
    cmd = _queue_lifecycle_command(node, "rotate", {"node_token": new_token})
    node.log_action(
        f"token rotation started by '{getattr(g, 'username', '?')}' ({cmd.command_id[:14]}…) — "
        f"both tokens valid until the agent confirms with the new one")
    node.db_update(user_update=False)
    return ok(result=cmd.display(),
              msg="Token rotation queued — the new token rides the authed command channel; "
                  "both tokens work until the agent confirms with the new one, which revokes "
                  "the old. If it never confirms, the agent keeps running on its current token "
                  f"and the pending token expires after {PENDING_TOKEN_TTL_MINUTES} minutes.")


@router.get(
    "/pods/nodes/{node_id}/bench",
    tags=["Nodes"],
    summary="list_node_bench_runs",
    operation_id="list_node_bench_runs",
    response_model=NodeCommandsListResponse)
async def list_node_bench_runs(node_id: str):
    """Bench run history for a node (node READ permission), newest first."""
    node = _get_node_or_404(node_id)
    store = _telemetry_store(NodeCommand)
    stmt = (
        sa_select(NodeCommand)
        .where(NodeCommand.node_id == node.node_id, NodeCommand.type == "bench")
        .order_by(NodeCommand.created_ts.desc())
        .limit(20))
    runs = store.run("execute", stmt, scalars=True, all=True)
    return ok(result=[c.display() for c in runs], msg=f"Retrieved {len(runs)} bench run(s).")


# Settings channel + audit ledger ---------------------------------------------

@router.put(
    "/pods/nodes/{node_id}/settings",
    tags=["Nodes"],
    summary="update_node_settings",
    operation_id="update_node_settings",
    response_model=NodeResponse)
async def update_node_settings(node_id: str, settings: Dict[str, Any] = Body(...)):
    """Replace this node's central agent settings (node ADMIN permission).

    The dict is a SPARSE overlay — absent keys mean agent defaults. Unknown or
    invalid keys are ignored (reported in the response message). The agent
    adopts changes within seconds on long-polling (0.5.0+) agents — the change
    wakes any held commands poll, which piggybacks the new overlay — and on the
    next heartbeat otherwise; env vars on the box always win over these, and
    the agent reports env-pinned keys back in its status. Every change (and
    every agent adoption) lands in the node's action ledger.
    """
    node = _get_node_or_404(node_id)
    clean, ignored = sanitize_agent_settings(settings)
    changes = diff_settings(node.agent_settings or {}, clean)
    node.agent_settings = clean
    node.log_action(f"agent settings changed by '{getattr(g, 'username', '?')}': {changes}")
    node.db_update(user_update=False)
    # A held long-poll ends early on settings changes (piggybacked in its
    # response) — adoption drops from one-heartbeat to sub-second.
    wake_commands_poll(node)
    msg = "Agent settings updated — the agent adopts them on its next heartbeat (env vars on the box still win)."
    if ignored:
        msg += f" Ignored unknown/invalid keys: {ignored}."
    return ok(result=node.display(), msg=msg)


@router.get(
    "/pods/nodes/{node_id}/ledger",
    tags=["Nodes"],
    summary="get_node_ledger",
    operation_id="get_node_ledger",
    response_model=NodeLedgerResponse)
async def get_node_ledger(node_id: str):
    """The node's action ledger, newest first (node READ permission) — creation,
    re-keys, settings changes + agent adoptions, bench triggers."""
    node = _get_node_or_404(node_id)
    return ok(result=list(reversed(node.action_logs or [])),
              msg=f"{len(node.action_logs or [])} ledger entr(ies).")


# Telemetry — Phase 3: agent-shipped logs + metrics history -------------------
# Write paths are agent-authenticated (X-Pods-Node-Token) like checkin; read
# paths are Tapis-JWT + node-READ gated via the route allowlist. Per-node
# retention (rows + age) is enforced at ingest, never by a background sweeper.

def _telemetry_store(model):
    _, _, store = model.get_site_tenant_session(tenant=g.request_tenant_id, site=getattr(g, 'site_id', None))
    return store


def _prune_telemetry(store, model, node_id: str, max_rows: int, max_age_days: int):
    """Per-node retention: drop rows past the age cap, then rows beyond the row
    cap (oldest first). The row-cap boundary subquery yields NULL when the node
    is under cap, which makes the second delete a no-op."""
    cutoff = _utcnow() - datetime.timedelta(days=max_age_days)
    store.run("execute", sa_delete(model).where(model.node_id == node_id, model.ts < cutoff))
    boundary = (
        sa_select(model.id)
        .where(model.node_id == node_id)
        .order_by(model.id.desc())
        .offset(max_rows).limit(1)
        .scalar_subquery())
    store.run("execute", sa_delete(model).where(model.node_id == node_id, model.id <= boundary))


def _log_retention_info() -> dict:
    """Advertised in every ingest response so agents size offline buffers to
    what the server will actually keep."""
    return {
        "max_batch": NODES_LOGS_MAX_BATCH,
        "max_line_chars": NODES_LOGS_MAX_LINE_CHARS,
        "max_rows_per_node": NODES_LOGS_MAX_ROWS,
        "max_age_days": NODES_LOGS_MAX_AGE_DAYS,
        "max_body_bytes": NODES_LOGS_MAX_BODY_BYTES,
        "encodings": supported_encodings(),
    }


@router.post(
    "/pods/nodes/{node_id}/logs",
    tags=["Nodes"],
    summary="ingest_node_logs",
    operation_id="ingest_node_logs",
    response_model=NodeLogIngestResponse)
async def ingest_node_logs(
    node_id: str,
    request: Request,
    dry_run: bool = Query(False, description="Decode, validate, and TIME the batch through the full path but store nothing — the bench suite's default mode, so benchmarks never pollute real logs or churn retention."),
):
    """Agent log shipping (X-Pods-Node-Token auth — same path as checkin).

    Body (identity/gzip/zstd per Content-Encoding): {"entries": [{"source", "ts", "line"}]}.
    source = container name or "agent"; ts = epoch seconds or ISO-8601 (falls back to
    receipt time). Batches are clamped (batch size, line length, decompressed body
    bytes) and per-node retention (rows + age) is applied immediately — the response
    reports accepted/dropped/truncated plus the caps so agents can adapt. `timings`
    (decode/insert ms) lets benchmarks split wall-clock into network vs server time.
    """
    node = _get_node_or_404(node_id)
    _require_agent(request, node)

    body = await request.body()
    t0 = time.monotonic()
    try:
        raw = decode_payload(body, request.headers.get("content-encoding"), NODES_LOGS_MAX_BODY_BYTES)
        payload = parse_json_payload(raw)
        rows, dropped, truncated = normalize_log_entries(
            payload.get("entries"), _utcnow(), NODES_LOGS_MAX_BATCH, NODES_LOGS_MAX_LINE_CHARS)
    except ValueError as e:
        raise ResourceError(f"Log ingest rejected: {e}", 400)
    decode_ms = round((time.monotonic() - t0) * 1000, 2)

    insert_ms = None
    if rows and not dry_run:
        now = _utcnow()
        values = [{**r, "node_id": node.node_id, "ingest_ts": now,
                   "tenant_id": node.tenant_id, "site_id": node.site_id} for r in rows]
        store = _telemetry_store(NodeLog)
        t1 = time.monotonic()
        store.run("execute", pg_insert(NodeLog).values(values))
        insert_ms = round((time.monotonic() - t1) * 1000, 2)
        _prune_telemetry(store, NodeLog, node.node_id, NODES_LOGS_MAX_ROWS, NODES_LOGS_MAX_AGE_DAYS)

    return ok(
        result={"accepted": len(rows), "dropped": dropped, "truncated": truncated,
                "dry_run": dry_run,
                "timings": {"decode_ms": decode_ms, "insert_ms": insert_ms,
                            "wire_bytes": len(body), "decoded_bytes": len(raw)},
                "retention": _log_retention_info()},
        msg=f"{'Timed (dry run, not stored)' if dry_run else 'Stored'} {len(rows)} log line(s).")


@router.get(
    "/pods/nodes/{node_id}/logs",
    tags=["Nodes"],
    summary="get_node_logs",
    operation_id="get_node_logs",
    response_model=NodeLogsResponse)
async def get_node_logs(
    node_id: str,
    source: Optional[str] = Query(None, description="Only lines from this source (container name or 'agent')."),
    since: Optional[str] = Query(None, description="Only lines after this time (epoch seconds or ISO-8601) — tail-follow with the newest ts you have."),
    before: Optional[str] = Query(None, description="Only lines before this time — page back from a previous page's oldest ts."),
    limit: int = Query(500, ge=1, le=5000, description="Max lines returned (newest window of the match, oldest-first in the response)."),
):
    """Stored log lines for a node (node READ permission).

    Returns the NEWEST `limit` lines matching the filters, oldest-first for display,
    plus the distinct source list (viewer filter chips) and has_more for paging back.
    """
    node = _get_node_or_404(node_id)
    store = _telemetry_store(NodeLog)

    stmt = sa_select(NodeLog).where(NodeLog.node_id == node.node_id)
    if source:
        stmt = stmt.where(NodeLog.source == source)
    for name, value, op in (("since", since, "gt"), ("before", before, "lt")):
        if value:
            dt = parse_ts(value)
            if dt is None:
                raise ResourceError(f"'{name}' must be epoch seconds or ISO-8601, got: {value}", 400)
            stmt = stmt.where(NodeLog.ts > dt if op == "gt" else NodeLog.ts < dt)
    stmt = stmt.order_by(NodeLog.ts.desc(), NodeLog.id.desc()).limit(limit + 1)
    log_rows = store.run("execute", stmt, scalars=True, all=True)

    has_more = len(log_rows) > limit
    page = list(reversed(log_rows[:limit]))
    src_stmt = sa_select(NodeLog.source).where(NodeLog.node_id == node.node_id).distinct()
    sources = sorted(store.run("execute", src_stmt, scalars=True, all=True))

    return ok(
        result={
            "entries": [{"source": r.source, "ts": r.ts, "line": r.line} for r in page],
            "sources": sources,
            "has_more": has_more,
        },
        msg=f"Retrieved {len(page)} log line(s).")


@router.get(
    "/pods/nodes/{node_id}/metrics",
    tags=["Nodes"],
    summary="get_node_metrics",
    operation_id="get_node_metrics",
    response_model=NodeMetricsResponse)
async def get_node_metrics(
    node_id: str,
    window_s: int = Query(3600, description="History window in seconds (600..2592000 — up to 30 days)."),
    step_s: int = Query(60, description="Bucket step in seconds (>=30; raised automatically to cap points at 500)."),
):
    """Metrics history series for a node (node READ permission) — chart food.

    Buckets with no samples are OMITTED (never interpolated), and start_ts/end_ts
    frame the requested window regardless of where samples exist — so charts can
    anchor a full-window x-axis and render off periods honestly. `caps` carries the
    latest-known cpu_count / mem_total_bytes for static y-axis scaling.
    """
    node = _get_node_or_404(node_id)
    window_s, step_s = clamp_window_step(window_s, step_s)
    end = _utcnow()
    start = end - datetime.timedelta(seconds=window_s)
    start_epoch = start.replace(tzinfo=datetime.timezone.utc).timestamp()

    store = _telemetry_store(NodeMetric)
    stmt = (
        sa_select(NodeMetric)
        .where(NodeMetric.node_id == node.node_id, NodeMetric.ts >= start, NodeMetric.ts <= end)
        .order_by(NodeMetric.ts.asc()))
    metric_rows = store.run("execute", stmt, scalars=True, all=True)

    sample_dicts = [
        {"ts": r.ts, "extras": r.extras, **{f: getattr(r, f) for f in METRIC_FIELDS}}
        for r in metric_rows]
    series = downsample_samples(sample_dicts, start_epoch, window_s, step_s)

    # Caps = static y-axis ceilings: fixed gauges + every extras ":total" key
    # (per-watched-path filesystem sizes), latest known value wins.
    caps: dict = latest_extras_caps(sample_dicts)
    for r in reversed(metric_rows):
        if "cpu_count" not in caps and r.cpu_count is not None:
            caps["cpu_count"] = r.cpu_count
        if "mem_total_bytes" not in caps and r.mem_total_bytes is not None:
            caps["mem_total_bytes"] = r.mem_total_bytes
        if "cpu_count" in caps and "mem_total_bytes" in caps:
            break

    return ok(
        result={
            "series": series,
            "window_s": window_s,
            "step_s": step_s,
            "start_ts": start_epoch,
            "end_ts": start_epoch + window_s,
            "caps": caps,
            "sample_count": len(metric_rows),
        },
        msg="Node metrics history retrieved.")


# Routes — publish v0 (Tapis JWT auth, node-permission gated) -----------------
# A route publishes one node port at https://<route_id>.pods.<tenant-domain> through
# central traefik. Rendering happens on the health-central pass (+ kubelet configmap
# sync), so create/delete take effect within ~2 minutes — same cadence as pods.

def _get_route_or_404(node_id: str, route_id: str) -> Route:
    route = Route.db_get_with_pk(route_id, tenant=g.request_tenant_id, site=getattr(g, 'site_id', None))
    if not route or route.node_id != node_id:
        raise ResourceError(f"Route '{route_id}' not found on node '{node_id}'.", 404)
    return route


def _check_route_hostname_free(route_id: str):
    """Routes share the <x>.pods.<domain> hostname namespace with pod networking urls —
    reject a route_id that would collide with an existing pod hostname. Best-effort at
    create time (pods have no reciprocal check yet); pod_ids cannot contain hyphens, so
    only two shapes can clash: bare pod_id, or pod_id-<networking_name>."""
    clash = Pod.db_get_with_pk(route_id, tenant=g.request_tenant_id, site=g.site_id)
    if clash:
        raise ResourceError(f"route_id '{route_id}' collides with an existing pod's hostname.", 400)
    if '-' in route_id:
        prefix, net_name = route_id.split('-', 1)
        pod = Pod.db_get_with_pk(prefix, tenant=g.request_tenant_id, site=g.site_id)
        if pod and net_name in (pod.networking or {}):
            raise ResourceError(f"route_id '{route_id}' collides with pod '{prefix}' networking '{net_name}' hostname.", 400)


@router.get(
    "/pods/nodes/{node_id}/routes",
    tags=["Nodes"],
    summary="list_node_routes",
    operation_id="list_node_routes",
    response_model=RoutesResponse)
async def list_node_routes(node_id: str):
    """List routes published from this node."""
    logger.info(f"GET /pods/nodes/{node_id}/routes - Top of list_node_routes.")
    _get_node_or_404(node_id)
    routes = Route.db_get_for_node(node_id, tenant=g.request_tenant_id, site=g.site_id)
    return ok(result=[route.display() for route in routes], msg="Routes retrieved successfully.")


# Hostname suffixes/names that resolve to cluster- or cloud-internal targets.
_INTERNAL_HOST_SUFFIXES = (".svc", ".cluster.local", ".internal", ".local")
_INTERNAL_HOST_EXACT = {"metadata", "metadata.google.internal", "localhost"}


def _ip_is_internal(ip_str: str) -> bool:
    """True for any address a public route/probe must never reach: private
    (RFC1918), loopback, link-local (incl. 169.254 cloud metadata), CGNAT
    (100.64/10), reserved, multicast, or unspecified — v4 and v6 alike."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        return True
    # 100.64.0.0/10 (CGNAT) is not flagged is_private on every Python version.
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return True
    return False


def _backend_host_is_internal(host: str) -> bool:
    """Shape + resolution check for a backend_host. An IP literal is judged
    directly; a hostname is judged by its suffix AND by resolving it (a public
    name pointed at an internal A record is the sharp case). Resolution failure
    is treated as internal=False here — the probe re-checks at dial time, and an
    unresolvable host simply won't route. Never raises."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    # Bare single-label names resolve to in-cluster k8s services.
    if "." not in host:
        return True
    if host in _INTERNAL_HOST_EXACT or host.endswith(_INTERNAL_HOST_SUFFIXES):
        return True
    if ".svc." in host:
        return True
    # IP literal → judge directly.
    try:
        ipaddress.ip_address(host)
        return _ip_is_internal(host)
    except ValueError:
        pass
    # Hostname → resolve and reject if ANY resolved address is internal.
    try:
        infos = socket.getaddrinfo(host, None)
        return any(_ip_is_internal(info[4][0]) for info in infos)
    except (socket.gaierror, socket.timeout, UnicodeError, ValueError):
        return False


def _reject_internal_backend(backend_host: str):
    """Publish hardening (SSRF containment): routes exist to expose EDGE services
    (tailnet/LAN addresses), never the cluster's own or a cloud metadata endpoint.
    Any authenticated user can create a node and publish a route on it, and a
    route with tapis_auth off is world-reachable — so a backend pointed at
    10.x/169.254.169.254/a k8s ClusterIP/*.svc would put an internal service on
    the public internet. Admins keep the escape hatch (central-side publishes are
    a legitimate admin move)."""
    if getattr(g, "admin", False):
        return
    if _backend_host_is_internal(backend_host):
        raise ResourceError(
            "backend_host may not point at a cluster-internal, loopback, "
            "link-local (incl. cloud metadata 169.254.169.254), or private "
            "address. Publish node ports on tailnet/LAN addresses reachable from "
            "central. (A platform admin can override for central-side backends.)",
            400)


@router.post(
    "/pods/nodes/{node_id}/routes",
    tags=["Nodes"],
    summary="create_node_route",
    operation_id="create_node_route",
    response_model=RouteResponse)
async def create_node_route(node_id: str, new_route: NewRoute):
    """Publish a node port: create a route at https://<route_id>.pods.<tenant-domain>.

    backend_host must be reachable FROM central (a tailnet IP/name, or a dev gateway
    like host.minikube.internal). With tapis_auth=true the route is gated by the same
    forwardAuth flow pods use, configured entirely per-route (allowed users/groups,
    response headers, excluded paths). Takes effect on the next proxy render (≤ ~2 min).
    """
    logger.info(f"POST /pods/nodes/{node_id}/routes - Top of create_node_route.")
    node = _get_node_or_404(node_id)
    _reject_internal_backend(new_route.backend_host)
    _check_route_hostname_free(new_route.route_id)
    if Route.db_get_with_pk(new_route.route_id, tenant=g.request_tenant_id, site=g.site_id):
        raise ResourceError(f"Route with route_id '{new_route.route_id}' already exists.", 400)

    route = Route(**new_route.dict(), node_id=node.node_id)
    route.db_create()
    node.log_action(f"route '{route.route_id}' published (port {route.port}, backend {route.backend_host or 'unset'}, tapis_auth={route.tapis_auth}) by '{g.username}'")
    node.db_update()
    return ok(result=route.display(), msg="Route created. It goes live on the next proxy render pass (up to ~2 minutes).")


@router.get(
    "/pods/nodes/{node_id}/routes/{route_id}",
    tags=["Nodes"],
    summary="get_node_route",
    operation_id="get_node_route",
    response_model=RouteResponse)
async def get_node_route(node_id: str, route_id: str):
    """Get one route's details."""
    logger.info(f"GET /pods/nodes/{node_id}/routes/{route_id} - Top of get_node_route.")
    route = _get_route_or_404(node_id, route_id)
    return ok(result=route.display(), msg="Route retrieved successfully.")


@router.delete(
    "/pods/nodes/{node_id}/routes/{route_id}",
    tags=["Nodes"],
    summary="delete_node_route",
    operation_id="delete_node_route",
    response_model=RouteDeleteResponse)
async def delete_node_route(node_id: str, route_id: str):
    """Delete a route. It disappears from the proxy on the next render pass (≤ ~2 min)."""
    logger.info(f"DELETE /pods/nodes/{node_id}/routes/{route_id} - Top of delete_node_route.")
    node = _get_node_or_404(node_id)
    route = _get_route_or_404(node_id, route_id)
    route.db_delete()
    node.log_action(f"route '{route_id}' deleted by '{g.username}'")
    node.db_update()
    return ok(result="", msg="Route deleted. It leaves the proxy on the next render pass (up to ~2 minutes).")


# In-cluster traefik service the probe dials to exercise the full public path without
# DNS/TLS (Host header carries the route's public hostname).
TRAEFIK_SERVICE = os.environ.get("PODS_TRAEFIK_SERVICE", "pods-traefik")
PROBE_TIMEOUT_SECONDS = float(os.environ.get("NODES_ROUTE_PROBE_TIMEOUT", "4"))


def _probe_http(url: str, host_header: str = None) -> dict:
    """One probe leg: GET url, no redirects followed (a 302 on an authed route is the
    auth gate working, and that's exactly what we want to report). Returns the
    RouteProbeCheck shape."""
    headers = {"Host": host_header} if host_header else {}
    start = time.monotonic()
    try:
        resp = requests.get(url, headers=headers, timeout=PROBE_TIMEOUT_SECONDS,
                            allow_redirects=False, stream=True)
    except requests.RequestException as e:
        return {"ok": False, "status_code": None, "latency_ms": None,
                "error": f"{type(e).__name__}: {e}"[:300], "snippet": None}
    latency_ms = int((time.monotonic() - start) * 1000)
    try:
        raw = next(resp.iter_content(chunk_size=240), b"") or b""
    except requests.RequestException:
        raw = b""
    finally:
        resp.close()
    snippet = "".join(c for c in raw.decode("utf-8", "replace") if c.isprintable() or c in "\n\t")[:240]
    return {"ok": True, "status_code": resp.status_code, "latency_ms": latency_ms,
            "error": None, "snippet": snippet}


@router.get(
    "/pods/nodes/{node_id}/routes/{route_id}/probe",
    tags=["Nodes"],
    summary="probe_node_route",
    operation_id="probe_node_route",
    response_model=RouteProbeResponse)
async def probe_node_route(node_id: str, route_id: str):
    """Central-side reachability test for a route — the UI can't test routes itself
    (TapisUI runs in the browser; route hostnames don't resolve in dev deployments).

    Two legs: `direct` dials backend_host:port from central (is the backend up?);
    `via_proxy` dials the in-cluster traefik with the route's public Host header (is
    the public path rendered and routing? — a 302 on an auth-gated route means the
    login redirect is working, a 404 usually means the render pass hasn't run yet).
    The probe can only reach what the rendered route itself exposes publicly.
    """
    logger.info(f"GET /pods/nodes/{node_id}/routes/{route_id}/probe - Top of probe_node_route.")
    _get_node_or_404(node_id)
    route = _get_route_or_404(node_id, route_id)

    # SSRF containment on the reflecting leg: the probe dials backend_host from
    # central and hands the caller (node USER) a body snippet. Re-check the
    # target at dial time — an admin-created internal backend, or a hostname
    # whose A record moved internal after create, must not be reflected to a
    # non-admin. Admins may probe internal backends (they can set them).
    if not getattr(g, "admin", False) and _backend_host_is_internal(route.backend_host):
        direct = {"ok": False, "status_code": None, "latency_ms": None,
                  "error": "backend resolves to an internal address; probe refused "
                           "(a platform admin can probe central-side backends).",
                  "snippet": None}
    else:
        direct = _probe_http(f"http://{route.backend_host}:{route.port}/")
    via_proxy = _probe_http(f"http://{TRAEFIK_SERVICE}/", host_header=route.url)

    if direct["ok"] and via_proxy["ok"]:
        msg = "Probe complete — backend reachable and the public path is responding."
    elif direct["ok"]:
        msg = "Backend is reachable, but the public path isn't routing yet — the proxy render pass runs every ~2 minutes."
    else:
        msg = "Backend unreachable from central — check backend_host/port and that the service is listening on a reachable interface."
    return ok(
        result={
            "route_id": route.route_id,
            "url": route.url,
            "tapis_auth": route.tapis_auth,
            "direct": direct,
            "via_proxy": via_proxy,
        },
        msg=msg)


# Route auth endpoints (no Tapis token — browser forwardAuth flow; see NO_TOKEN_ROUTES
# and NEED-BASEURL entries in auth.py) ----------------------------------------

def _route_auth_entity(route_id: str) -> TapisAuthEntity:
    route = Route.db_get_with_pk(route_id, tenant=g.request_tenant_id, site=getattr(g, 'site_id', None))
    if not route:
        raise ResourceError(f"Route '{route_id}' not found.", 404)
    return TapisAuthEntity(
        auth_cfg=route.dict(),
        permissions=route.get_permissions(),
        public_url=route.url,
        auth_path=f"pods/routes/{route.route_id}/auth",
        client_id=f"PODS-SERVICE-{route.traefik_service_name()}",
        label=f"route '{route.route_id}'",
    )


@router.get(
    "/pods/routes/{route_id}/auth",
    tags=["Nodes"],
    summary="route_auth",
    operation_id="route_auth",
    response_model=RouteResponse)
async def route_auth(route_id: str, request: Request):
    """forwardAuth endpoint for tapis_auth-gated routes — same flow as pod auth
    (validate an attached Tapis token, else bounce browsers through the tenant OAuth2
    login), driven by the route's own tapis_auth config and permissions."""
    logger.debug(f"GET /pods/routes/{route_id}/auth - Top of route_auth.")
    entity = _route_auth_entity(route_id)
    return run_tapis_auth_check(request, entity)


@router.get(
    "/pods/routes/{route_id}/auth/callback",
    tags=["Nodes"],
    summary="route_auth_callback",
    operation_id="route_auth_callback",
    response_model=RouteResponse)
async def route_auth_callback(route_id: str, request: Request):
    """OAuth2 callback for tapis_auth-gated routes — exchanges the code, validates the
    token + allowed users, sets cookies, and redirects to the route's return path."""
    logger.debug(f"GET /pods/routes/{route_id}/auth/callback - Top of route_auth_callback.")
    entity = _route_auth_entity(route_id)
    return run_tapis_auth_callback(request, entity)
