"""
Local audit bridge for the Pods MCP — the choke point that makes every MCP tool
call visible to the UI.

Two ways in, ONE audit stream out:
  * Claude Code / any MCP client -> connects to the MCP protocol at  /mcp
      (`claude mcp add pods-audited --transport http http://localhost:8100/mcp`)
    A FastMCP middleware audits every call it makes.
  * The tapis-ui tab's own actions -> /audit/tool (one tool) and /chat (LLM loop).

Both feed a global broadcast, streamed to the tab at /audit/stream. So you can
DRIVE from Claude Code and just WATCH in the tab (read-only) — same audit bits,
no writing through the UI.

Endpoints:
    GET  /health
    GET  /catalog          -> catalog() TOC (overview tiles)
    POST /audit/tool        -> {tool,args} : SSE, invoke ONE tool (no LLM)
    POST /chat              -> {question}  : SSE, LLM loop (pluggable)
    GET  /audit/stream      -> SSE, the GLOBAL audit trail (replay + live)
    ANY  /mcp               -> the MCP server itself (for external clients)

Run:
    TAPIS_BASE_URL=... TAPIS_TOKEN=... uvicorn bridge:app --port 8100
"""
import asyncio
import contextvars
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastmcp.server.middleware import Middleware
from pydantic import BaseModel

import auth  # Tapis token auto-refresh (password grant) — falls back to TAPIS_TOKEN
import pods_mcp  # reuse the curated tools + shared client
from pods_mcp import _is_write

# ---------------------------------------------------------------------------
# HTTP-call capture: record requests the MCP makes during one bridge invocation
# ---------------------------------------------------------------------------
_http_calls: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "http_calls", default=None
)


async def _record_response(resp):
    calls = _http_calls.get()
    if calls is not None:
        calls.append({"method": resp.request.method,
                      "path": resp.request.url.path,
                      "status": resp.status_code})


pods_mcp.client.event_hooks.setdefault("response", [])
pods_mcp.client.event_hooks["response"].append(_record_response)


# ---------------------------------------------------------------------------
# Global audit broadcast — one stream every viewer subscribes to
# ---------------------------------------------------------------------------
_subs: set[asyncio.Queue] = set()
_recent: list[dict] = []
_seq = 0

# Durable audit log: when BRIDGE_STATE_DIR is set (mount a volume there), every
# published event is appended as one JSON line to audit.jsonl — so the full MCP
# history survives restarts and powers /audit/history. Unset -> in-memory only.
STATE_DIR = os.environ.get("BRIDGE_STATE_DIR") or ""
_AUDIT_PATH = os.path.join(STATE_DIR, "audit.jsonl") if STATE_DIR else ""
_audit_fh = None
# set while the bridge itself is calling a tool, so the MCP middleware doesn't
# double-publish (invoke() publishes those directly, tagged source=bridge).
_via_invoke: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "via_invoke", default=False
)


def publish(event: dict) -> dict:
    """Stamp a seq, persist, fan out. Returns the seq-stamped copy so callers
    (invoke, /chat) can hand the SAME event — seq included — to SSE viewers;
    the experiment runner uses that seq to slice audit.jsonl per task run."""
    global _seq
    _seq += 1
    event = {**event, "seq": _seq}
    _recent.append(event)
    if len(_recent) > 500:
        del _recent[: len(_recent) - 500]
    if _audit_fh is not None:
        try:
            _audit_fh.write(json.dumps(event) + "\n")
        except Exception:  # noqa: BLE001 — never let disk issues break the choke point
            pass
    for q in list(_subs):
        try:
            q.put_nowait(event)
        except Exception:  # noqa: BLE001 — a slow/closed subscriber never blocks publish
            pass
    return event


def _open_audit() -> None:
    """Open the durable log (append) and preload its tail into _recent, so a restart
    keeps the live view populated and the seq counter keeps climbing. No-op when
    BRIDGE_STATE_DIR is unset or the folder can't be written."""
    global _audit_fh, _seq
    if not _AUDIT_PATH:
        return
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.exists(_AUDIT_PATH):
            with open(_AUDIT_PATH) as f:
                tail = f.readlines()[-500:]
            for line in tail:
                try:
                    ev = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                _recent.append(ev)
                if isinstance(ev.get("seq"), int):
                    _seq = max(_seq, ev["seq"])
        _audit_fh = open(_AUDIT_PATH, "a", buffering=1)  # line-buffered
        print(f"[audit] persisting to {_AUDIT_PATH} "
              f"({len(_recent)} recent events preloaded)", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[audit] persistence unavailable ({e}); staying in-memory", flush=True)
        _audit_fh = None


_open_audit()


# ---------------------------------------------------------------------------
# History aggregation — daily heatmap + batches over the durable log
# ---------------------------------------------------------------------------
def _read_events() -> list[dict]:
    """Every persisted event (or the in-memory buffer when not persisting)."""
    if not _AUDIT_PATH or not os.path.exists(_AUDIT_PATH):
        return list(_recent)
    out: list[dict] = []
    try:
        with open(_AUDIT_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        return list(_recent)
    return out


def _cap_args(args, per_value=2_000):
    """Request args for the History inspector: keep structure, cap each top-level
    value's serialized size so one giant template body can't bloat the payload."""
    if not isinstance(args, dict):
        return args
    out = {}
    for k, v in args.items():
        try:
            s = v if isinstance(v, str) else json.dumps(v)
        except Exception:  # noqa: BLE001
            s = str(v)
        out[k] = v if len(s) <= per_value else s[:per_value] + f"…(+{len(s) - per_value} chars)"
    return out


def _pair_calls(events: list[dict]) -> list[dict]:
    """Fold tool_start/tool_end pairs into ordered call records (start ts order)."""
    pending: dict[str, dict] = {}
    calls: list[dict] = []
    for ev in events:
        t = ev.get("type")
        if t == "tool_start" and ev.get("id"):
            c = {"tool": ev.get("tool"), "kind": ev.get("kind", "read"),
                 "source": ev.get("source") or "bridge", "intent": ev.get("intent", ""),
                 "agent": ev.get("agent", ""), "args": _cap_args(ev.get("args") or {}),
                 "ts": ev.get("ts"), "ok": None, "elapsed_ms": None,
                 "summary": None, "error": None, "http": None,
                 "result_count": None, "evidence": None}
            pending[ev["id"]] = c
            calls.append(c)
        elif t == "tool_end" and ev.get("id") in pending:
            c = pending.pop(ev["id"])
            c["ok"] = ev.get("ok")
            c["elapsed_ms"] = ev.get("elapsed_ms")
            c["summary"] = ev.get("summary")
            c["error"] = ev.get("error")
            c["http"] = ev.get("http")
            c["result_count"] = ev.get("result_count")
            c["evidence"] = ev.get("evidence")
    calls = [c for c in calls if c.get("ts") is not None]
    calls.sort(key=lambda c: c["ts"])
    return calls


_BATCH_GAP_S = 90  # a >90s pause OR a source switch starts a new batch


def _build_batches(calls: list[dict], limit: int) -> list[dict]:
    """Group calls into time-clustered bursts (newest first), each carrying its calls
    with their per-call 'why' (intent)."""
    batches: list[dict] = []
    cur = None
    for c in calls:
        if (cur is None or c["source"] != cur["source"]
                or c["ts"] - cur["end_ts"] > _BATCH_GAP_S):
            cur = {"start_ts": c["ts"], "end_ts": c["ts"],
                   "source": c["source"], "calls": [], "_tools": set()}
            batches.append(cur)
        cur["end_ts"] = c["ts"]
        cur["_tools"].add(c["tool"])
        cur["calls"].append({k: c[k] for k in
                             ("tool", "kind", "intent", "agent", "ts", "ok",
                              "elapsed_ms", "summary", "error", "args", "http",
                              "result_count", "evidence")})
    for b in batches:
        b["tools"] = sorted(t for t in b["_tools"] if t)
        b["count"] = len(b["calls"])
        del b["_tools"]
    batches.reverse()
    return batches[:limit]


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------
def _unwrap(call_result) -> Any:
    d = getattr(call_result, "structured_content", None)
    if isinstance(d, dict) and set(d.keys()) == {"result"}:
        return d["result"]
    if d is not None:
        return d
    return getattr(call_result, "data", None)


def _trim(value, n=3):
    if isinstance(value, list):
        return [_trim(v, n) for v in value[:n]]
    if isinstance(value, dict):
        keep = ("pod_id", "stack_id", "template", "template_id", "tag", "status",
                "image", "kind", "volume_id", "snapshot_id", "description",
                "required", "error", "missing")
        out = {k: value[k] for k in keep if k in value}
        return out or {k: value[k] for k in list(value)[:6]}
    return value


def _summarize(tool: str, data: Any) -> dict:
    res = data.get("result") if isinstance(data, dict) and "result" in data else data
    count = len(res) if isinstance(res, list) else None
    if isinstance(res, list):
        summary = f"{count} item(s)"
    elif isinstance(res, dict):
        summary = res.get("status") or res.get("message") or "ok"
    else:
        summary = str(res)[:80] if res is not None else "ok"
    return {"result_count": count, "summary": summary, "evidence": _trim(res)}


# ---------------------------------------------------------------------------
# MCP middleware: audit calls made by external clients (Claude Code, IDEs, ...)
# ---------------------------------------------------------------------------
class AuditMiddleware(Middleware):
    async def on_call_tool(self, context, call_next):
        if _via_invoke.get():
            # bridge's own call — invoke() already publishes it.
            return await call_next(context)
        params = getattr(context, "message", None)
        tool = getattr(params, "name", "?")
        args = getattr(params, "arguments", None) or {}
        # Reserved audit args — popped BEFORE the tool sees or validates the
        # arguments, so any client may attach them to any call:
        #   intent: one short sentence, why this call is being made ("why:" in the UI)
        #   agent:  who is calling — model/agent self-identification for multi-agent
        #           runs (e.g. "haiku subagent", "fable-5 orchestrator"). MCP itself
        #           does not transmit model identity, so this is self-reported.
        intent, agent = "", ""
        if isinstance(args, dict):
            intent = str(args.pop("intent", "") or "")
            agent = str(args.pop("agent", "") or "")
        if not agent:
            # Fall back to the client app's self-declared identity from the MCP
            # initialize handshake (clientInfo) — distinguishes client apps, not models.
            try:
                ci = context.fastmcp_context.session.client_params.clientInfo
                agent = f"{ci.name} {getattr(ci, 'version', '') or ''}".strip()
            except Exception:  # noqa: BLE001
                agent = ""
        # Session hash separates concurrent connections of the same client app.
        try:
            sid = str(getattr(context.fastmcp_context, "session_id", "") or "")
            if sid:
                agent = f"{agent} #{sid[:6]}".strip()
        except Exception:  # noqa: BLE001
            pass
        kind = "write" if _is_write(tool) else "read"
        ev_id = f"m{int(time.time() * 1000) % 1000000}"
        publish({"type": "tool_start", "id": ev_id, "tool": tool, "args": args,
                 "kind": kind, "intent": intent, "agent": agent,
                 "source": "claude-code", "ts": time.time()})
        t0 = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as e:  # noqa: BLE001
            publish({"type": "tool_end", "id": ev_id, "tool": tool, "ok": False,
                     "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                     "kind": kind, "agent": agent, "error": str(e),
                     "source": "claude-code", "ts": time.time()})
            raise
        data = _unwrap(result)
        publish({"type": "tool_end", "id": ev_id, "tool": tool,
                 "ok": not getattr(result, "is_error", False),
                 "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                 "kind": kind, "agent": agent, "source": "claude-code",
                 "ts": time.time(), **_summarize(tool, data)})
        return result


pods_mcp.mcp.add_middleware(AuditMiddleware())


# ---------------------------------------------------------------------------
# Bridge-side tool invocation (interactive tab actions + /chat)
# ---------------------------------------------------------------------------
async def invoke(tool: str, args: dict, intent: str = "", agent: str = "") -> tuple[dict, dict]:
    ev_id = f"t{int(time.time() * 1000) % 100000}"
    kind = "write" if _is_write(tool) else "read"
    start = {"type": "tool_start", "id": ev_id, "tool": tool, "args": args,
             "kind": kind, "intent": intent, "agent": agent, "source": "bridge",
             "ts": time.time()}
    http_token = _http_calls.set([])
    invoke_token = _via_invoke.set(True)
    t0 = time.perf_counter()
    try:
        result = await pods_mcp.mcp.call_tool(tool, args or {})
        data = _unwrap(result)
        ok, err = True, None
    except Exception as e:  # noqa: BLE001
        data, ok, err = None, False, str(e)
    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    http = _http_calls.get() or []
    _http_calls.reset(http_token)
    _via_invoke.reset(invoke_token)
    end = {"type": "tool_end", "id": ev_id, "tool": tool, "ok": ok,
           "elapsed_ms": elapsed, "kind": kind, "agent": agent, "source": "bridge",
           "ts": time.time(),
           "http": http[-1] if http else None, "http_calls": len(http),
           "error": err, **({} if not ok else _summarize(tool, data))}
    start = publish(start)
    end = publish(end)
    return start, end


# ---------------------------------------------------------------------------
# App — MCP protocol mounted at /mcp, bridge routes alongside
# ---------------------------------------------------------------------------
def _apply_token(token: str) -> None:
    """Install a freshly-minted token everywhere the bridge reads it."""
    pods_mcp.TOKEN = token
    pods_mcp.client.headers["X-Tapis-Token"] = token


_mcp_app = pods_mcp.mcp.http_app(path="/")


@asynccontextmanager
async def _lifespan(app):
    # If a client + creds are configured, mint our own token and keep it fresh —
    # no manual TAPIS_TOKEN needed. Otherwise fall back to the static one.
    task = None
    if auth.password_grant_configured():
        try:
            token = await auth.mint_password_token(pods_mcp.BASE)
            _apply_token(token)
            print("[auth] password-grant token minted; auto-refresh enabled",
                  flush=True)
            task = asyncio.create_task(
                auth.refresh_loop(pods_mcp.BASE, _apply_token, token)
            )
        except Exception as e:  # noqa: BLE001
            print(f"[auth] initial mint failed ({e}); using TAPIS_TOKEN fallback",
                  flush=True)
    async with _mcp_app.lifespan(app):
        yield
    if task:
        task.cancel()


app = FastAPI(title="Pods MCP Audit Bridge", lifespan=_lifespan)
app.add_middleware(CORSMiddleware,
                   # local UI origins only — the bridge holds a privileged Tapis
                   # token and has no auth of its own; wildcard CORS would let any
                   # webpage in the developer's browser drive tool calls
                   allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
                   allow_methods=["*"],
                   allow_headers=["*"])
app.mount("/mcp", _mcp_app)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


class ToolCall(BaseModel):
    tool: str
    args: dict = {}
    intent: str = ""
    agent: str = ""  # who is calling (self-ID); falls back to X-Bridge-Caller / User-Agent


class ChatReq(BaseModel):
    # Optional per-request LLM override (experiment harness): {"kind":
    # "anthropic"|"openai", "base_url", "model", "api_key"}. Localhost-only
    # bridge, so an in-body key is acceptable — it is used for the upstream
    # call and NEVER echoed into audit events or responses.
    question: str
    provider: dict | None = None


def _rest_caller(request: Request) -> str:
    """Attribution for REST callers without auth: an explicit X-Bridge-Caller header
    wins; otherwise the first User-Agent token (e.g. 'Mozilla/5.0', 'curl/8.5')."""
    hdr = request.headers.get("x-bridge-caller", "").strip()
    if hdr:
        return hdr[:80]
    ua = request.headers.get("user-agent", "").strip()
    return ua.split(" ")[0][:40] if ua else ""


@app.get("/health")
async def health():
    tools = await pods_mcp.mcp.list_tools()
    return {"ok": True, "base_url": pods_mcp.API_BASE, "tools": len(tools),
            "token": bool(pods_mcp.TOKEN), "mcp_endpoint": "/mcp",
            "auth": "password-grant" if auth.password_grant_configured() else "manual",
            "verbose": pods_mcp.VERBOSE}


@app.get("/catalog")
async def catalog(request: Request):
    _, end = await invoke("catalog", {}, intent="catalog load (quick-action tiles)",
                          agent=_rest_caller(request))
    return end.get("evidence") or {}


@app.post("/audit/replay")
async def audit_replay(call: ToolCall):
    """Re-run a READ-ONLY tool with the given args and return the FULL result.

    History evidence is trimmed at publish time (lists capped, dicts filtered), so this
    is the "dig deeper" path for the History inspector: same tool, same args, live
    re-execution, untrimmed payload. Write tools are refused. The replay itself is
    audited like any bridge call, tagged as a replay in its intent.
    """
    if _is_write(call.tool):
        return JSONResponse({"error": f"'{call.tool}' is a write tool - replay is read-only."},
                            status_code=400)
    ev_id = f"r{int(time.time() * 1000) % 100000}"
    start = {"type": "tool_start", "id": ev_id, "tool": call.tool, "args": call.args,
             "kind": "read", "intent": call.intent or "history replay (full result)",
             "source": "bridge", "ts": time.time()}
    http_token = _http_calls.set([])
    invoke_token = _via_invoke.set(True)
    t0 = time.perf_counter()
    try:
        result = await pods_mcp.mcp.call_tool(call.tool, call.args or {})
        data = _unwrap(result)
        ok, err = True, None
    except Exception as e:  # noqa: BLE001
        data, ok, err = None, False, str(e)
    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    http = _http_calls.get() or []
    _http_calls.reset(http_token)
    _via_invoke.reset(invoke_token)
    publish(start)
    publish({"type": "tool_end", "id": ev_id, "tool": call.tool, "ok": ok,
             "elapsed_ms": elapsed, "kind": "read", "source": "bridge", "ts": time.time(),
             "http": http[-1] if http else None, "http_calls": len(http), "error": err,
             **({} if not ok else _summarize(call.tool, data))})
    if not ok:
        return JSONResponse({"error": err}, status_code=502)
    return {"result": data, "elapsed_ms": elapsed}


@app.post("/audit/tool")
async def audit_tool(call: ToolCall, request: Request):
    caller = call.agent or _rest_caller(request)
    async def gen():
        start, end = await invoke(call.tool, call.args, call.intent, agent=caller)
        yield _sse(start)
        await asyncio.sleep(0)
        yield _sse(end)
        yield _sse({"type": "done", "calls": 1, "elapsed_ms": end["elapsed_ms"]})
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/audit/stream")
async def audit_stream():
    """The GLOBAL audit trail: last ~100 events replayed, then live. Read-only —
    this is what the tab's Live mode watches while you drive from Claude Code."""
    async def gen():
        q: asyncio.Queue = asyncio.Queue()
        _subs.add(q)
        try:
            for ev in list(_recent)[-100:]:
                yield _sse(ev)
            yield _sse({"type": "live", "text": "watching MCP activity"})
            while True:
                ev = await q.get()
                yield _sse(ev)
        finally:
            _subs.discard(q)
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/audit/history")
async def audit_history(batches: int = 60, batch_calls: int = 1500):
    """All-time MCP activity from the durable log: a daily heatmap ({date:{c,r,w}}),
    totals, top tools, and recent batches (bursts of calls, each with its per-call
    'why'). Falls back to the in-memory buffer when BRIDGE_STATE_DIR is unset."""
    calls = _pair_calls(_read_events())
    heat: dict[str, dict] = {}
    by_tool: dict[str, int] = {}
    by_source: dict[str, int] = {}
    reads = writes = 0
    for c in calls:
        day = datetime.fromtimestamp(c["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        h = heat.setdefault(day, {"c": 0, "r": 0, "w": 0})
        h["c"] += 1
        if c["kind"] == "write":
            h["w"] += 1
            writes += 1
        else:
            h["r"] += 1
            reads += 1
        by_tool[c["tool"]] = by_tool.get(c["tool"], 0) + 1
        by_source[c["source"]] = by_source.get(c["source"], 0) + 1
    top = sorted(by_tool.items(), key=lambda kv: -kv[1])[:15]
    return {
        "persisted": bool(_AUDIT_PATH),
        "path": _AUDIT_PATH or None,
        "totals": {
            "calls": len(calls), "reads": reads, "writes": writes,
            "by_source": by_source, "distinct_tools": len(by_tool),
            "first_ts": calls[0]["ts"] if calls else None,
            "last_ts": calls[-1]["ts"] if calls else None,
        },
        "top_tools": [{"tool": t, "count": n} for t, n in top],
        "heatmap": heat,
        "batches": _build_batches(calls[-batch_calls:], batches),
    }


@app.post("/chat")
async def chat(req: ChatReq):
    from chat_loop import run_chat, llm_configured

    async def gen():
        if not llm_configured(req.provider):
            yield _sse({"type": "assessment",
                        "text": "No LLM configured. Set ANTHROPIC_API_KEY or "
                                "LLM_BASE_URL to enable chat. Audit + overview "
                                "work without one."})
            yield _sse({"type": "done", "calls": 0, "elapsed_ms": 0})
            return
        try:
            async for event in run_chat(req.question, invoke,
                                        provider=req.provider):
                if event.get("type") == "chat_summary":
                    # Per-chat accounting (egress/ingress bytes, turns,
                    # provider kind) belongs in the durable audit trail;
                    # chat_loop already redacted it (no api_key), so publish
                    # as-is to stamp a seq.
                    event = publish(event)
                yield _sse(event)
        except Exception as e:  # noqa: BLE001 — surface LLM/transport failure
            # as a clean SSE event instead of aborting the stream mid-body
            # (the experiment runner records this as a failed run).
            yield _sse({"type": "error", "text": f"chat failed: {e}"})
            yield _sse({"type": "done", "calls": 0, "elapsed_ms": 0})
    return StreamingResponse(gen(), media_type="text/event-stream")
