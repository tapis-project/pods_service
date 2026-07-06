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

A /chat request may also OVERRIDE the provider per-request (the experiment
harness in experiments/ swaps providers per run) by passing a cfg dict:
    {"kind": "anthropic"|"openai", "base_url": ..., "model": ..., "api_key": ...}
Missing cfg keys fall back to the env values above. The bridge is localhost-only
so an in-body api_key is acceptable — but it must NEVER appear in audit events.

Each provider instance also counts wire bytes to/from the LLM endpoint
(egress_bytes = request bodies sent, ingress_bytes = response bodies received)
plus token usage, and run_chat emits one `chat_summary` event per chat with the
totals — that's the "bytes egressed to the provider" number the paper reports.

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
    "and whether it's advisable right now. Be concise; cite what you observed. "
    # Tool-call formatting guideline. Some models (seen with SambaNova-served
    # Llama/Qwen via LiteLLM) echo each tool's JSON-schema envelope back as the
    # argument VALUE — e.g. {\"pod_id\": {\"type\": \"string\", \"value\": ...}}
    # instead of {\"pod_id\": \"papertesttarget\"}. That produces malformed
    # calls the service rejects. This guideline asks for raw values; the harness
    # also repairs the envelope defensively (see _unwrap_schema_echo).
    "When you call a tool, pass each argument as its RAW value only — a string, "
    "number, or boolean — never a {\"type\": ..., \"value\": ...} schema "
    "wrapper. Example: pass pod_id as \"papertesttarget\", not "
    "{\"type\": \"string\", \"value\": \"papertesttarget\"}."
)
MAX_TURNS = 8


def llm_configured(provider: dict | None = None) -> bool:
    return bool(provider or os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("LLM_BASE_URL"))


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
# tool_calls: list of {"id","name","args"}. cfg (optional, per-request) overrides
# env; api_key stays on the instance only — never emit it in an event.
# Bodies are serialized by hand (not httpx json=) so len(body) is the exact
# egress byte count; ingress counts len(r.content).
# --------------------------------------------------------------------------
class _Wire:
    """Byte + token counters shared by both providers."""

    def __init__(self):
        self.egress_bytes = 0
        self.ingress_bytes = 0
        self.tokens_in = 0
        self.tokens_out = 0

    async def _post(self, url, headers, payload):
        body = json.dumps(payload).encode()
        self.egress_bytes += len(body)
        headers = {**headers, "content-type": "application/json"}
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(url, headers=headers, content=body)
            r.raise_for_status()
            self.ingress_bytes += len(r.content)
            return r.json()


class _Anthropic(_Wire):
    kind = "anthropic"

    def __init__(self, cfg: dict | None = None):
        super().__init__()
        cfg = cfg or {}
        self.key = cfg.get("api_key") or os.environ["ANTHROPIC_API_KEY"]
        self.model = cfg.get("model") or os.environ.get("ANTHROPIC_MODEL",
                                                        "claude-opus-4-8")
        self.base = (cfg.get("base_url") or "https://api.anthropic.com").rstrip("/")

    def tools(self, defs):
        return [{"name": d["name"], "description": d["description"],
                 "input_schema": d["schema"]} for d in defs]

    async def complete(self, messages, tools):
        data = await self._post(
            f"{self.base}/v1/messages",
            {"x-api-key": self.key, "anthropic-version": "2023-06-01"},
            {"model": self.model, "max_tokens": 2048,
             "system": SYSTEM, "messages": messages, "tools": tools})
        usage = data.get("usage") or {}
        self.tokens_in += usage.get("input_tokens") or 0
        self.tokens_out += usage.get("output_tokens") or 0
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


class _OpenAICompat(_Wire):
    kind = "openai"

    def __init__(self, cfg: dict | None = None):
        super().__init__()
        cfg = cfg or {}
        self.base = (cfg.get("base_url") or os.environ["LLM_BASE_URL"]).rstrip("/")
        self.key = cfg.get("api_key") or os.environ.get("LLM_API_KEY", "x")
        self.model = cfg.get("model") or os.environ.get("LLM_MODEL", "gpt-4o-mini")
        # Extra request headers (e.g. X-Tapis-Token for a litellm pod behind
        # Tapis ingress auth, which ignores Authorization: Bearer). Never
        # emitted in audit events — stays on the instance like api_key.
        self.extra_headers = cfg.get("headers") or {}

    def tools(self, defs):
        return [{"type": "function", "function": {
            "name": d["name"], "description": d["description"], "parameters": d["schema"]}}
            for d in defs]

    async def complete(self, messages, tools):
        data = await self._post(
            f"{self.base}/chat/completions",
            {"Authorization": f"Bearer {self.key}", **self.extra_headers},
            {"model": self.model, "messages": messages,
             "tools": tools, "tool_choice": "auto"})
        usage = data.get("usage") or {}
        self.tokens_in += usage.get("prompt_tokens") or 0
        self.tokens_out += usage.get("completion_tokens") or 0
        msg = data["choices"][0]["message"]
        calls = [{"id": tc["id"], "name": tc["function"]["name"],
                  "args": json.loads(tc["function"].get("arguments") or "{}")}
                 for tc in (msg.get("tool_calls") or [])]
        messages.append(msg)
        return msg.get("content") or "", calls

    def add_tool_results(self, messages, results):
        for tc, out in results:
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": json.dumps(out)[:6000]})


def _make_client(provider: dict | None = None):
    if provider:  # per-request override (experiment harness)
        kind = provider.get("kind", "openai")
        if kind == "anthropic":
            return _Anthropic(provider)
        if kind == "openai":
            return _OpenAICompat(provider)
        raise ValueError(f"unknown provider kind: {kind!r}")
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _Anthropic()
    if os.environ.get("LLM_BASE_URL"):
        return _OpenAICompat()
    return None


# --------------------------------------------------------------------------
# The loop (provider-agnostic). `client` just needs the 4 methods above; the
# tests pass a mock. `invoke` is the bridge's audited tool runner.
# --------------------------------------------------------------------------
def _unwrap_schema_echo(args):
    """Repair the schema-echo tool-call defect: some models emit each argument
    as its JSON-schema envelope instead of the bare value, e.g.
        {"pod_id": {"type": "string", "value": {"pod_id": "papertesttarget"}}}
    where the intended call is {"pod_id": "papertesttarget"}. Unwrap the
    {"type","value"} envelope (and the doubled {argname: value} nesting some
    providers add) so the call still actuates. Returns (clean_args, n_repaired).
    Non-dict args and already-clean args pass through with n_repaired == 0, so
    this is a no-op for models that format correctly."""
    if not isinstance(args, dict):
        return args, 0
    out, n = {}, 0
    ENVELOPE_KEYS = {"type", "value", "description", "title"}
    for k, v in args.items():
        if (isinstance(v, dict) and "value" in v
                and set(v.keys()) <= ENVELOPE_KEYS):
            v = v["value"]
            n += 1
            # doubled nesting: value is itself {argname: realvalue}
            if isinstance(v, dict) and len(v) == 1 and k in v:
                v = v[k]
        out[k] = v
    return out, n


async def run_chat(question, invoke, mcp=None, client=None, provider=None):
    if mcp is None:  # lazy: lets tests pass a mock without importing the server
        import pods_mcp
        mcp = pods_mcp.mcp
    client = client or _make_client(provider)
    if client is None:
        yield {"type": "assessment", "text": "No LLM configured."}
        yield {"type": "done", "calls": 0, "elapsed_ms": 0}
        return

    defs = await _tool_defs(mcp)
    # Optional per-request tool curation: provider cfg may carry
    # `tools_allowlist` (list of tool names). Small local models can't see a
    # 34k-token 88-tool schema inside their context window, so the local tier
    # offers a curated subset. Filtering happens HERE, before serialization —
    # the model never sees (and can't call) anything outside the subset;
    # anything it does call still goes through the audited invoke().
    allow = (provider or {}).get("tools_allowlist")
    if allow:
        allow = set(allow)
        defs = [d for d in defs if d["name"] in allow]
    tools = client.tools(defs)
    messages = [{"role": "user", "content": question}]
    total, turns, coerced, t0 = 0, 0, 0, time.perf_counter()

    for _ in range(MAX_TURNS):
        text, calls = await client.complete(messages, tools)
        turns += 1
        if text:
            yield {"type": "token", "text": text}
        if not calls:
            break
        results = []
        for tc in calls:
            # Defensively repair schema-echo envelopes before actuating; count
            # repairs so the mitigation's effect is measured, not hidden.
            clean_args, nfix = _unwrap_schema_echo(tc["args"])
            coerced += nfix
            yield {"type": "plan", "text": f"calling {tc['name']}", "tool": tc["name"],
                   "args": clean_args}
            start, end = await invoke(tc["name"], clean_args, intent=text[:120])
            yield start
            yield end
            total += 1
            # Feed back count+summary+evidence, not evidence alone: the
            # bridge trims list evidence to a 3-item preview for the audit
            # log, so without result_count the model can't answer "how
            # many" questions — external /mcp clients see full results, so
            # this keeps the /chat tiers comparable.
            results.append((tc, {"result_count": end.get("result_count"),
                                 "summary": end.get("summary"),
                                 "evidence": end.get("evidence")} if end["ok"]
                            else {"error": end.get("error")}))
        client.add_tool_results(messages, results)

    # Full message array (what was actually sent to/received from the model) —
    # the reproducibility + egress-audit artifact. Yielded to the requester
    # only, NEVER published to the audit log (too big); contains no api_key.
    yield {"type": "transcript", "messages": messages}
    # One accounting event per chat. getattr defaults keep mock clients (tests)
    # working; NO api_key/base_url here — this line lands in the audit log.
    yield {"type": "chat_summary",
           "provider_kind": getattr(client, "kind", "?"),
           "model": getattr(client, "model", "?"),
           "turns": turns, "calls": total,
           "tools_offered": len(defs),
           "args_coerced": coerced,
           "egress_bytes": getattr(client, "egress_bytes", None),
           "ingress_bytes": getattr(client, "ingress_bytes", None),
           "tokens_in": getattr(client, "tokens_in", None),
           "tokens_out": getattr(client, "tokens_out", None),
           "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
           "ts": time.time()}
    yield {"type": "done", "calls": total,
           "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}
