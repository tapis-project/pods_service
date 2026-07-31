"""
Node telemetry tables (Phase 3): agent-shipped logs + metrics history.

Two append-mostly tables, both hard-capped per node (rows + age) at ingest time
so one hot edge can never flood central storage — transport discipline #4 in
ROADMAP_EDGE_REMOTE. Rows are plain data (no permissions/action_logs columns);
access control rides the parent Node's permissions, enforced by the route
allowlist (nodes/{node_id} resolution in auth.py).

Write paths (api_nodes.py):
  * POST /pods/nodes/{node_id}/logs  — agent, X-Pods-Node-Token, batched, gzip/zstd
  * checkin `metrics_samples`        — agent samples batched into the heartbeat
Read paths: GET .../logs and GET .../metrics, node-READ gated.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import BigInteger, DateTime, Float, Index, Integer, UniqueConstraint
from sqlmodel import Field, Column, String
from models_base import TapisModel, TapisApiModel


class NodeLog(TapisModel, table=True):
    """One shipped log line. source = container name (docker/k8s) or 'agent'."""
    __table_args__ = (
        Index("ix_nodelog_node_ts", "node_id", "ts"),
        Index("ix_nodelog_node_source", "node_id", "source"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True))
    node_id: str = Field(..., sa_column=Column(String, nullable=False), description="Node this line came from.")
    source: str = Field(..., sa_column=Column(String, nullable=False), description="Log source: container name or 'agent'.")
    ts: datetime = Field(..., sa_column=Column(DateTime, nullable=False), description="Event time (UTC) as reported by the agent; server receipt time when absent.")
    line: str = Field(..., sa_column=Column(String, nullable=False), description="The log line (truncated server-side past the per-line cap).")
    ingest_ts: datetime = Field(..., sa_column=Column(DateTime, nullable=False), description="Server receipt time (UTC).")
    tenant_id: str = Field("", description="Tapis tenant of the parent node.")
    site_id: str = Field("", description="Tapis site of the parent node.")


class NodeMetric(TapisModel, table=True):
    """One agent metrics sample (~30-60 s cadence). All gauges nullable — an
    agent reports what its runtime detection grants (docker-only edges have no
    k8s counts, etc.). (node_id, ts) unique so offline-buffer resends after a
    lost checkin ack dedupe server-side (ON CONFLICT DO NOTHING)."""
    __table_args__ = (
        UniqueConstraint("node_id", "ts", name="uq_nodemetric_node_ts"),
        Index("ix_nodemetric_node_ts", "node_id", "ts"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column(BigInteger, primary_key=True, autoincrement=True))
    node_id: str = Field(..., sa_column=Column(String, nullable=False), description="Node this sample came from.")
    ts: datetime = Field(..., sa_column=Column(DateTime, nullable=False), description="Sample time (UTC), agent-reported.")
    load1: Optional[float] = Field(None, sa_column=Column(Float, nullable=True), description="1-minute load average.")
    cpu_count: Optional[int] = Field(None, sa_column=Column(Integer, nullable=True), description="Logical CPU count — the static y-axis cap for load graphs.")
    mem_used_bytes: Optional[int] = Field(None, sa_column=Column(BigInteger, nullable=True), description="Memory in use (total - available).")
    mem_total_bytes: Optional[int] = Field(None, sa_column=Column(BigInteger, nullable=True), description="Total memory — the static y-axis cap for memory graphs.")
    root_disk_pct: Optional[float] = Field(None, sa_column=Column(Float, nullable=True), description="Root filesystem used percent (0-100).")
    docker_running: Optional[int] = Field(None, sa_column=Column(Integer, nullable=True), description="Running docker containers.")
    docker_total: Optional[int] = Field(None, sa_column=Column(Integer, nullable=True), description="Total docker containers.")
    k8s_running: Optional[int] = Field(None, sa_column=Column(Integer, nullable=True), description="Running k8s pods (agent namespace scope).")
    k8s_total: Optional[int] = Field(None, sa_column=Column(Integer, nullable=True), description="Total k8s pods (agent namespace scope).")
    tenant_id: str = Field("", description="Tapis tenant of the parent node.")
    site_id: str = Field("", description="Tapis site of the parent node.")


# ---------------------------------------------------------------------------
# API request/result models
# ---------------------------------------------------------------------------

class NodeLogIngestResult(TapisApiModel):
    accepted: int = Field(..., description="Log lines stored.")
    dropped: int = Field(0, description="Entries discarded (malformed, empty, or over the batch cap).")
    truncated: int = Field(0, description="Lines cut to the per-line character cap.")
    retention: Dict[str, Any] = Field(default_factory=dict, description="Server retention caps (rows/age per node) so agents can size their buffers.")


class NodeLogEntry(TapisApiModel):
    source: str
    ts: datetime
    line: str


class NodeLogsResult(TapisApiModel):
    entries: List[NodeLogEntry] = Field([], description="Log lines, oldest first within the returned page.")
    sources: List[str] = Field([], description="Distinct sources stored for this node (viewer filter chips).")
    has_more: bool = Field(False, description="True when older lines exist before the returned page (page back with before_ts).")


class NodeMetricsResult(TapisApiModel):
    series: Dict[str, List[List[float]]] = Field(default_factory=dict, description="Per-field [[epoch_ts, mean], ...]; empty buckets are omitted (off periods stay visible as gaps).")
    window_s: int = Field(..., description="Actual window used (after clamping).")
    step_s: int = Field(..., description="Actual bucket step used (after clamping).")
    start_ts: float = Field(..., description="Epoch of the window start — the UI anchors the x-axis here regardless of where samples exist.")
    end_ts: float = Field(..., description="Epoch of the window end.")
    caps: Dict[str, Any] = Field(default_factory=dict, description="Latest-known static y-axis caps: cpu_count, mem_total_bytes.")
    sample_count: int = Field(0, description="Raw samples inside the window before downsampling.")


class NodeLogIngestResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: NodeLogIngestResult
    status: str
    version: str


class NodeLogsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: NodeLogsResult
    status: str
    version: str


class NodeMetricsResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: NodeMetricsResult
    status: str
    version: str
