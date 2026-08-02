#!/usr/bin/env bash
# Verbalized local run of the CI Checks workflow — narrates the same jobs/steps
# as .github/workflows/ci-checks.yml so you can watch the flow in the terminal
# (like reading a GitHub Actions log) without a runner. `make ci` calls this.
#
#   CI_SHOW=1 bash ci/act.sh   # also print each gate's full output (expanded groups)
set -uo pipefail
cd "$(dirname "$0")/.."

G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; DIM='\033[2m'; B='\033[1m'; C='\033[0;36m'; NC='\033[0m'
SHOW="${CI_SHOW:-0}"
now() { date +%s; }
RUN_START=$(now)
FAILED_JOBS=()

rule() { printf "${DIM}────────────────────────────────────────────────────────${NC}\n"; }
header() {
  printf "\n${B}▶ CI Checks${NC} ${DIM}· pods_service · local run (ci/act.sh)${NC}\n"
  printf "${DIM}  mirrors .github/workflows/ci-checks.yml — no runner needed${NC}\n"; rule
}
step()  { printf "    ${G}✓${NC} ${DIM}%s${NC}\n" "$1"; }               # instant/no-op setup step
skip()  { printf "  ${Y}⊘ Job: %s${NC} ${DIM}(skipped — needs %s)${NC}\n" "$1" "$2"; }

# job "<name>" "<needs>" "<setup step labels ; separated>" <gate-script>
job() {
  local name="$1" needs="$2" setups="$3" gate="$4"
  printf "\n  ${B}● Job: %s${NC}" "$name"
  [ -n "$needs" ] && printf " ${DIM}(needs: %s)${NC}" "$needs"
  printf "\n"
  IFS=';' read -ra S <<< "$setups"
  for s in "${S[@]}"; do [ -n "$s" ] && step "$s"; done

  local t0 out rc; t0=$(now)
  printf "    ${C}▶ %s${NC}\n" "$gate"
  out="$(bash "$gate" 2>&1)"; rc=$?
  if [ "$SHOW" = "1" ] || [ $rc -ne 0 ]; then
    printf '%s\n' "$out" | sed 's/^/      │ /'
  fi
  local dt=$(( $(now) - t0 ))
  if [ $rc -eq 0 ]; then
    printf "    ${G}✓ %s passed${NC} ${DIM}(%ss)${NC}\n" "$name" "$dt"
  else
    printf "    ${R}✗ %s FAILED${NC} ${DIM}(%ss)${NC}\n" "$name" "$dt"
    FAILED_JOBS+=("$name")
  fi
  return $rc
}

header

# Job 1 — compile. Everything else "needs" it (fail-fast, like the workflow).
job "compile" "" "Checkout;Set up Python 3.11" ci/compile.sh
COMPILE_RC=$?

if [ $COMPILE_RC -ne 0 ]; then
  skip "unit" "compile"; skip "security" "compile"
else
  job "unit" "compile" "Checkout;Set up Python 3.11" ci/unit.sh || true
  job "security" "compile" "Checkout (full history);Set up Python 3.11;Install scanners" ci/security.sh || true
fi

rule
DT=$(( $(now) - RUN_START ))
if [ ${#FAILED_JOBS[@]} -eq 0 ] && [ $COMPILE_RC -eq 0 ]; then
  printf "  ${G}${B}✓ All jobs passed${NC} ${DIM}(%ss total)${NC}\n\n" "$DT"; exit 0
else
  printf "  ${R}${B}✗ Run failed${NC} ${DIM}(%ss)${NC}" "$DT"
  [ $COMPILE_RC -ne 0 ] && printf " ${DIM}— compile failed, downstream jobs skipped${NC}"
  [ ${#FAILED_JOBS[@]} -gt 0 ] && printf " ${DIM}— failed: %s${NC}" "${FAILED_JOBS[*]}"
  printf "\n\n"; exit 1
fi
