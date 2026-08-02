# pods_service CI gates (`ci/`)

## In plain terms
When you open a PR or push, three quick automated checks run before the code is
allowed in. Think of it as a bouncer with a short checklist:

1. **Does it even compile?** (every Python file is syntax-checked)
2. **Do the fast tests pass?** (only the ones that don't need a running cluster)
3. **Is it safe?** (scan for leaked secrets + the specific mistakes our security
   audits already found — no `eval`, no hand-built SQL, cookies with the right
   flags, etc.)

Checks run cheapest-first and **stop at the first failure** — a typo never waits
on the security scan. Check 3 splits into HARD rules (things we permanently
banned, like `eval` — these block the merge) and ADVISORY rules (known issues
we're still working on — these just print a warning, they don't block). So the
gate turns each past bug into a tripwire: fix it once, and CI catches it forever
if it ever comes back.

The whole thing is plain shell scripts, so it runs the same on GitHub, on Gitea,
or on your own laptop with one command: `bash ci/all.sh`. It's deliberately
*light* for pods_service — the big test suite (which needs a real cluster) is a
separate nightly job, not part of this PR gate.

---

## How it's wired
Thin-workflow / fat-script CI: the workflow only triggers; the real checks live
here as shell so they run identically on GitHub Actions, Gitea `act_runner`, or a
dev shell (`bash ci/all.sh`). Nothing here needs minikube, postgres, rabbit, or
NFS — it is the LIGHT gate for pods_service (the heavy in-cluster suite stays a
nightly/manual job).

## The failing waterfall (`ci/all.sh`)
Cheapest → slowest, fail-fast (stops at the first gate that fails):

| # | Gate | Script | Needs | Gate type |
|---|------|--------|-------|-----------|
| 1 | Syntax sweep | `compile.sh` | python only | HARD |
| 2 | Cluster-free unit tests | `unit.sh` | pytest (auto-installed) | HARD |
| 3 | Deterministic security scan | `security.sh` | gitleaks, semgrep (+ pip-audit) | 3a/3b-ERROR HARD; rest advisory |

**T1 lint/format (`lint.sh`, ruff) — OFF by default.** `ruff` is one binary for
formatting (black-compatible) + linting (config: `ruff.toml`). It is NOT in the
`make ci` waterfall yet: `ci/lint.sh` prints a skip note unless `RUFF=1`. Use
`make fmt` to apply formatting and `make lint` to run the checks. When the tree is
clean, flip `RUFF=1` (and add it to the workflow) to gate — formatting is safe to
hard-gate first since it's mechanical.

Run one gate: `bash ci/compile.sh` · `bash ci/unit.sh` · `bash ci/security.sh`.
Run all (plain): `bash ci/all.sh`.
Run all, **narrated like a GitHub Actions log** (jobs, `needs:`, per-step ✓,
timing): `make ci` (or `make ci-verbose` to also print each gate's full output;
`make ci-gate GATE=security` for one gate). This is `ci/act.sh` — it mirrors the
jobs in `.github/workflows/ci-checks.yml` so you can watch the flow with no
runner and no cluster.

### Gate 1 — compile.sh
`py_compile` every tracked `.py` (no imports, no deps). Catches syntax errors
across the whole tree in seconds. Does NOT catch import-time errors (needs deps).

### Gate 2 — unit.sh
Runs ONLY the stdlib-only suites whose imports were verified to load with no
Tapis config: `test_node_telemetry_utils` (the parser has zero service imports)
and the whole `test_agent_*` set (`agent/` is stdlib-only). Everything else
imports `conf`/models and needs the image or a cluster — see "deeper gate".

### Gate 3 — security.sh
3a gitleaks (secret scan, HARD) · 3b our audit-derived semgrep rules
(`semgrep/pods.yml`; ERROR rules HARD, WARNING advisory) · 3c community packs
(advisory) · 3d pip-audit (advisory). Export `BASELINE=origin/dev` to gate on
NEW findings only, so known-open WARNINGs don't block but any reintroduced or
added instance fails.

## Deeper gate (optional, later) — run pure suites in the image
The config-needing pure suites (containment, layering, sparse mounts,
stack-template utils) can't import bare but need no cluster — run them inside the
freshly-built image:
```bash
docker run --rm tapis/pods-api:<tag> pytest tests/test_volume_path_containment.py \
  tests/test_sparse_volume_mounts.py -q
```
Add as a job that depends on the existing image build.

## Tools (install on the runner; pin versions)
`gitleaks`, `semgrep`, `pip-audit`. Absent tools SKIP (visible), so the gate
degrades gracefully until the runner has them.

## Portability
No GitHub/Gitea specifics in these scripts. The workflow (`.github/workflows/
ci-checks.yml`, also read by Gitea) is the only platform-coupled file, and it is
thin. `nektos/act` runs it locally; `bash ci/all.sh` needs no runner at all.
