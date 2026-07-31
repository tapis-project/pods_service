# pods-agent

Zero-dependency edge node agent for the Tapis Pods service. One Python file, stdlib only —
runs on any box with Python 3.9+. Speaks the HTTP-only node contract: single-use claim-token
join, then a checkin loop (capabilities + status + hash-gated docker inventory) and a
best-effort command poll. Central being unreachable is treated as a normal state.

**There is no library to install.** The agent is a single stdlib-only file — no pip, no
venv required. Any Python works: system `python3`, `nix develop`, whatever. Copy the file
(or use the container image) and run it. Building "locally" means nothing more than
`python3 agent/pods_agent.py --help`.

## What access does this agent have? (read this first)

This agent exists to *manage machines*, so be clear-eyed about what registering a node means:

- **The docker socket is root-equivalent.** Mounting `/var/run/docker.sock` lets the agent
  see and (once command dispatch lands) control every container on the host — and anything
  that controls docker can effectively become root on that machine. Today's agent only
  *reads* (inventory), but the command channel exists precisely so central can direct
  nodes later. Treat the mount as handing over the box.
- **Central is in the loop.** Node admins on the Tapis side can re-key this node, read its
  inventory, and eventually send it commands. Register machines you *intend to be managed*
  — a dedicated VM, microVM, or spare box — not your personal workstation.
- **Minimal-risk ways to try it:**
  - run without the socket mount (or set `PODS_AGENT_CHECK_DOCKER=false`) — the agent
    becomes heartbeat/status-only and holds nothing but its own node token;
  - a bare `host`-type node with no socket and no tailscale is report-only;
  - on shared boxes, prefer rootless docker or a dedicated user account to shrink the
    blast radius.
- **What the token can and can't do:** the node token in `state.json` (mode 0600)
  authenticates *this node's* checkins only — it cannot read other nodes or act as your
  Tapis account. Revoke it any time with **Regenerate** in TapisUI (the node goes offline
  until re-joined; that's the point).
- **Kubernetes flavor is read-only and self-auditing.** The kubectl-apply manifest (from
  TapisUI's Add Node for `k8s` nodes) grants a namespaced ServiceAccount list/get/watch
  on pods plus pod-log reads — no create, delete, exec, or secrets. Before using ANY of
  it, the agent probes what it was actually granted (SelfSubjectAccessReview) and reports
  only the confirmed permissions as `k8s.*` capabilities — a missing RoleBinding shows up
  as a visible capability gap in the UI, never as silent 403 churn. Access is re-probed
  hourly (`PODS_AGENT_K8S_REPROBE`), so an RBAC fix lands without a restart.

Longer-form guides belong with the Tapis docs — a pods-agent page in the style of the
other specialized-service guides (e.g. the ETL guide) is planned; until then the pods
docs live at https://tapis.readthedocs.io/en/latest/technical/pods.html.

## Quick start (dev loop: docker-container edge → develop minikube central)

1. Create the node (Tapis token, e.g. via TapisUI or curl):

   ```bash
   curl -s -X POST "$BASE/v3/pods/nodes" -H "X-Tapis-Token: $JWT" \
     -H 'Content-Type: application/json' \
     -d '{"node_id": "edge-dev", "type": "docker", "description": "dev laptop container edge"}'
   ```

   The response contains `claim_token` and a ready-made `join_command` — both shown ONLY once.

2. Build and run the agent container next to tapis-ui:

   ```bash
   docker build -t pods-agent ./agent
   docker run -d --name pods-agent \
     -v /var/run/docker.sock:/var/run/docker.sock \
     -v pods-agent-state:/var/lib/pods-agent \
     -e PODS_JOIN_URL="$BASE/v3/pods" \
     -e PODS_JOIN_TOKEN="pnc_..." \
     -e PODS_NODE_ID=edge-dev \
     pods-agent
   ```

   `run` auto-joins when no state exists and the `PODS_JOIN_*` env vars are set, so one
   `docker run` is the whole bootstrap. Bare-host flavor: `./pods_agent.py join --url ... --node ... --token ...`
   then `./pods_agent.py run`.

3. Watch it: `docker logs -f pods-agent`, then `GET /pods/nodes/edge-dev` — `liveness`
   should read `live`, and capabilities should include `runtime.docker`.

## Shell commands (off by default, env-only to enable)

Central can ask the agent to run a one-shot shell command — but ONLY if the box
itself opted in with `PODS_AGENT_ALLOW_SHELL=true`. This is the single
capability central can never switch on remotely: unlike self-update (which
fetches central's own hash-verified code), a shell command is arbitrary code,
so its enable lives physically on the machine. The agent ignores any central
setting of this key entirely, and the server's settings whitelist won't even
store one.

With it enabled: node-ADMIN users queue a command, the agent runs it through
the box's shell as the agent's own user, and reports exit code, stdout, stderr,
and duration. Every run is bounded — a server-clamped timeout (default 60 s,
max 300 s) after which the process is killed, and output capped at 20 000
characters per stream. The command and its exit code are written to the node's
action ledger, so shell use is auditable after the fact. This is deliberately
NOT an interactive shell: no PTY, no session, no streaming — one command, one
recorded result. Long-running work belongs in a pod.

## Token rotation with no downtime

`/regenerate` revokes the agent's token immediately and parks the agent until a
human re-runs a join on the box. Rotation is the online alternative:

1. central mints a new token and stores it as *pending* — both the current and
   the pending token authenticate from this moment,
2. the new token rides a `rotate` command down the already-authed channel,
3. the agent persists it and then confirms **using the new token**,
4. that confirmation is the proof it landed: central promotes pending → active
   and revokes the old one.

If anything goes wrong — agent offline, crash between persist and confirm,
confirmation that never arrives — the old token is still active, the agent
keeps checking in, and the unconfirmed pending simply expires (default 60 min).
An agent whose confirmation fails rolls its own state back to the old token, so
both sides agree. Rotation never requires touching the box.

## How command delivery works (long-poll)

The agent is the only side that ever opens a connection — edges live behind
NAT/firewalls, so central can never dial out. That never changes. What the
long-poll adds (agent 0.5.0+ against a central that advertises `commands_wait`
in its checkin endpoints) is central *answering slowly on purpose*:

1. Between heartbeats, the agent sends `GET /nodes/{id}/commands?wait=20` —
   an ordinary outbound GET.
2. If nothing is queued, central **holds the request open** for up to that many
   seconds, re-checking the queue (and the settings overlay) about once a
   second. Empty at the deadline → it answers "nothing", and the agent
   immediately sends the next one. The result is a standing "call me when you
   have something" line built entirely of agent-initiated requests — NATs and
   proxies see nothing unusual, and a held connection costs neither side CPU.
3. The moment someone queues a command (Update, Restart, Bench, Decommission)
   or changes the node's settings, central ends the hold and the response
   carries the command and/or the fresh settings overlay. Delivery latency
   drops from "up to one checkin interval" to roughly one round-trip.

Nothing about the queue changes: commands still live in the same table with
the same exactly-once delivery (queued → delivered atomically) — long-polling
only moves *when* the dequeue attempt happens, never the order or semantics.
Heartbeat checkins continue at their normal cadence regardless (liveness,
metrics, status, log shipping); the long-poll just fills the silence between
them. If the long-poll errors (central restarting, network blip), the agent
falls back to a plain sleep until the next heartbeat and tries again — offline
remains a normal state. Older agents ignore `commands_wait` and keep classic
polling; older centrals never advertise it and agents never hold. No flag day.

## Behavior notes

- **Tokens**: the join response's `node_token` (`pna_...`) is stored in `state.json`
  (mode 0600) and sent as `X-Pods-Node-Token`. A 403 on checkin means an admin ran
  `/regenerate` — the agent parks with long backoff until re-joined.
- **Inventory hash-gating**: full inventory is sent only on the first checkin, when the
  hash changes, or when central responds `resync: true`.
- **Config-as-data**: each checkin response republishes central's endpoints; the agent
  adopts `api_base` changes automatically (`PODS_AGENT_ADOPT_ENDPOINTS=false` pins the
  join-time URL for debugging).
- **Host-command confirmation**: the agent NEVER runs host commands (e.g. `tailscale up`)
  silently. `PODS_AGENT_HOST_CMDS=ask` (default) prompts (Y/n) on a terminal and refuses
  when there's no TTY; `always` pre-approves for unattended installs; `never` is a
  hard opt-out. Every skip is logged with the env that controls it.
- **Tailnet**: if join returns a headscale preauth key, a `tailscale` binary exists,
  tailscale is NOT already in use (a host runs one tailscale — an existing tailnet is
  never touched), and you confirm, the agent runs `tailscale up`. In every other
  case it explains why and stays in API-only mode. The dev container image ships
  without tailscale on purpose.
- **Probe scoping**: capability probes are env-gated per check (`PODS_AGENT_CHECK_DOCKER`,
  `PODS_AGENT_CHECK_K8S` — set `false` to disable). Defaults keep it easy (all on); the
  startup log prints the active probe policy so deployers can see exactly what the agent
  will touch.
- **Dev TLS**: `PODS_AGENT_INSECURE=true` skips certificate verification.
