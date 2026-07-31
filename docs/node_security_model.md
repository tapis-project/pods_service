# Node / Edge-Agent Security Model

How the edge-node feature authenticates callers, scopes permissions, and bounds
what each party can do. This is the reference for anyone extending node routes,
the agent, or the publish/routes surface. Companion to `traefik_routing.md` and
`developer_docs.md`.

Two independent trust boundaries meet here:

1. **User ↔ central** — ordinary Tapis JWT auth + per-node object permissions,
   graded exactly like pods/volumes/templates.
2. **Agent ↔ central** — a node-scoped bearer token (`X-Pods-Node-Token`), NOT a
   Tapis JWT, authenticated in-handler. An agent token grants nothing on user
   routes; a user JWT grants nothing on agent routes.

## 1. Endpoint access matrix

Permission is enforced **centrally in `auth.py::check_route_permissions`** before
the handler runs (via `check_object_id` → `check_permissions`, tenant-scoped).
For user-token routes **the allowlist level IS the enforcement** — handlers then
call `_get_node_or_404`, which only re-scopes by tenant/site.

| Route | Method | Level | Who can call |
|---|---|---|---|
| `/pods/nodes` | GET | NONE | any authed user, filtered to nodes they have READ on |
| `/pods/nodes` | POST | NONE | any authed user (creator becomes node ADMIN) |
| `/pods/nodes/{id}` | GET | READ | node READ |
| `/pods/nodes/{id}` | DELETE (+`?decommission`) | ADMIN | node ADMIN |
| `/pods/nodes/{id}/regenerate` | POST | ADMIN | node ADMIN |
| `/pods/nodes/{id}/routes` | GET | READ | node READ |
| `/pods/nodes/{id}/routes` | POST | ADMIN | node ADMIN (+ SSRF backend guard) |
| `/pods/nodes/{id}/routes/{rid}` | GET / DELETE | READ / ADMIN | node READ / ADMIN |
| `/pods/nodes/{id}/routes/{rid}/probe` | GET | USER | node USER (+ SSRF dial guard) |
| `/pods/nodes/{id}/logs` | GET | READ | node READ |
| `/pods/nodes/{id}/metrics` | GET | READ | node READ |
| `/pods/nodes/{id}/bench` | POST / GET | USER / READ | node USER / READ |
| `/pods/nodes/{id}/settings` | PUT | ADMIN | node ADMIN (whitelist-sanitized) |
| `/pods/nodes/{id}/ledger` | GET | READ | node READ |
| `/pods/nodes/{id}/restart` | POST | ADMIN | node ADMIN |
| `/pods/nodes/{id}/update` | POST | ADMIN | node ADMIN + `allow_self_update` effective |
| `/pods/nodes/{id}/shell` | POST | ADMIN | node ADMIN **and** box `PODS_AGENT_ALLOW_SHELL=true` |
| `/pods/nodes/{id}/shell` | GET | READ | node READ (sees command text + output) |
| `/pods/nodes/{id}/rotate` | POST | ADMIN | node ADMIN |
| `/pods/nodes/{id}/join` | POST | (token-skip) | holder of the one-time claim token |
| `/pods/nodes/{id}/checkin` | POST | (token-skip) | agent-token-only |
| `/pods/nodes/{id}/commands` | GET | (token-skip) | agent-token-only |
| `/pods/nodes/{id}/commands/{cid}/result` | POST | (token-skip) | agent-token-only |
| `/pods/nodes/{id}/logs` | POST | (token-skip) | agent-token-only |
| `/pods/nodes/{id}/agent-source` | GET | (token-skip) | agent-token-only |

Invariants: every mutating action (delete, regenerate, settings, restart,
update, shell, rotate) is **ADMIN and centrally enforced** — none reachable at a
lower level (regexes are `^…$`-anchored; unlisted routes fail closed). The six
agent routes skip Tapis-token auth and authenticate in-handler via
`_require_agent` (or claim-token match for join). Node creation at NONE is
intentional (self-service registration); the creator is seeded `{user}:ADMIN`
and only ever gets ADMIN on nodes they create.

## 2. Credential model

| Secret | Prefix | At rest | Exposure |
|---|---|---|---|
| Claim token | `pnc_` | SHA-256 hash | shown once at create/regenerate; single-use, TTL (hours) |
| Agent token | `pna_` | SHA-256 hash | shown once at join; sent as `X-Pods-Node-Token` |
| Pending rotate token | `pna_` | SHA-256 hash | raw scrubbed from the command row at delivery/expiry/completion |

- 256-bit CSPRNG (`secrets.token_urlsafe(32)`); validated with `hmac.compare_digest`
  on the hash; scoped to exactly one node (`_require_agent` matches only that
  node's active-or-pending hash). No raw token is ever logged or returned twice.
- **Revocation**: `/regenerate` nulls the hash (parks the agent until a human
  re-joins on the box); hard-delete removes the row.
- **Rotation** (no downtime): mint → pending hash where BOTH tokens authenticate
  → the raw token rides one exactly-once command delivery → the agent confirms
  **using the new token** (that request is the proof it persisted) → promote +
  revoke old. Any failure leaves the old token active; unconfirmed pendings
  expire (default 60 min, swept on the next checkin).

**Rotate vs Regenerate — operational rule.** Rotation is for *routine hygiene*
only. It assumes the running agent is the sole token holder, so it is **not** a
safe response to a *suspected token theft*: an attacker who already holds the
token can race the agent's command poll, confirm the new token first, and lock
the real agent out. For a compromised token use **Regenerate** (revokes now,
forces a human re-join on the box). The rotate UI states this.

## 3. What a stolen credential grants

| Stolen | Scope | Can do | Cannot do |
|---|---|---|---|
| Agent token (`state.json`) | ONE node, one tenant | spoof that node's telemetry/status/inventory; receive that node's queued commands (exactly-once, so it can intercept a delivery); falsely-complete an already-queued command | read other nodes/tenants/pods; act as the user's Tapis account; **queue** shell/rotate/update/delete (those need a Tapis JWT) — no self-escalation |
| Claim token | ONE node, single-use, TTL | exchange for an agent token via `/join` if unconsumed | anything after join consumes it |
| Central write access to `agent/pods_agent.py` | **whole fleet** with `allow_self_update` on | push code the agents `os.execv` | reach a box that pinned `PODS_AGENT_ALLOW_SELF_UPDATE=false` |
| Node-ADMIN JWT | that user's nodes | restart/update/rotate/decommission/settings; shell only if the box enabled it | — |

The docker socket mount is the real edge-side risk: registering a node ≈ handing
over the box (documented in `agent/README.md`). Keep that in mind for node
placement — a dedicated VM/microVM, not a workstation.

## 4. Agent code-execution surface (two levers, different trust)

- **Self-update** (`/update`, node-ADMIN + `allow_self_update`): the agent fetches
  central's *own* source and re-execs it after **sha256 verification + compile
  check**, keeping the previous copy and abandoning a crash-looping install after
  3 tries. Important honesty: the sha256 is verified against a value central
  *itself* advertised — this is **integrity, not authenticity**. Central is the
  trust root; whoever can write central's `agent/pods_agent.py` (or MITM it with
  `PODS_AGENT_INSECURE=true`) can push code to every edge that opted in. This is
  consistent with the "central is trusted" model, but self-update should be left
  **off** (the default) on any box that doesn't need it, and a box can hard-pin
  it off via env. A pinned code-signing key would upgrade this to authenticity;
  it is a deliberate future item, not shipped.
- **Shell** (`/shell`, node-ADMIN): arbitrary one-shot commands. Enable is
  **env-only** — `PODS_AGENT_ALLOW_SHELL=true` on the box. Central genuinely
  cannot turn it on: the key is absent from the server settings whitelist (so a
  `PUT /settings` drops it) AND the agent ignores any central value for it
  (`_ENV_ONLY_KEYS`). Bounded by a server-clamped timeout (1–300 s), a 4096-char
  command cap, and a 20 000-char output cap; every run is ledgered with its exit
  code. Note the command runs as the agent's user, which in the standard
  containerized deploy is **root with the docker socket** — i.e. root on the host.

## 5. Central's powers over a well-behaved edge (all bounded)

| Power | Route/gate | Bounded? |
|---|---|---|
| Read status/inventory/metrics/logs | passive | agent chooses what to ship; probes env-gateable |
| Change settings overlay | `PUT /settings` (ADMIN) | whitelisted keys only; **env pins on the box always win**; cannot enable shell |
| Restart | `/restart` (ADMIN) | re-exec, no new code |
| Self-update | `/update` (ADMIN) | gated by `allow_self_update`; box can pin off (§4) |
| Shell | `/shell` (ADMIN) | **env-only**, central cannot enable (§4) |
| Decommission/delete | `/delete[?decommission]` (ADMIN) | agent wipes only its own state/container; never other host state |
| Tailnet join / host commands | agent-side | **never silent** — TTY-confirm or explicit `PODS_AGENT_HOST_CMDS=always` |

The agent never runs host commands silently and only ever removes its own
property. Two escape hatches the box fully controls: pin `allow_self_update=false`
and never set `allow_shell`.

## 6. Data exposure & publish (SSRF containment)

- `display()` scrubs every secret (all token hashes, `pending_token_ts`,
  `inventory`, `permissions`, `tenant_id`, `site_id`, `action_logs`); the typed
  `NodeResponseModel`/`RouteResponseModel` subclass only the *Read* models, so
  secret columns are dropped even if a `display()` pop is ever missed
  (defense-in-depth). `inventory` has no exposing endpoint at all.
- **Publish backend guard**: a route with `tapis_auth=false` is world-reachable,
  and any authed user can create a node + route. `_reject_internal_backend`
  therefore refuses (for non-admins) any `backend_host` that is or resolves to a
  private / loopback / link-local (incl. cloud metadata `169.254.169.254`) /
  reserved / CGNAT address, plus `.svc`/`.cluster.local`/`.internal`/`.local`
  suffixes and single-label k8s names. The USER-level probe re-checks the target
  at dial time so an admin-created internal backend can't be reflected to a
  non-admin. Admins keep the escape hatch (central-side backends are legitimate).
- **Shell text/output is READ-visible**: `GET /shell` and the ledger expose the
  command and its stdout/stderr to any node-READ user. This is the intended audit
  surface — **do not put secrets in shell commands** (`env`, inline bearer
  tokens, etc. land in the ledger). Documented so it's a conscious model.
- **Checkin/ingest caps**: status (256 K) / inventory (1 M) / capabilities (128)
  are 413'd, not silently trimmed; `action_logs` is a 500-entry ring; metrics
  batch (1500) + extras (32 keys) clamped; log ingest caps the **decompressed**
  size on all encodings (gzip/zstd bomb-safe) + per-line/batch/body caps. All
  telemetry is per-node hard-capped at ingest and tenant-scoped; cross-node/
  cross-tenant reads are blocked by the node-permission gate + tenant-scoped store.

## 7. Known limits (accepted / deferred)

- **X-Pods-Tenant existence oracle** (LOW): the unauthenticated agent routes take
  the tenant from a header and resolve the node before authenticating, so an
  anonymous caller can distinguish 404 (no node) from 403 (exists, bad token) —
  a cross-tenant existence probe. No data/writes leak; node_ids must be guessed.
- **Self-update is integrity-not-authenticity** (§4) — central is a fleet trust
  root when boxes opt into self-update. Mitigation: default-off + per-box pin.
- **Raw-body size** at `/checkin` and `/logs` is buffered before the decompressed
  cap; a very large raw upload is bounded only by the pod's memory (no app/ingress
  body limit). Concurrent long-poll holds have no per-node/global cap.
- `cgarcia` is a hardcoded admin username (project convention) — remove before any
  external hardening pass.

These are tracked; the ones with live-code fixes applied are in the changelog /
commit history, the deferred ones in the team's working backlog.
