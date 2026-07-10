# Change Log for the Tapis Pods Service

All notable changes to this project will be documented in this file.

Please find documentation here:
https://tapis.readthedocs.io/en/latest/index.html

You may also reference live-docs based on the OpenAPI v3 specification here:
https://tapis-project.github.io/live-docs


## 26Q3.0-alpha
> Not released yet. Entries are added as features land; the `-alpha` suffix comes
> off when the release is tagged.

The edge release. Pods learned to leave the cluster.

### New features:
- **MCP server for the Pods API** — an agent-facing tool surface generated from the OpenAPI spec, so an LLM can drive pods with typed tools instead of guessing at HTTP.
- Template tag descriptions raised from 400 to 20,000 characters — long-form usage guides live with the tag.
- MCP durable audit log with `/audit/history`: every tool call recorded with caller attribution, a heatmap of activity, call detail, and read-only replay of full untrimmed results.
- MCP full create/read/update tool surface (exec, file readers, secret use, all-resource CRUD). Deletes stay gated behind a flag.
- The no-ADMIN invariant accepts APPROVEDADMIN, so a sole owner can self-promote for private image pulls.
- **Pod access gate** — share a pod with someone who has no Tapis account: shared passwords (bcrypt) or high-entropy links, secrets shown exactly once, secure httponly cookies, and per-IP rate limiting on redemption.
- MCP tool spans export to Phoenix over OTLP, so agent activity is traceable alongside everything else.
- Fleet metrics endpoints with cached PromQL and an availability stanza.
- `scripts/smoke.sh` — one command that tells you which layer of a deployment is broken (nginx, api, traefik-config, or client) from the shape of the response.
- bruno: stacks and stack-template-tag requests, plus example template-tag definitions (codeserver, gatus monitoring suites, onlyoffice, peekaping, phoenix, uptimekuma) to crib from.
- flexserv pod template — transformers CPU variant with a gateway and backend.
- **Edge nodes** begin: the cluster registry becomes a node registry, where one entry maps to one machine you own.
- Node agent contract: claim-token join, checkin, a commands channel, and node-scoped agent tokens. The contract is HTTP-only — an edge box never needs Postgres or RabbitMQ.
- **pods-agent v1** — a single-file, zero-dependency Python agent: join, heartbeat, and Docker inventory over the local socket. It always dials out, so it works from behind any NAT with no inbound ports.
- Dev: `PODS_UVICORN_RELOAD` makes service code changes go live without a pod restart.
- **Publish a node port** at `<route>.pods.<domain>` through central Traefik, with the full per-route Tapis auth stack (allowed users/groups, response headers, excluded paths). A central-side probe tests the backend both directly and through Traefik, since browsers can't resolve route hostnames yet.
- Agents on a Kubernetes box report in-cluster inventory, gated behind an RBAC self-probe so a permission-less agent degrades quietly instead of erroring.
- `${stack:...}` references work inside mounted config files, not just environment variables — so a stack member's config can point at another member by name. Saving a stack as a template un-bakes those hostnames back into references.
- **Node telemetry** — the agent ships container logs and CPU/memory metrics with an offline buffer, so a box that loses network backfills its history on reconnect instead of leaving a hole. Metrics graphs omit empty buckets rather than interpolating, so an outage stays visible.
- Command dispatcher (exactly-once delivery over the agent's existing poll) and an edge benchmark suite: compression matrix, wire-path probes, latency legs, Docker socket cost, and clock skew. Every cold start also self-documents through startup milestones.
- **Agent settings channel** — cadence, log filters, and container globs are edited centrally and adopted by the agent within one heartbeat, no redeploy. The box can pin any setting with an env var and wins. Every change and every adoption lands in a per-node audit ledger.
- **Storage watch** — point a node at `/scratch` with a `90%` or `200G` threshold and get ledger warnings with hysteresis, plus disk usage graphed against the real filesystem size. Scans are I/O-budgeted so watching never competes with real work.
- **Remote agent lifecycle** — restart and self-update from the UI. Updates are sha256-verified against a hash the agent fetches separately, install atomically with a fallback copy, and survive container restarts. A crash-loop guard abandons a bad update after three failed handoffs, so a broken update costs a few restarts, never the node.
- **Polite decommission** — deleting a node asks the agent to remove itself first (wipe its token and state, remove its own container where it can) and reports honestly what it could and couldn't clean up. Force delete stays one click away, and an offline agent parks itself on its next checkin.
- **Long-poll command delivery** — the agent holds a request open between heartbeats, so a restart you click lands in under a second, and settings adopt just as fast. Idle cost is near zero, and older agents keep working unchanged.
- **Node shell** — one-shot commands with recorded exit code and output, auditable after the fact. Deliberately not a PTY, and enabled only by an environment variable on the box itself: central cannot turn on arbitrary code execution remotely.
- **No-downtime token rotation** — central mints a new token, both work during the handshake, and the old one is revoked only once the agent proves it persisted the new one by using it.
- Agents can share their dialable addresses (tailnet and LAN) so publishing a port offers one-click suggestions instead of a hand-typed host. Off by default — addresses are new disclosure, so an operator or node admin opts in.
- Node security hardening: publishing rejects private, loopback, link-local, and cloud-metadata addresses for non-admins and re-checks at dial time; agent-supplied checkin blobs are size-capped; the action ledger is ring-capped.
- `docs/node_security_model.md` — the endpoint access matrix, the two-boundary credential model, what a stolen credential grants, and the bounds on what central can do to a well-behaved edge.
- **CI gate** — a fail-fast waterfall (syntax sweep, cluster-free tests, secret scan plus audit-derived rules) that runs identically on GitHub Actions, Gitea, or a laptop. Every security fix above became a permanent rule, so the class is caught forever.

### Bug fixes:
- Permission guards return proper 4xx codes instead of bare errors that mapped to 500.
- Client errors return proper 4xx instead of 500 across pod, volume, snapshot, and template guards — the error handler only mapped Tapis errors, so bare raises leaked as server errors.
- The Traefik template can no longer render a bare `tcp.middlewares:` — the resulting YAML null made Traefik reject the entire dynamic config, 404ing every route on the site.
- An API client whose token fails validation gets an explicit 403 instead of the browser OAuth redirect; only browser navigations still get bounced to re-login.
- `config_content` writes honor `sub_path` — the file used to be seeded at the volume root while the pod mounted only the subdirectory, so the config never appeared.
- Adopted pods are never claimed by the stack member matcher, `depends_on` works in both directions with adopted pods, and leaving a stack prunes dangling dependency references (a missing dependency hard-blocks the dependent's next start).
- Action-log validators seed only when the log is empty — with assignment validation on, the creation entry was silently clobbering appended entries.
- **Removed `eval()` from the database layer.** The shared query builder spliced caller values into an eval'd string, and one path reached it straight from a URL parameter — an authenticated remote code execution. Replaced with SQLAlchemy column operators, which parameterize properly. Regression-tested with breakout payloads.
- Image allowlist writes require admin mode — previously any authenticated user could allowlist an arbitrary container image or delete one other tenants depended on.
- Traefik config renders are validated before being pushed and fail closed, keeping the last known-good config. A stray quote in one pod's networking field could previously 404 every route on the site.
- Blocked `sub_path` traversal in volume config writes, with both a field validator and a containment guard at the write sink. `service/` now contains zero `eval()` calls.
- RabbitMQ init declares fail loudly instead of silently. A failed user, vhost, or permission create used to log "init complete" and surface much later as an opaque AMQP auth error; it now names the object that failed, with passwords scrubbed from the message.


## 26Q2.1

### New features:
- **Stacks** — compose-style multi-pod deployments as one resource: ordered startup, permission inheritance, and stack templates (save-as and create-from). Members share secrets through a `secret_map` that is checked at write time, so a stack can never reference someone else's secret.
- Stack update endpoint. Destructive from-template updates (member delete/recreate) require stack ADMIN, matching delete.
- Template tags carry a description, and `pods:default:` placeholders are always optional (an empty default resolves to an empty string).
- Per-pod TLS certificate provisioning, with cert state visible in the health loop.
- Status-aware landing pages: a pod that is starting, stopped, errored, finished, or missing says so in the browser instead of failing opaquely.
- Pod layering: reset a single field back to its template value, see provenance and overrides, sparse `volume_mounts`, and a cheap `derived_lite` read for merged display values.
- `docs/traefik_routing.md` documents the routing/splash state model and the rule that certificate state never gates routing.

### Bug fixes:
- Restored tables dropped by a bad migration; migrations now run and verify on startup.
- `PODS_UVICORN_WORKERS` defaults to 1 so only one background loop runs.
- Unexpected 500s log the full server traceback instead of a bare message.
- Startup migrations are serialized with a Postgres advisory lock, so concurrent replicas can't race each other.
- Template tag display is kind-aware — a stack tag no longer shows an empty pod definition.


## 26Q2.0

### New features:
- Traffic observability: Traefik JSON access logs ingested per pod. Tokens, cookies, and sensitive query-param values are redacted at the source, before anything is stored.
- Pod log runs are persisted and downloadable — each start/stop cycle is archived with its own metadata instead of being lost on restart.
- A bruno API request collection now lives in the repo (Admin/Pods/Volumes/Snapshots/Templates + per-tenant environments), so the API is explorable without hand-writing curl.
- Admin endpoints: health, metrics, and debug-traffic — the operator view of what the service is actually doing.
- Template gallery: templates can carry photos and a note (5MB per image, admin-gated writes).
- Volume and snapshot usage reporting, plus pod events and per-pod metrics routes.
- Volume/snapshot listing accepts `?path=` to list a subdirectory instead of the whole root.
- Bring your own domain: pods can be served at a custom domain with DNS verification and an extended Traefik route.
- Exec audit log records the executable, the result, and the duration of every command run in a pod.
- Pod updates write a leaf-diff action log — the audit trail shows which individual fields changed, not just that an update happened.
- Kubernetes healthchecks on pods (`HealthcheckProbe`/`PodHealthchecks`): probes translate to k8s liveness/readiness, and networking can wait for readiness before routing traffic.
- Auth: short secret-ref expansion on update, usernames may start with an underscore, and `local_admin_usernames` for local development.

### Bug fixes:
- Timestamps are stamped by write-kind, so `updated` no longer moves on reads and internal writes.
- Safer schema/query defaults; the runtime search_path is the tenant schema only, matching what migrations set.
- Traffic router-regex now handles the `@entrypoint` suffix, and legacy `pod_id@…` router names still match.
- `SetPermission` re-enforces the tenant-level guard — `tenant.*` and `**` grants are READ-only.


## 26Q3.0-alpha
> Not released yet. Entries are added as features land; the `-alpha` suffix comes
> off when the release is tagged.

The edge release. Pods learned to leave the cluster.

### New features:
- **MCP server for the Pods API** — an agent-facing tool surface generated from the OpenAPI spec, so an LLM can drive pods with typed tools instead of guessing at HTTP.
- Template tag descriptions raised from 400 to 20,000 characters — long-form usage guides live with the tag.
- MCP durable audit log with `/audit/history`: every tool call recorded with caller attribution, a heatmap of activity, call detail, and read-only replay of full untrimmed results.
- MCP full create/read/update tool surface (exec, file readers, secret use, all-resource CRUD). Deletes stay gated behind a flag.
- The no-ADMIN invariant accepts APPROVEDADMIN, so a sole owner can self-promote for private image pulls.
- **Pod access gate** — share a pod with someone who has no Tapis account: shared passwords (bcrypt) or high-entropy links, secrets shown exactly once, secure httponly cookies, and per-IP rate limiting on redemption.

### Bug fixes:
- Permission guards return proper 4xx codes instead of bare errors that mapped to 500.


## 26Q2.1

### New features:
- **Stacks** — compose-style multi-pod deployments as one resource: ordered startup, permission inheritance, and stack templates (save-as and create-from). Members share secrets through a `secret_map` that is checked at write time, so a stack can never reference someone else's secret.
- Stack update endpoint. Destructive from-template updates (member delete/recreate) require stack ADMIN, matching delete.
- Template tags carry a description, and `pods:default:` placeholders are always optional (an empty default resolves to an empty string).
- Per-pod TLS certificate provisioning, with cert state visible in the health loop.
- Status-aware landing pages: a pod that is starting, stopped, errored, finished, or missing says so in the browser instead of failing opaquely.
- Pod layering: reset a single field back to its template value, see provenance and overrides, sparse `volume_mounts`, and a cheap `derived_lite` read for merged display values.
- `docs/traefik_routing.md` documents the routing/splash state model and the rule that certificate state never gates routing.

### Bug fixes:
- Restored tables dropped by a bad migration; migrations now run and verify on startup.
- `PODS_UVICORN_WORKERS` defaults to 1 so only one background loop runs.
- Unexpected 500s log the full server traceback instead of a bare message.
- Startup migrations are serialized with a Postgres advisory lock, so concurrent replicas can't race each other.
- Template tag display is kind-aware — a stack tag no longer shows an empty pod definition.


## 26Q2.0

### New features:
- Traffic observability: Traefik JSON access logs ingested per pod. Tokens, cookies, and sensitive query-param values are redacted at the source, before anything is stored.
- Pod log runs are persisted and downloadable — each start/stop cycle is archived with its own metadata instead of being lost on restart.
- A bruno API request collection now lives in the repo (Admin/Pods/Volumes/Snapshots/Templates + per-tenant environments), so the API is explorable without hand-writing curl.
- Admin endpoints: health, metrics, and debug-traffic — the operator view of what the service is actually doing.
- Template gallery: templates can carry photos and a note (5MB per image, admin-gated writes).
- Volume and snapshot usage reporting, plus pod events and per-pod metrics routes.
- Volume/snapshot listing accepts `?path=` to list a subdirectory instead of the whole root.
- Bring your own domain: pods can be served at a custom domain with DNS verification and an extended Traefik route.
- Exec audit log records the executable, the result, and the duration of every command run in a pod.
- Pod updates write a leaf-diff action log — the audit trail shows which individual fields changed, not just that an update happened.
- Kubernetes healthchecks on pods (`HealthcheckProbe`/`PodHealthchecks`): probes translate to k8s liveness/readiness, and networking can wait for readiness before routing traffic.
- Auth: short secret-ref expansion on update, usernames may start with an underscore, and `local_admin_usernames` for local development.

### Bug fixes:
- Timestamps are stamped by write-kind, so `updated` no longer moves on reads and internal writes.
- Safer schema/query defaults; the runtime search_path is the tenant schema only, matching what migrations set.
- Traffic router-regex now handles the `@entrypoint` suffix, and legacy `pod_id@…` router names still match.
- `SetPermission` re-enforces the tenant-level guard — `tenant.*` and `**` grants are READ-only.


## 26Q1.0
### Breaking Changes:
- Volume Mount nomenclature has changed.
  

### New features:
- Added secret implementation to access Tapis secrets and have template/pod placeholders be somewhat easily specified
- Placeholders are resolved at runtime allowing for some additional flows
- Beginnings of better direct pod download/upload endpoints, still issues depending on if cli tools are available or not
- Adding more CORS options into networking
- Better public templates
- Migrating to a public tenant templates/images scheme
- Advanced secret resolution
- Improvements to dependency checking for templates and tag via materialized view, visible for admins
- Default compression + static path exclusion for tapis auth to speed things up and allow for apps to route around tapis_auth for backend files which can queue up over time due to tapis_auth (should be sped up)
- tapis_auth allow AUTHORIZED_USERS permission
- tapis_auth allow sign-in from other tenants for cases like TapisUI
- Reworked g.admin with a secondary g.admin_active for all endpoints (TUI work as well)
- Substantial speedup to Dockerfile

### Bug fixes:
- Fixes for many volume paths to one pod


## 25Q4.0
### Breaking Changes:
- No change.
  
### Bug fixes:
- Fixes for throughput maximization
- Added additional updatable fields


## 1.9.0 - 2025-07-07:

### Breaking Changes:
- No change.
  
### New features:
- Added CORs options
- Added ensure endpoints (only for jupyter so far)
- Updated oidc/auth features
- Added flake.nix for use with nix develop
- Updated some images
- Updated models
- Added compatibility with timescaledb
- Exec endpoints
- Template `save_pod_as_template` endpoint
- Added bulk image upload
- Added materialized views for template/tag metrics
- Now using pika for rabbit
- Updated to SQLModel, FastAPI, SqlAlchemy, Alembic to latest

### Bug fixes:
- Lots.


## 1.8.0 - 2024-12-04:

### Breaking Changes:
- No change.
  
### New features:
- No change.

### Bug fixes:
- No change.


## 1.7.0 - 2024-09-13:

### Breaking Changes:
- Large DB model changes. Migrations should automate changes, but be warned.
  
### New features:
- Added Pod Templates and Template Tags to define sharable Pod templates.
- Added Image endpoints.
- Added Volume & Snapshot download endpoints.
- Added compute_queues, configurable with kubernetes flag. Allowing multi-GPU configuration.
- Added initial workings for tapis-auth option for Pods to use Tapis auth. Will be fully implemented after client changes.
- PVC pod volume option now exists permanently. Non-sharable, but can be useful.
- Revamped auth logic for organizational purposes.
- Changed CORS for tapis-ui integration.
- Lots of changes for TapisUI.
- Added auto saving openapi.json, removing manual step of copy/paste.
- Updating openapi.json.
- Added dev_tools useful links to `make vars`.

### Bug fixes:
- Multi-slash object routing is now much better.


## 1.6.0 - 2024-02-05

### Breaking Changes:
- No change.
  
### New features:
- Added local_only protocol in pods networking.
- GPU support within resources attr.

### Bug fixes:
- Pinned templated Postgres version.
- Harden t init for startup.


## 1.5.3 - 2023-12-01
- `1.5.1` and `1.5.2`: No Changes. Jumping to `1.5.3` to match deployer version.

### Breaking Changes:
- Implemented direct access to NFS server instead of routing through Files for volumes/snapshots.
    - This solves occassional networking hiccups causing troubles when Files couldn't be accessed.
    - This solves deployment across multiple namespaces as Files access was a stickler.
- Health is now split into health and health-central.
    - `health-central` deploys with the main stack.
        - It takes care of metrics, traefik management, and NFS management.
    - `health` deploys with computer (health and spawner) in whatever namespace.
        - Takes care of Kubernetes health and management in a particular namespace.
- New deployment files for the above features along with deleting no longer used files.
    - Works locally as well.
- Fix in traefik to properly throw a 500 so proxy backup in nginx works properly when no location is matched.
  
### New features:
- Added health deployment that doesn't restart, allowing for easier debugging.
- Improvements to NFS permissions

### Bug fixes:
- Improvements for health regarding processes when new tenants are created while already running.


## 1.5.0 - 2023-10-24

### Breaking Changes:
- No change.
  
### New features:
- `action_logs` added to pod object along with logs endpoint for detailed audit of actions done on pod_id.

### Bug fixes:
- Fixed some user nested validation errors not showing proper error messages.
- Ensure pods always save logs
- Fixed migrations for action_logs and how it works if logs are empty.
- Better normalized paths.


## 1.4.0 - 2023-07-06

### Breaking Changes:
- No change.
  
### New features:
- Better certs.

### Bug fixes:
- No change.


## 1.3.2 - 2023-06-30

### Breaking Changes:
- No change.
  
### New features:
- Traefik proxying now automatically creates certificates at runtime for each subdomain (meaning each pod).
- Service no longer requires initial, or any, manual certificate creation.
- Some edits for Neo4j as it requires a injected cert.
- Changes for local dev as it's now different from deployment.

### Bug fixes:
- No change.


## 1.3.1 - 2023-06-06

### Breaking Changes:
- Changed image declarations from `custom-myuser/myimage` to `myuser/myimage`.
- `neo4j` and `postgres` templates are now under `template/neo4j` and `template/postgres`.
- Status changes: `RUNNING` -> `AVAILABLE`, `SHUTTING_DOWN` -> `DELETING`, `CREATING_CONTAINER (and volume)` -> `CREATING` 

### New features:
- Added volume and snapshot support/utils/models/etc. Using nfs pvc storage to volume mount block storage to running pods.
    - Users can share volumes and collaborate live on the same storage. Snapshots allow users to take copies of volumes for data versioning purposes.
- Automatic creation of nfs backend with Files along with secure PKI access throughout.
- New model schema to reduce replicated code and have a consolidated method to update models.
- Rewrote nfs health code to reduce number of calls to Files from each tenant + volume to once per health check run.
- Added siteadmintable to allow for site wide database configs.
- Added database allowlist
- Added `develop_mode` config to easily turn nfs/other features off and on.

### Bug fixes:
- Changed how datetime is setup to validate OpenAPI spec properly.
- Changed namespace code which was causing breaks.
- Added testing for new features and improved testing for old.
- Additional changes for Postgres
- Reworked auth slightly to make it simpler


## 1.3.0 - 2023-03-09

### Breaking Changes:
- Yes.

### New features:
- Since last changelog, now using Traefik as backend, Postgres workaround, Graphdb workarounds as well.

### Bug fixes:
- Yes.


## 0.30.4 - 2022-06-09

### Breaking Changes:
- Yes.

### New features:
- Fixed nginx routing and resolver. 
- Pods are now routed based on pods.url attr. mypod.pods.tacc.develop.tapis.io is the uri format.

### Bug fixes:
- Yes.


## 0.30.3 - 2022-06-02

### Breaking Changes:
- Yes.

### New features:
- Added in proper health and spawner pods so users don't have to manually start the scripts.
- Added check in health.py to wait for database connection instead of failing.

### Bug fixes:
- Yes.


## 0.30.2 - 2022-06-02

### Breaking Changes:
- Yes.

### New features:
- Now includes pre-baked certs with the option to create certs at run time if you do have cert-manager (requires code edits. Ask Christian.)

### Bug fixes:
- Yes.


## 0.30.1 - 2022-06-02

### Breaking Changes:
- Yes.

### New features:
- Now including certs in Neo4j pods so users can make encrypted calls and we can intercept subdomain.
- Changed pods-main to pods-api. No concept of "main" anymore. Just api components.
- Improved serviceaccount. No longer cluster level, only namespace level, easier to manage this way.
- Updated logic for updating configmap. Now checks if old configmap == new. If not update. Tested, no issue in updating configmap hundreds of time per second.
- Init container support is now working.
- Neo4J now has example code for setting volume mounts in kubernetes_templates.

### Bug fixes:
- Yes.


## 0.30.0 - 2022-05-24

### Breaking Changes:
- Yes.

### New features:
- Optimized TapisModel model further. Simplified store access. Simplified running sqlalchemy commands. SqlAlchemy 2.0 compliant.
- Service is now called pods, changed everywhere.
- Added custom pod_template image configuration alongside database.
- Pods can now be dynamically exposed via Nginx through HTTP or TCP.
- Nginx hot reload implemented through health.py and nginx pod livenessProbe.
- Multiple slashes in URL path are now simplified by redirect middleware.
- Added permissions on object along with permission checking in authorization.
- Revealing /docs/redoc/openapi.json now.
- Added new model functions (display, get_permissions, etc.)
- Added set/delete permission functions that ensure there is always one ADMIN per pod.
- Added more configuration for cpu/mem limit and request.
- Better error handling for pods. Some status messages. More in progress.
- pods/{pod_id}/logs endpoint added.
- pods/{pod_id}/permissions endpoint added.
- Added in TapisMiddleware 2.0.
- Now based off PyPi derived tapipy and tapisservice rather than Flaskbase. Allows for Python:3.10
- Support now for more than just Neo4j, also custom images.

### Bug fixes:
- Yes.


## 0.0.3 - 2022-05-03

### Breaking Changes:
- Yes.

### New features:
- Auth!
- Updated flaskbase-plugins to flaskbase-fastapi. This uses a new TapisMiddleware that deals with authn/authz.
- Removed duplicate req_utils stuff. Only have error handler locally now, all other utils from flaskbase (including global g)

### Bug fixes:
- Yes.


## 0.0.2 - 2022-05-02

### Breaking Changes:
- Yes.

### New features:
- Now using SQLModel with SqlAlchemy ORM doing database calls rather than custom made things.
- SQLModel has validation along with helper functions along with attrs for tenant/site schema/db selection.
- Basic CRUD ops for TapisModel.
- Using Alembic for database migrations. Will configure initial databases as well, over all tenants and sites for the base_url.
- Spawner working and creating Neo4j pods. No user facing auth yet. Passwords model created to store that stuff. Needs encryption.
- Health script takes care of checking on all pods in Kubernetes we need to manage. Updates pod statuses, removes stuff.
- kubernetes_utils greatly improved. Much more readable, easy functionality.
- kubernetes_utils added service creation/delete functions.
- Added Neo4J creation function to specify exactly what we need + create service for Neo4J database_type.

### Bug fixes:
- Yes.

## 0.0.1 - 2022-04-08

### Breaking Changes:
- Init. Nothing to break.

### New features:
- Rabbitmq initialization working
- Channels are added and simplified from Abaco implementation
- TapisMiddleware is declared
- FastAPI Global middleware for flask-like g object.
- Error handling with FastAPI. Ok and Error messages routed properly.
- DEV_TOOLS flag in Makefile for volume mount, and auto jupyter lab creation + port reveal.
- Makefile for minikube deployment/build/cleaning.

### Bug fixes:
- Init. Nothing to fix.
