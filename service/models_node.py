import os
import re
from datetime import datetime
from typing import List, Dict, Any, Optional
from pydantic import validator, model_validator, create_model
from codes import PermissionLevel

from tapisservice.tapisfastapi.utils import g
from tapisservice.logs import get_logger
logger = get_logger(__name__)

from sqlalchemy.dialects.postgresql import ARRAY
from sqlmodel import Field, select, JSON, Column, String
from models_base import TapisModel, TapisApiModel


NODE_TYPES = ['k8s', 'docker', 'host']

# The interval the server hands agents at join/checkin — also the yardstick for the
# server-derived liveness field, so UI staleness badges carry real authority.
CHECKIN_INTERVAL_SECONDS = int(os.environ.get("NODES_CHECKIN_INTERVAL", "60"))

# Capabilities are REPORTED by the agent at join/checkin (detected, not claimed) —
# e.g. runtime.k8s, runtime.docker, runtime.none, exec, tunnels, metrics.
# UI renders panels by detected capability; a mismatch vs the declared `type`
# surfaces as a warning rather than mystery breakage.


class NodeBase(TapisApiModel):
    # Required
    node_id: str = Field(..., description="Unique node identifier.", primary_key=True)
    name: str = Field("", description="Human-readable name for the node.")
    type: str = Field(..., description=f"Node runtime type. One of: {NODE_TYPES}.")
    description: str = Field("", description="Description of the node.")
    # Optional user-settable config
    login_server: Optional[str] = Field(None, description="Headscale/Tailscale login server this node joins. Defaults to the service-wide login server when unset.")
    namespace: Optional[str] = Field(None, description="Advisory Kubernetes namespace for k8s-type nodes; handed to the agent in its join config bundle.")


class NodeBaseRead(NodeBase):
    # Central-stored agent settings (sparse overlay — absent key = agent default),
    # carried to the agent in every checkin response; edited via PUT .../settings.
    # Env vars on the box always win over these (the operator pins locally).
    agent_settings: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON), description="Per-node agent settings (sharing profile, container filters, intervals, log encoding preference). Sparse: absent keys mean agent defaults; env vars on the node override these.")
    # Provided — agent-reported at join/checkin
    capabilities: List[str] = Field([], description="Capabilities last reported by the agent (e.g. runtime.k8s, runtime.docker, exec, tunnels, metrics).", sa_column=Column(ARRAY(String)))
    status: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON), description="Status snapshot last reported by the agent (os, arch, versions, health).")
    agent_version: Optional[str] = Field(None, description="Agent version last reported at checkin.")
    inventory_hash: Optional[str] = Field(None, description="Hash of the last stored workload inventory; agents compare against this to skip resending unchanged inventory.")
    last_checkin_ts: Optional[datetime] = Field(None, description="Time (UTC) of the last agent checkin.")
    claimed_at: Optional[datetime] = Field(None, description="Time (UTC) the node was claimed by an agent via join. Null = unclaimed.")
    decommission_ts: Optional[datetime] = Field(None, description="Time (UTC) a decommission was requested. Non-null = waiting for the agent to remove itself; the row hard-deletes on the agent's confirmation or after the timeout.")
    claim_token_expires: Optional[datetime] = Field(None, description="Expiry of the outstanding single-use claim token, if any.")
    # Provided — headscale provisioning metadata
    ts_preauthkey_id: Optional[str] = Field(None, description="Identifier of the issued headscale preauth key.")
    ts_preauthkey_expires: Optional[datetime] = Field(None, description="Expiration time of the headscale preauth key.")
    ts_routes: List[str] = Field([], description="Subnet routes granted (metadata).", sa_column=Column(ARRAY(String)))
    creation_ts: datetime | None = Field(None, description="Time (UTC) that this node was created.")
    update_ts: datetime | None = Field(None, description="Time (UTC) that this node was updated.")


class NodeBaseFull(NodeBaseRead):
    # Provided
    tenant_id: str = Field("", description="Tapis tenant used during creation of this node.")
    site_id: str = Field("", description="Tapis site used during creation of this node.")
    permissions: List[str] = Field([], description="Node permissions for each user.", sa_column=Column(ARRAY(String, dimensions=1)))
    action_logs: List[str] = Field([], description="Log of past actions taken on this node.", sa_column=Column(ARRAY(String, dimensions=1)))
    # Secrets — SHA-256 hashes only; raw tokens are returned exactly once at create/join.
    claim_token_hash: Optional[str] = Field(None, description="SHA-256 of the single-use claim token. Never returned.")
    agent_token_hash: Optional[str] = Field(None, description="SHA-256 of the node-scoped agent bearer token. Never returned.")
    # No-downtime rotation: during a rotate the NEW token's hash lives here and BOTH
    # tokens authenticate. The agent proves it persisted the new one by confirming
    # with it, which promotes pending -> active and revokes the old — so a rotate
    # that never lands leaves the running agent perfectly authed (no park, no re-join).
    pending_agent_token_hash: Optional[str] = Field(None, description="SHA-256 of a newly minted agent token awaiting the agent's confirmation. Never returned.")
    pending_token_ts: Optional[datetime] = Field(None, description="When the pending token was minted; unconfirmed pendings expire.")
    # Agent-reported workload inventory (hash-gated by the checkin protocol). Kept out of
    # display() so list/get responses stay light — a dedicated inventory endpoint can expose it.
    inventory: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON), description="Workload inventory last reported by the agent.")

    def checkin_age_seconds(self):
        if not self.last_checkin_ts:
            return None
        return max(0, int((datetime.utcnow() - self.last_checkin_ts).total_seconds()))

    def liveness(self):
        """Server-derived staleness the UI can state with authority — the server set the
        agent's checkin interval, so the server judges lateness.
        unclaimed | live | overdue (>2 intervals) | stale (>10 intervals or never)."""
        if not self.claimed_at:
            return "unclaimed"
        age = self.checkin_age_seconds()
        if age is None or age > 10 * CHECKIN_INTERVAL_SECONDS:
            return "stale"
        if age > 2 * CHECKIN_INTERVAL_SECONDS:
            return "overdue"
        return "live"

    def display(self):
        display = self.dict()
        # Rows created before the settings migration hold NULL — the API contract
        # (and every consumer) wants the sparse-overlay dict, so coerce here.
        display['agent_settings'] = display.get('agent_settings') or {}
        display.pop('action_logs', None)
        display.pop('tenant_id', None)
        display.pop('site_id', None)
        display.pop('permissions', None)
        display.pop('claim_token_hash', None)
        display.pop('agent_token_hash', None)
        display.pop('pending_agent_token_hash', None)
        # Rotation is a transient internal state — the ledger is its record.
        display.pop('pending_token_ts', None)
        display.pop('inventory', None)
        display['liveness'] = self.liveness()
        display['last_checkin_age_seconds'] = self.checkin_age_seconds()
        display['checkin_interval_seconds'] = CHECKIN_INTERVAL_SECONDS
        return display


TapisNodeBaseFull = create_model("TapisNodeBaseFull", __base__=type("_ComboModel", (NodeBaseFull, TapisModel), {}))


class Node(TapisNodeBaseFull, table=True, validate=True):
    @validator('node_id')
    def check_node_id(cls, v):
        reserved_node_ids = ["default", "admin", "system", "central"]
        if v in reserved_node_ids:
            raise ValueError(f"node_id overlaps with reserved node ids: {reserved_node_ids}")
        res = re.fullmatch(r'[a-z][a-z0-9\-]+', v)
        if not res:
            raise ValueError(f"node_id must be lowercase alphanumeric or hyphen, starting with alpha.")
        if len(v) > 64 or len(v) < 3:
            raise ValueError(f"node_id length must be between 3-64 characters. Inputted length: {len(v)}")
        return v

    @model_validator(mode="after")
    def check_name(cls, values):
        # set name to node_id if not user set
        final_name = getattr(values, 'name', '') or getattr(values, 'node_id', '')
        object.__setattr__(values, 'name', final_name)
        res = re.fullmatch(r'[a-z][a-z0-9\-]+', final_name)
        if not res:
            raise ValueError(f"name must be lowercase alphanumeric or hyphen, starting with alpha.")
        if len(final_name) > 64 or len(final_name) < 3:
            raise ValueError(f"name length must be between 3-64 characters. Inputted length: {len(final_name)}")
        return values

    @validator('type')
    def check_type(cls, v):
        if v not in NODE_TYPES:
            raise ValueError(f"type must be one of: {NODE_TYPES}. Inputted type: {v}")
        return v

    @validator('tenant_id')
    def check_tenant_id(cls, v):
        return g.request_tenant_id

    @validator('site_id')
    def check_site_id(cls, v):
        return g.site_id

    @validator('creation_ts')
    def check_creation_ts(cls, v):
        return datetime.utcnow()

    @validator('update_ts')
    def check_update_ts(cls, v):
        return datetime.utcnow()

    @validator('action_logs')
    def check_action_logs(cls, v):
        # validate_assignment=True re-runs this on EVERY assignment — including
        # log_action() appends — so only seed the creation entry when empty,
        # never clobber an existing ledger.
        return v or [f"{datetime.utcnow().strftime('%y/%m/%d %H:%M')}: Node object created by '{g.username}'"]

    @validator('permissions')
    def check_permissions(cls, v):
        if not v:
            v = [f"{g.username}:ADMIN"]
        return v

    @validator('description')
    def check_description(cls, v):
        if not v.isascii():
            raise ValueError(f"description field may only contain ASCII characters.")
        if len(v) > 255:
            raise ValueError(f"description field must be less than 255 characters. Inputted length: {len(v)}")
        return v

    def log_action(self, msg: str):
        """Append a timestamped entry to action_logs (db_update only auto-logs for pods)."""
        self.action_logs = (self.action_logs or []) + [f"{datetime.utcnow().strftime('%y/%m/%d %H:%M')}: {msg}"]

    @classmethod
    def db_get_all_with_permission(cls, user, level, tenant, site):
        """
        Get all and ensure permission exists.
        """
        site, tenant, store = cls.get_site_tenant_session(tenant=tenant, site=site)
        table_name = cls.table_name()
        logger.info(f'Top of {table_name}.db_get_all_with_permissions() for tenant.site: {tenant}.{site}')

        # Get list of level specified + levels above.
        authorized_levels = PermissionLevel(level).authorized_levels()

        # Create list of permissions user needs to access this resource
        # In the case of level=USER, USER and ADMIN work, so: ["cgarcia:ADMIN", "cgarcia:USER"]
        permission_list = []
        for authed_level in authorized_levels:
            permission_list.append(f"{user}:{authed_level}")
        # Create statement
        stmt = select(Node).where(Node.permissions.overlap(permission_list))
        results = store.run("execute", stmt, scalars=True, all=True)
        logger.info(f"Got rows from table {tenant}.{table_name}.")
        return results


class NewNode(NodeBase):
    """
    Object with fields that users are allowed to specify for the Node class.
    """
    pass


# ---------------------------------------------------------------------------
# Agent contract request/result models
# ---------------------------------------------------------------------------

class NodeJoinRequest(TapisApiModel):
    """Agent's one-time claim-token exchange. The claim token is consumed on success."""
    claim_token: str = Field(..., description="Single-use claim token from node creation (or regenerate).")
    agent_version: Optional[str] = Field(None, description="Agent version string.")
    capabilities: List[str] = Field([], description="Capabilities detected by the agent at startup.")


class NodeCheckinRequest(TapisApiModel):
    """Periodic agent heartbeat. Inventory is hash-gated: send inventory_hash every time,
    the full inventory only when it changed (or when the previous checkin returned resync=true)."""
    agent_version: Optional[str] = Field(None, description="Agent version string.")
    capabilities: List[str] = Field([], description="Currently detected capabilities.")
    status: Dict[str, Any] = Field(default_factory=dict, description="Status snapshot (os, arch, versions, health).")
    inventory_hash: Optional[str] = Field(None, description="Hash of the agent's current workload inventory.")
    inventory: Optional[Dict[str, Any]] = Field(None, description="Full workload inventory; include only when the hash changed.")
    metrics_samples: Optional[List[Dict[str, Any]]] = Field(None, description="Batched metrics samples buffered since the last successful checkin — each {ts, load1, cpu_count, mem_used_bytes, mem_total_bytes, root_disk_pct, docker_running, docker_total, k8s_running, k8s_total}. Stored into per-node metrics history, deduped on (node_id, ts); a 200 response means the agent can clear its buffer.")


class NodeCreateResult(TapisApiModel):
    node: Dict[str, Any] = Field(..., description="The created node.")
    claim_token: str = Field(..., description="Single-use claim token — shown only once.")
    claim_token_expires: datetime = Field(..., description="Claim token expiry (UTC).")
    join_command: str = Field(..., description="One-liner to run on the node to join it.")


class NodeJoinResult(TapisApiModel):
    node_id: str
    node_token: str = Field(..., description="Node-scoped agent bearer token — shown only once; sent as the X-Pods-Node-Token header on checkin/commands.")
    login_server: str = Field(..., description="Headscale/Tailscale login server to join.")
    ts_preauthkey: Optional[str] = Field(None, description="Headscale preauth key for joining the tailnet (null when central has no headscale API key configured).")
    central_base_url: str = Field(..., description="Central pods API base URL for subsequent agent calls.")
    checkin_interval_seconds: int = Field(..., description="How often the agent should check in.")
    namespace: Optional[str] = Field(None, description="Advisory Kubernetes namespace for k8s-type nodes.")


class NodeCheckinResult(TapisApiModel):
    # Config-as-data: central publishes its current endpoints on every checkin so edges
    # never cache stale values (bootstrap values are first-contact only).
    endpoints: Dict[str, Any] = Field(..., description="Current central endpoints + capabilities (api_base, login_server, log_ingest, agent_source*, commands_wait, ...). Values are strings except numeric capabilities like commands_wait.")
    resync: bool = Field(False, description="True when central wants the full inventory on the next checkin.")
    poll_after_seconds: int = Field(..., description="Seconds until the agent should check in again.")
    desired: Dict[str, Any] = Field(default_factory=dict, description="Desired state for this node (reserved; command dispatch lands with the agent).")
    settings: Dict[str, Any] = Field(default_factory=dict, description="Central-stored agent settings (sparse overlay) — the agent adopts these each heartbeat; env vars on the box win over them.")


class NodeCommandsResult(TapisApiModel):
    commands: List[Dict[str, Any]] = Field([], description="Pending commands for this node.")
    poll_after_seconds: int = Field(..., description="Seconds until the agent should poll again.")
    settings: Dict[str, Any] = Field(default_factory=dict, description="Current central agent-settings overlay — piggybacked on every commands response so a long-poll wake delivers settings changes in the same round-trip (adoption drops from one-heartbeat to sub-second).")


# ---------------------------------------------------------------------------
# Response envelopes
# ---------------------------------------------------------------------------

class NodeResponseModel(NodeBaseRead):
    # Server-derived staleness — added by display(); authoritative for UI badges
    # ("node hasn't checked in for X") since the server set the expected interval.
    liveness: Optional[str] = Field(None, description="unclaimed | live | overdue | stale — derived from last_checkin_ts vs the checkin interval the server handed the agent.")
    last_checkin_age_seconds: Optional[int] = Field(None, description="Seconds since the last agent checkin; null when the node has never checked in.")
    checkin_interval_seconds: Optional[int] = Field(None, description="Checkin interval (seconds) the server expects agents to honor.")


class NodeResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: NodeResponseModel
    status: str
    version: str


class NodesResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[NodeResponseModel]
    status: str
    version: str


class NodeCreateResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: NodeCreateResult
    status: str
    version: str


class NodeJoinResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: NodeJoinResult
    status: str
    version: str


class NodeCheckinResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: NodeCheckinResult
    status: str
    version: str


class NodeCommandsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: NodeCommandsResult
    status: str
    version: str


class NodeDeleteResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: str
    status: str
    version: str


class NodeLedgerResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[str]
    status: str
    version: str
