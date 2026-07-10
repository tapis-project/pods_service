# Tapis Pods — MCP server

A spec-reflecting [MCP](https://modelcontextprotocol.io) server for the Pods API.
The tool catalog **is** the live OpenAPI spec: at startup we fetch
`/v3/openapi.json` and FastMCP turns every permitted operation into a tool. New
Pods endpoints become tools on restart with **no code changes here**.

The only hand-written code is:
- `route_maps.py` — the curation/scope policy (what the LLM may see and do).
- three generic template helpers in `pods_mcp.py` (`list_deployable_templates`,
  `deploy_from_template`, `update_from_template`) that let a model create things
  by *filling a template's blanks* instead of authoring giant nested bodies.
- `placeholders.py` — a dependency-free port of the `${:?...}` grammar from
  `service/secret_utils.py`.

None of it grows with the endpoint count. It imports nothing from `service/`.

## Run (stdio, v1)

```bash
cd mcp
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

export TAPIS_BASE_URL=https://tacc.develop.tapis.io
export TAPIS_TOKEN=<your X-Tapis-Token>     # server acts AS you; per-user perms enforced by the service
fastmcp run pods_mcp.py                       # stdio
# or inspect interactively:
fastmcp dev pods_mcp.py
```

Point an MCP client (Claude Desktop, an IDE) at the same command. Start with the
`catalog()` tool — it's a table of contents (resources grouped by tag, read vs.
write), then `list_deployable_templates()` to see what you can deploy.

### Env

| var | required | meaning |
|-----|----------|---------|
| `TAPIS_BASE_URL` | yes | tenant base, e.g. `https://tacc.develop.tapis.io` |
| `TAPIS_TOKEN` | for live calls | your `X-Tapis-Token` |
| `PODS_OPENAPI_URL` | no | override spec URL (default `${TAPIS_BASE_URL}/v3/openapi.json`) |

## Scope

Read + lifecycle (start/stop/restart are GETs) + deploy/update-from-template +
permission set/delete. **Excluded** (see `route_maps.py`): `exec` (RCE), file
upload/download/listing, raw secret **values**, forwardAuth/oauth, jupyter,
infra endpoints, and all raw `create_*`/`update_*`/`delete_*` — creation is
funneled through templates so the complex bodies never reach the model.

To change scope, edit `route_maps.py` only.

## Audited UI — local bridge + tapis-ui tab

`bridge.py` (FastAPI) is the choke point that makes every MCP tool call visible to
the UI. It reuses the exact curated 35 tools, so nothing runs unaudited. Each call
emits `tool_start`/`tool_end` (timing, the underlying HTTP call, summary, evidence)
over SSE. `chat_loop.py` drives an LLM with the tools, routing every model tool
call through the same audit path.

Run the bridge:
```bash
pip install -r bridge-requirements.txt        # fastapi, uvicorn
export TAPIS_BASE_URL=https://tacc.develop.tapis.io
export TAPIS_TOKEN=<your X-Tapis-Token>
# optional LLM (chat): pick ONE, or omit for audit-only
export ANTHROPIC_API_KEY=sk-ant-...            # direct Anthropic (default opus)
#   or point at any OpenAI-compatible endpoint (litellm stack, local ollama):
# export LLM_BASE_URL=https://<litellm>/v1  LLM_API_KEY=...  LLM_MODEL=claude-...
uvicorn bridge:app --port 8100
```
Endpoints: `GET /health`, `GET /catalog`, `POST /audit/tool` (SSE), `POST /chat` (SSE).

The tapis-ui tab (`src/app/Pods/_components/PodsAssistant/`) is a **local-only** Pods
top-bar tab (shown only on `localhost`). Run tapis-ui (`pnpm dev`), open Pods, click
**Assistant**. It reads the bridge at `http://localhost:8100` (override with
`VITE_PODS_MCP_BRIDGE`). Shows: streamed answer, tool-call chips (the "80 GETs"
collapse into a chip strip, not raw JSON), facets + coverage, prism overview tiles,
and per-call evidence. Chat works once an LLM is configured; audit + overview work
without one.

## Later: stack-hosted MCP

`Dockerfile` builds an http-transport image to deploy as a one-member Pods stack
behind `tapis_auth`. **TODO:** per-request token forwarding (read the caller's
`X-Tapis-Token` via `fastmcp.server.dependencies.get_http_headers` and inject it
into the outbound client) so the server acts as the *caller*; and point the
bridge's `LLM_BASE_URL` at the deployed litellm stack.
```
