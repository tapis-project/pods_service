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
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
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
# set while the bridge itself is calling a tool, so the MCP middleware doesn't
# double-publish (invoke() publishes those directly, tagged source=bridge).
_via_invoke: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "via_invoke", default=False
)


def publish(event: dict) -> None:
    global _seq
    _seq += 1
    event = {**event, "seq": _seq}
    _recent.append(event)
    if len(_recent) > 500:
        del _recent[: len(_recent) - 500]
    for q in list(_subs):
        try:
            q.put_nowait(event)
        except Exception:  # noqa: BLE001 — a slow/closed subscriber never blocks publish
            pass


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
        kind = "write" if _is_write(tool) else "read"
        ev_id = f"m{int(time.time() * 1000) % 1000000}"
        publish({"type": "tool_start", "id": ev_id, "tool": tool, "args": args,
                 "kind": kind, "source": "claude-code", "ts": time.time()})
        t0 = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as e:  # noqa: BLE001
            publish({"type": "tool_end", "id": ev_id, "tool": tool, "ok": False,
                     "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                     "kind": kind, "error": str(e), "source": "claude-code"})
            raise
        data = _unwrap(result)
        publish({"type": "tool_end", "id": ev_id, "tool": tool,
                 "ok": not getattr(result, "is_error", False),
                 "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                 "kind": kind, "source": "claude-code", **_summarize(tool, data)})
        return result


pods_mcp.mcp.add_middleware(AuditMiddleware())


# ---------------------------------------------------------------------------
# Bridge-side tool invocation (interactive tab actions + /chat)
# ---------------------------------------------------------------------------
async def invoke(tool: str, args: dict, intent: str = "") -> tuple[dict, dict]:
    ev_id = f"t{int(time.time() * 1000) % 100000}"
    kind = "write" if _is_write(tool) else "read"
    start = {"type": "tool_start", "id": ev_id, "tool": tool, "args": args,
             "kind": kind, "intent": intent, "source": "bridge", "ts": time.time()}
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
           "elapsed_ms": elapsed, "kind": kind, "source": "bridge",
           "http": http[-1] if http else None, "http_calls": len(http),
           "error": err, **({} if not ok else _summarize(tool, data))}
    publish(start)
    publish(end)
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


class ChatReq(BaseModel):
    question: str


@app.get("/health")
async def health():
    tools = await pods_mcp.mcp.list_tools()
    return {"ok": True, "base_url": pods_mcp.API_BASE, "tools": len(tools),
            "token": bool(pods_mcp.TOKEN), "mcp_endpoint": "/mcp",
            "auth": "password-grant" if auth.password_grant_configured() else "manual",
            "verbose": pods_mcp.VERBOSE}


@app.get("/catalog")
async def catalog():
    _, end = await invoke("catalog", {})
    return end.get("evidence") or {}


@app.post("/audit/tool")
async def audit_tool(call: ToolCall):
    async def gen():
        start, end = await invoke(call.tool, call.args, call.intent)
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


@app.post("/chat")
async def chat(req: ChatReq):
    from chat_loop import run_chat, llm_configured

    async def gen():
        if not llm_configured():
            yield _sse({"type": "assessment",
                        "text": "No LLM configured. Set ANTHROPIC_API_KEY or "
                                "LLM_BASE_URL to enable chat. Audit + overview "
                                "work without one."})
            yield _sse({"type": "done", "calls": 0, "elapsed_ms": 0})
            return
        async for event in run_chat(req.question, invoke):
            yield _sse(event)
    return StreamingResponse(gen(), media_type="text/event-stream")
