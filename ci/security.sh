#!/usr/bin/env bash
# GATE 3 — deterministic adversarial (source scan, no cluster, no deps beyond the
# scanner CLIs). This is where the audits become permanent guards.
#
# Waterfall inside the gate (all run; any HARD failure fails the gate):
#   3a gitleaks         — secret scan (HARD). Would have caught the committed key.
#   3b semgrep custom   — our audit-derived rules (ci/semgrep/pods.yml).
#                          ERROR rules are HARD; WARNING rules advisory. With
#                          BASELINE set, only NEW findings vs that ref count.
#   3c semgrep packs    — p/python, p/secrets community rules (ADVISORY to start).
#   3d pip-audit        — dependency CVEs (ADVISORY).
#
# BASELINE (optional): export BASELINE=origin/dev to gate on NEW findings only,
# so known-open WARNINGs in PRE_EDGE_SECURITY_AUDIT don't block, but any
# reintroduced/added instance fails.
set -uo pipefail
cd "$(dirname "$0")/.."
source ci/lib.sh
BASELINE="${BASELINE:-}"

section "3a — secret scan (gitleaks)"
if have gitleaks; then
  # TREE scan (--no-git), deliberately not history: old rotated/burned secrets
  # are baked into public git history forever, so a history scan can never go
  # green — this gate guards the checkout instead: no secret may EXIST in the
  # tree. Known gap: a secret committed and removed within a single PR escapes;
  # history hygiene is the roadmap's rotation work, not a per-PR gate.
  # Allowlist for placeholders/fixtures lives in .gitleaks.toml (repo root).
  run "no secrets in tree" gitleaks detect --no-git --no-banner --redact --exit-code 1
else
  note "gitleaks not installed — SKIPPED (install to enable; HARD gate once present)"
fi

section "3b — semgrep: audit-derived rules"
if have semgrep; then
  base_args=()
  [ -n "$BASELINE" ] && base_args=(--baseline-commit "$BASELINE") && note "baseline: $BASELINE (NEW findings only)"
  # --error exits non-zero on ANY finding, so scope the HARD pass to ERROR
  # severity (the eradicated classes — eval, f-string SQL). WARNING rules run
  # separately WITHOUT --error: they surface known-open spread advisory-only
  # (and BASELINE suppresses the existing ones, so only NEW warnings show).
  run "ERROR rules (gate)" semgrep --error --severity ERROR --disable-version-check \
      --config ci/semgrep/pods.yml "${base_args[@]}" .
  note "advisory (WARNING) rules:"
  semgrep --severity WARNING --disable-version-check \
      --config ci/semgrep/pods.yml "${base_args[@]}" . \
      || note "advisory warnings above (not gating)"
else
  note "semgrep not installed — SKIPPED (install to enable)"
fi

section "3c — semgrep community packs (advisory)"
if have semgrep; then
  semgrep --disable-version-check --config p/python --config p/secrets . \
    || note "community pack findings above (advisory — not gating yet)"
else
  note "semgrep not installed — SKIPPED"
fi

section "3d — dependency CVEs (advisory)"
if [ -f requirements.txt ] && have pip-audit; then
  pip-audit -r requirements.txt || note "dependency advisories above (advisory)"
else
  note "pip-audit or requirements.txt absent — SKIPPED"
fi

finish
