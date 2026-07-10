"""
Pluggable LLM chat loop for the audit bridge.

Drives a model with the MCP tools and yields the SAME audit events the bridge
emits for direct calls, so the UI renders one stream. EVERY tool the model calls
goes through `invoke(...)` — nothing bypasses the audit trail.

Provider is chosen from env at call time:
    ANTHROPIC_API_KEY  [+ ANTHROPIC_MODEL=claude-opus-4-8]        -> Anthropic Messages API
    LLM_BASE_URL       [+ LLM_API_KEY, LLM_MODEL]                 -> OpenAI-compatible
                                                                    (litellm, ollama, ...)
Local dev: set either. When the bridge is later deployed as a stack, point
LLM_BASE_URL at the litellm stack — no code change.

Both providers are spoken over httpx (no vendor SDK). A model turn yields text as
`token` events, then for each requested tool call: `plan` -> `tool_start` ->
`tool_end`, feeding results back until the model produces a final answer.
"""
import json
import os
import time

import httpx

SYSTEM = (
    "You are the Tapis Pods assistant. Answer questions about pods, stacks, "
    "templates, volumes and snapshots by calling the provided tools. Prefer "
    "read tools; to create things, use deploy_from_template (fill only the "
    "template's blanks). Before any write/deploy, state plainly what it will do "
    "and whether it's advisable right now. Be concise; cite what you observed."
)
MAX_TURNS = 8


def llm_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("LLM_BASE_URL"))


async def _tool_defs(mcp):
    """MCP tools -> a normalized [{name, description, schema}] list."""
    defs = []
    for t in await mcp.list_tools():
        schema = (getattr(t, "inputSchema", None) or getattr(t, "parameters", None)
                  or {"type": "object", "properties": {}})
        defs.append({"name": t.name,
                     "description": (getattr(t, "description", "") or "")[:900],
                     "schema": schema})
    return defs


# --------------------------------------------------------------------------
# Providers: each returns (text, tool_calls) and knows how to thread messages.
# tool_calls: list of {"id","name","args"}.
# --------------------------------------------------------------------------
class _Anthropic:
    def __init__(self):
        self.key = os.environ["ANTHROPIC_API_KEY"]
        self.model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")

    def tools(self, defs):
        return [{"name": d["name"], "description": d["description"],
                 "input_schema": d["schema"]} for d in defs]

    async def complete(self, messages, tools):
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                             headers={"x-api-key": self.key,
                                      "anthropic-version": "2023-06-01"},
                             json={"model": self.model, "max_tokens": 2048,
                                   "system": SYSTEM, "messages": messages, "tools": tools})
            r.raise_for_status()
            data = r.json()
        text, calls = "", []
        for block in data.get("content", []):
            if block["type"] == "text":
                text += block["text"]
            elif block["type"] == "tool_use":
                calls.append({"id": block["id"], "name": block["name"], "args": block["input"]})
        messages.append({"role": "assistant", "content": data["content"]})
        return text, calls

    def add_tool_results(self, messages, results):
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tc["id"],
             "content": json.dumps(out)[:6000]} for tc, out in results]})


class _OpenAICompat:
    def __init__(self):
        self.base = os.environ["LLM_BASE_URL"].rstrip("/")
        self.key = os.environ.get("LLM_API_KEY", "x")
        self.model = os.environ.get("LLM_MODEL", "gpt-4o-mini")

    def tools(self, defs):
        return [{"type": "function", "function": {
            "name": d["name"], "description": d["description"], "parameters": d["schema"]}}
            for d in defs]

    async def complete(self, messages, tools):
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{self.base}/chat/completions",
                             headers={"Authorization": f"Bearer {self.key}"},
                             json={"model": self.model, "messages": messages,
                                   "tools": tools, "tool_choice": "auto"})
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
        calls = [{"id": tc["id"], "name": tc["function"]["name"],
                  "args": json.loads(tc["function"].get("arguments") or "{}")}
                 for tc in (msg.get("tool_calls") or [])]
        messages.append(msg)
        return msg.get("content") or "", calls

    def add_tool_results(self, messages, results):
        for tc, out in results:
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": json.dumps(out)[:6000]})


def _make_client():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _Anthropic()
    if os.environ.get("LLM_BASE_URL"):
        return _OpenAICompat()
    return None


# --------------------------------------------------------------------------
# The loop (provider-agnostic). `client` just needs the 4 methods above; the
# tests pass a mock. `invoke` is the bridge's audited tool runner.
# --------------------------------------------------------------------------
async def run_chat(question, invoke, mcp=None, client=None):
    import pods_mcp
    mcp = mcp or pods_mcp.mcp
    client = client or _make_client()
    if client is None:
        yield {"type": "assessment", "text": "No LLM configured."}
        yield {"type": "done", "calls": 0, "elapsed_ms": 0}
        return

    defs = await _tool_defs(mcp)
    tools = client.tools(defs)
    messages = [{"role": "user", "content": question}]
    total, t0 = 0, time.perf_counter()

    for _ in range(MAX_TURNS):
        text, calls = await client.complete(messages, tools)
        if text:
            yield {"type": "token", "text": text}
        if not calls:
            break
        results = []
        for tc in calls:
            yield {"type": "plan", "text": f"calling {tc['name']}", "tool": tc["name"],
                   "args": tc["args"]}
            start, end = await invoke(tc["name"], tc["args"], intent=text[:120])
            yield start
            yield end
            total += 1
            results.append((tc, end.get("evidence") if end["ok"]
                            else {"error": end.get("error")}))
        client.add_tool_results(messages, results)

    yield {"type": "done", "calls": total,
           "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}
