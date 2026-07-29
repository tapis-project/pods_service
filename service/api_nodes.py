import datetime
import hashlib
import hmac
import os
import secrets

import requests
from fastapi import APIRouter, Request

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
)
from tapisservice.tapisfastapi.utils import g, ok
from tapisservice.logs import get_logger

logger = get_logger(__name__)

router = APIRouter()

# Agent contract tunables — env-overridable, defaults are sane for develop.
# CHECKIN_INTERVAL_SECONDS lives in models_node (shared with liveness derivation).
CLAIM_TTL_HOURS = int(os.environ.get("NODES_CLAIM_TTL_HOURS", "4"))
COMMANDS_POLL_AFTER_SECONDS = int(os.environ.get("NODES_COMMANDS_POLL_AFTER", "25"))
DEFAULT_LOGIN_SERVER = os.environ.get("TS_LOGIN_SERVER", "https://headscale.pods.tacc.develop.tapis.io")

# Agents authenticate with this header on checkin/commands — NOT Authorization, so the
# Tapis token middleware never tries to parse it as a JWT.
AGENT_TOKEN_HEADER = "X-Pods-Node-Token"


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
    """Authenticate an agent request via the node-scoped bearer token minted at join."""
    token = request.headers.get(AGENT_TOKEN_HEADER)
    if not _token_matches(token, node.agent_token_hash):
        raise ResourceError(f"Invalid or missing {AGENT_TOKEN_HEADER} for node '{node.node_id}'.", 403)
    # Agent requests carry no Tapis token, so g.username is unset — give db writes an actor.
    if not getattr(g, 'username', None):
        g.username = f"_node_agent_{node.node_id}"


def _central_base_url(request: Request) -> str:
    # Host header is the tenant base URL when fronted by Traefik/nginx; good enough for
    # the join one-liner and the checkin endpoints payload (refreshed every heartbeat).
    # X-Forwarded-Proto keeps local/dev deployments on http instead of a forced https.
    scheme = request.headers.get('x-forwarded-proto') or getattr(request.url, 'scheme', None) or 'https'
    host = request.headers.get('host') or str(request.url.hostname or '')
    return f"{scheme}://{host}/v3"


def _login_server_for(node: Node) -> str:
    return node.login_server or DEFAULT_LOGIN_SERVER


def _agent_endpoints(request: Request, node: Node) -> dict:
    """Config-as-data: current central endpoints, republished on every checkin so agents
    never rely on stale cached values (decision 5 in ROADMAP_EDGE_REMOTE)."""
    base = _central_base_url(request)
    return {
        "api_base": f"{base}/pods",
        "login_server": _login_server_for(node),
        # log_ingest lands with Phase 3 (Vector HTTP sink target).
    }


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
    #   2. NewNode carries login_server, and join sends the headscale ADMIN api key
    #      (TS_API_KEY) as a bearer to whatever control plane that field names. Until
    #      login_server is validated against an allowlist, an open create would let any
    #      authenticated user point central at a server they control and collect that key.
    #      Inert while TS_API_KEY is unset, live the moment the headnet cutover sets one.
    # The route stays codes.NONE because no object exists yet for an object-level check.
    # Roadmap: admin mints a node ON BEHALF OF a user (owner field + claim token handed
    # over), which is what should replace this gate — not handing out admin.
    if not getattr(g, 'admin', False):
        raise ResourceError(
            "Creating nodes currently requires admin. Ask an admin to create the node "
            "and hand you its claim token — the join command is all an operator needs.",
            403)

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


@router.delete(
    "/pods/nodes/{node_id}",
    tags=["Nodes"],
    summary="delete_node",
    operation_id="delete_node",
    response_model=NodeDeleteResponse)
async def delete_node(node_id: str):
    """Delete a node. The agent's token stops working immediately."""
    logger.info(f"DELETE /pods/nodes/{node_id} - Top of delete_node.")
    node = _get_node_or_404(node_id)
    # TODO Phase 2: expire the headscale node/preauth key so the machine leaves the tailnet.
    node.db_delete()
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

    node.last_checkin_ts = _utcnow()
    if checkin.agent_version:
        node.agent_version = checkin.agent_version
    if checkin.capabilities:
        node.capabilities = checkin.capabilities
    if checkin.status:
        node.status = checkin.status

    resync = False
    if checkin.inventory is not None:
        node.inventory = checkin.inventory
        node.inventory_hash = checkin.inventory_hash
    elif checkin.inventory_hash and checkin.inventory_hash != node.inventory_hash:
        resync = True

    node.db_update(user_update=False)

    return ok(
        result={
            "endpoints": _agent_endpoints(request, node),
            "resync": resync,
            "poll_after_seconds": CHECKIN_INTERVAL_SECONDS,
            "desired": {},
        },
        msg="Checkin recorded.")


@router.get(
    "/pods/nodes/{node_id}/commands",
    tags=["Nodes"],
    summary="get_node_commands",
    operation_id="get_node_commands",
    response_model=NodeCommandsResponse)
async def get_node_commands(node_id: str, request: Request):
    """Pending commands for this node.

    Phase 1 stub: the response shape is the contract; there is no command dispatcher yet,
    so this always returns an empty list. The dispatcher (and true long-poll hold) lands
    with the Phase 2 agent work.
    """
    node = _get_node_or_404(node_id)
    _require_agent(request, node)
    return ok(
        result={"commands": [], "poll_after_seconds": COMMANDS_POLL_AFTER_SECONDS},
        msg="No pending commands.")
