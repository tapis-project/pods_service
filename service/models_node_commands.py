"""
Node command dispatcher (v1) — one-shot commands queued by users, delivered to
the agent over its existing GET /commands poll, completed by an agent result
POST. First consumer: the benchmark suite (`type: bench`). Distinct from
desired state (continuous, checkin-carried) per the edge-autonomy model.

Lifecycle: queued -> delivered (handed to the agent exactly once) -> done|error.
A command that is delivered but never completed just sits there — visible, not
retried (v1 keeps redelivery semantics out; regenerate/rejoin restarts cleanly).
"""
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import DateTime, Index
from sqlmodel import Field, Column, String, JSON
from models_base import TapisModel, TapisApiModel

COMMAND_TYPES = ["bench"]
COMMAND_STATUSES = ["queued", "delivered", "done", "error"]

# Result JSON cap — bench reports are small aggregates; anything bigger is a bug
# (or an agent trying to use results as bulk storage).
COMMAND_RESULT_MAX_BYTES = 256 * 1024


class NodeCommand(TapisModel, table=True):
    __table_args__ = (Index("ix_nodecommand_node_status", "node_id", "status"),)

    command_id: str = Field(..., sa_column=Column(String, primary_key=True), description="Unique command id (nc_...).")
    node_id: str = Field(..., sa_column=Column(String, nullable=False), description="Node the command targets.")
    type: str = Field(..., sa_column=Column(String, nullable=False), description=f"Command type. One of: {COMMAND_TYPES}.")
    params: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON), description="Command parameters (sanitized server-side at create).")
    status: str = Field("queued", sa_column=Column(String, nullable=False), description=f"One of: {COMMAND_STATUSES}.")
    result: Optional[Dict[str, Any]] = Field(None, sa_column=Column(JSON, nullable=True), description="Agent-posted result payload (null until done/error).")
    requested_by: str = Field("", description="Username that queued the command.")
    created_ts: Optional[datetime] = Field(None, sa_column=Column(DateTime, nullable=True))
    delivered_ts: Optional[datetime] = Field(None, sa_column=Column(DateTime, nullable=True), description="When the agent picked the command up.")
    completed_ts: Optional[datetime] = Field(None, sa_column=Column(DateTime, nullable=True))
    tenant_id: str = Field("", description="Tapis tenant of the parent node.")
    site_id: str = Field("", description="Tapis site of the parent node.")

    def display(self):
        d = self.dict()
        d.pop("tenant_id", None)
        d.pop("site_id", None)
        return d


class NodeCommandResultIn(TapisApiModel):
    """Agent's completion report for a delivered command."""
    status: str = Field(..., description="done | error")
    result: Dict[str, Any] = Field(default_factory=dict, description="Result payload (bench report, error detail, ...).")


class NodeBenchRequest(TapisApiModel):
    """User-facing bench trigger. Every field optional — server sanitizes/clamps
    into the stored params so the agent only ever sees valid settings."""
    encodings: Optional[List[str]] = Field(None, description="Encodings to test: identity, gzip:1|6|9, zstd:3|9 (zstd skipped agent-side when unsupported).")
    corpora: Optional[List[str]] = Field(None, description="Corpus kinds: real (node's recent log lines), json, text, entropy.")
    line_bytes: Optional[List[int]] = Field(None, description="Synthetic line sizes to test (bytes/line).")
    line_counts: Optional[List[int]] = Field(None, description="Batch volumes to test (lines/batch).")
    probe_count: Optional[int] = Field(None, description="Latency probes per target (default 10, max 50).")
    dry_run: Optional[bool] = Field(None, description="Default TRUE: benchmark ingest POSTs are decoded+timed but NOT stored. False stores bench lines in your node's real logs (they count against retention and can push out real lines) — visualization-only data, only flip this deliberately.")


class NodeCommandResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: Dict[str, Any]
    status: str
    version: str


class NodeCommandsListResponse(TapisApiModel):
    message: str
    metadata: Dict
    result: List[Dict[str, Any]]
    status: str
    version: str
