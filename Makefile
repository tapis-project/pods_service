# Makefile for local development

# Colors in echos: https://stackoverflow.com/questions/5947742/how-to-change-the-output-color-of-echo-in-linux
# Colors
BLACK=\033[0;30m
RED=\033[0;31m
GREEN=\033[0;32m
ORANGE=\033[0;33m
BLUE=\033[0;34m
PURPLE=\033[0;35m
CYAN=\033[0;36m
GRAY=\033[1;30m

# Light colors
WHITE=\033[1;37m
LRED=\033[1;31m
LGREEN=\033[1;32m
YELLOW=\033[1;33m
LBLUE=\033[1;34m
LPURPLE=\033[1;35m
LCYAN=\033[1;36m
LGRAY=\033[0;37m

# No color
NC=\033[0m

.ONESHELL: down
.PHONY: down clean help ci ci-verbose ci-gate ci-llm ci-full review-sweep review-status commit-audit fmt lint test-local

# TAG to use for service image
# options: "dev" | "whatever"
# default: "dev"
export TAG := dev

# DAEMON to use containers. Either minikube daemon or regular local daemon
# options: "minikube" | "docker"
# default: "minikube"
export DAEMON := minikube

# IMG_SOURCE to get images from, either locally built or remotely pulled
# options: "local" | "remote"
# default: "local"
export IMG_SOURCE := local

# NAMESPACE for minikube instance to use.
# options: "default" | "whatever"
# default: "default"
export NAMESPACE := default

# SERVICE_NAME to use throughout. Changes deployment folder. Have to modify here too.
# options: "pods" | "whatever"
# default: "pods"
export SERVICE_NAME := pods

# SERVICE_PASS to use throughout. Must be filled.
export SERVICE_PASS := password

# TEST_ABACO_SERVICE_PASSWORD to use throughout. Must be filled for testing (it has ability to create tokens)
export TEST_ABACO_SERVICE_PASS := changeme

# STATIC_NFS_IP to use throughout. Must be filled.
export STATIC_NFS_IP := 10.96.175.175

# DEV_TOOLS bool. Whether or not to start jupyter + mount pods/service folder in pods (main).
# options: "false" | "true"
# default: "false"
export DEV_TOOLS := false



# Grouped, self-aligning help. Sections are `#@ Name`; entries are `#: desc` on
# the line above a target. Pure awk with %-15s padding — deliberately NOT
# `column -s $'\t'`, which dash turns into a split on the letter 't'.
#@ Help
#: Show this help (grouped command list)
help:
	@awk 'BEGIN{g="\033[0;32m";b="\033[1m";dm="\033[2m";n="\033[0m"} /^#@ /{sub(/^#@ /,"");printf "\n%s%s%s\n",b,$$0,n;next} /^#> /{h=$$0;sub(/^#> /,"",h);printf "    %s%s%s\n",dm,h,n;next} /^#: /{d=$$0;sub(/^#: /,"",d);getline; if($$0 ~ /^[a-zA-Z0-9_%-]+:/){t=$$0;sub(/:.*/,"",t);printf "  %s%-15s%s %s\n",g,t,n,d}}' $(MAKEFILE_LIST)
# Gets all remote images and starts pods in daemon mode
#@ Deploy
#: Deploy service
up: vars build
	@printf "Makefile: $(GREEN)up$(NC)\n"
	@printf "  🔍 : Looking to run ./burnup in deployment folder.\n"
	rm -rf deployment; mkdir deployment; cp -r deploymentTemplate/* deployment;
	cd deployment
	@printf "  🔨 : Created deployment folder with templates.\n"
	@sed -i 's/"version".*/"version": "$(TAG)",/g' config.json
	@sed -i 's/MAKEFILE_SERVICE_NAME/$(SERVICE_NAME)/g' *
	@sed -i 's/MAKEFILE_SERVICE_PASS/$(SERVICE_PASS)/g' *
	@sed -i 's/MAKEFILE_TEST_ABACO_SERVICE_PASS/$(TEST_ABACO_SERVICE_PASS)/g' *
	@sed -i 's/MAKEFILE_STATIC_NFS_IP/$(STATIC_NFS_IP)/g' *
	@sed -i 's/MAKEFILE_TAG/$(TAG)/g' *
	@printf "  🔥 : Running burnup.\n"
ifeq ($(DEV_TOOLS),true)
	@sed -i 's/#DEV//g' *
# Delete #DEV lines when DEV_TOOLS is set to false. Config can break b/c it has to be proper JSON.
else
	@sed -i '/#DEV/d' *
	@printf "  🔗 : Jupyter Lab URL: dev_tools is set to 'false'\n"
endif
	@printf "\n"
	./burnup
	printf "\n"

ifeq ($(DEV_TOOLS),true)
	@printf "  🔗 : Jupyter Lab URL: $(LCYAN)http://$$(minikube ip):$$(kubectl get service pods-api-jupyter | grep -o -P '(?<=8888:).*(?=/TCP)')$(NC)\n"
else
	@printf "  🔗 : Jupyter Lab URL: dev_tools is set to 'false'\n"
endif
	@printf "  🔗 : API URL: $(LCYAN)http://$$(minikube ip):$$(kubectl get service pods-traefik | grep -o -P '(?<= 80:)\d+(?=/TCP)')$(NC)/v3\n"
	@printf "  🔗 : Docs URL: $(LCYAN)http://$$(minikube ip):$$(kubectl get service pods-api | grep -o -P '(?<=8000:)\d+(?=/TCP)')$(NC)/docs\n"
	@printf "  🔗 : Spec URL: $(LCYAN)http://$$(minikube ip):$$(kubectl get service pods-api | grep -o -P '(?<=8000:)\d+(?=/TCP)')$(NC)/openapi.json\n"
	@printf "  🔗 : Traefik Dash URL: $(LCYAN)http://$$(minikube ip):$$(kubectl get service pods-traefik | grep -o -P '(?<=8080:)\d+(?=/TCP)')$(NC)/dashboard\n"
	@printf "\n"
ifeq ($(DEV_TOOLS),true)
# Surface model/schema drift in the terminal. Wait for the new pods-api to be
# ready (its startup runs `alembic upgrade head`, so the DB is at head by then),
# then run the read-only check. Non-fatal: never fails `make up`.
	@printf "  🔍 : Waiting for pods-api to be ready, then checking for migration drift...\n"
	@kubectl rollout status deploy/pods-api --timeout=120s >/dev/null 2>&1 || true
	@$(MAKE) -C $(CURDIR) --no-print-directory check || true
endif


#: Initialize a few templates
init-data:
	@printf "Not yet implemented\n"


# Runs pytest in the pods-api container
#@ Tests  (full suite runs in-cluster — need `make up`)
#: Run ALL tests in the pods-api container — e.g. `make test`
test:
	@printf "Makefile: $(GREEN)test$(NC)\n"
	@printf "  📝  : Running all tests\n"
	@printf "\n"
	kubectl exec -it deploy/pods-api -- pytest tests/*.py --disable-pytest-warnings
	@printf "\n"

# Pattern rule for running specific test files
#: Run ONE test file in-cluster — e.g. `make test-test_agent_watch.py`
test-%:
	@printf "Makefile: $(GREEN)test-$*$(NC)\n"
	@printf "  📝  : Running tests/$*\n"
	@printf "\n"
	kubectl exec -it deploy/pods-api -- pytest tests/$* --disable-pytest-warnings
	@printf "\n"

#: Fast unit tests — cluster-free suite, no `make up` needed (CI gate 2 on its own)
test-local:
	@bash ci/unit.sh

#@ CI — local gates (no cluster, no deps)
#: Run the CI Checks locally, narrated like a GitHub Actions run (no runner/cluster needed)
ci:
	@bash ci/act.sh

#: Same, but also print each gate's full output (expanded step logs)
ci-verbose:
	@CI_SHOW=1 bash ci/act.sh

#: Run a single CI gate — make ci-gate GATE=compile|unit|security
ci-gate:
	@bash ci/$(GATE).sh

#@ LLM review (local, optional — reviewer not shipped in the pushed CI)
#> configure: LLM_BASE_URL=<litellm>/v1  LLM_MODELS=openai/MiniMax-M2.7,qwen3-32b  (tapis_auth pods: LLM_AUTH_HEADER=X-Tapis-Token LLM_AUTH_PREFIX=)
#> scope:     whole branch vs origin/dev (default) · one commit: LLM_DIFF_CMD='jj diff -r <rev> --git' · feature sweep: make review-sweep FEATURE=auth
#> sweep:     whole-file review of a subsystem (features in ci/review/features.txt) · recent-only: LLM_SWEEP_RECENT_DAYS=7
#> status:    make review-status — what's been reviewed, and what's STALE (>LLM_STALE_DAYS old or files changed since)
#> output:    ci/reviews/ (gitignored) — per-model report + LEDGER + COMPARE + REQUEST (sizes/tokens/waterfall) + coverage.json
#: LLM adversarial review — advisory (local-only; needs LLM_BASE_URL, else skips)
ci-llm:
	@test -f ci/llm_review.py && python3 ci/llm_review.py || echo "  · LLM review: ci/llm_review.py not present in this checkout (local-only tool)"

#: LLM review of one feature area — make review-sweep FEATURE=auth (see ci/review/features.txt)
review-sweep:
	@test -f ci/llm_review.py && LLM_SWEEP=$(FEATURE) python3 ci/llm_review.py || echo "  · LLM review: ci/llm_review.py not present (local-only tool)"

#: Review freshness — what's been reviewed and what's stale (no LLM call)
review-status:
	@test -f ci/llm_review.py && LLM_STATUS=1 python3 ci/llm_review.py || echo "  · LLM review: ci/llm_review.py not present (local-only tool)"

#: Pre-push commit audit (order/scoping/tests) → ci/reviews/COMMIT_AUDIT.md (no LLM call)
commit-audit:
	@mkdir -p ci/reviews && test -f ci/review/commit_audit.py && python3 ci/review/commit_audit.py | tee ci/reviews/COMMIT_AUDIT.md || echo "  · commit_audit.py not present (local-only tool)"

#: The 3 deterministic gates THEN the LLM review (advisory 4th step)
ci-full: ci ci-llm

#@ Lint / format (ruff — OFF by default; flip RUFF=1 when the tree is clean)
#> ruff = one binary for format (black-compatible) + lint. Config: ruff.toml. Not yet gating (ci/lint.sh skips unless RUFF=1).
#: Apply ruff formatting to the tree (writes changes)
fmt:
	@command -v ruff >/dev/null 2>&1 && ruff format . || echo "  · ruff not installed (pip install ruff, or nix run nixpkgs#ruff)"

#: Lint + format check (ruff) — runs ci/lint.sh with RUFF=1
lint:
	@RUFF=1 bash ci/lint.sh


# Builds core locally and sets to correct tag. This should take priority over DockerHub images
#@ Build & images
#: Build core image
build: vars
	@printf "Makefile: $(GREEN)build$(NC)\n"
	@printf "  🔨 : Running image build.\n"
	@printf "  🌎 : Using daemon: $(LCYAN)minikube$(NC)\n"
	@printf "  🏃 : Building: This part takes a while if it takes a while.\n"
	@printf "\n"
	minikube image build -t $(SERVICE_NAME)/pods-api:$$TAG ./
	@printf "\n"


# Builds core locally with docker Daemon for publish/local-usage
#: Build core image in docker for publishing/develop
build-docker: vars
	@printf "Makefile: $(GREEN)build$(NC)\n"
	@printf "  🔨 : Running image build.\n"
	@printf "  🌎 : Using daemon: $(LCYAN)docker$(NC)\n"
	@printf "\n"
	docker build -t tapis/pods-api:$$TAG ./
	@printf "\n"


#: Pull core image
pull:
	@printf "Makefile: $(GREEN)pull$(NC)\n"
	@printf "Not yet implemented\n"


# Ends all active k8 containers needed for pods
#@ Teardown
#: Delete service
down:
	@printf "Makefile: $(GREEN)down$(NC)\n"
	@printf "  🔍 : Looking to run ./burndown in deployment folder.\n"
	if [ -d "deployment" ]; then
		printf "  🎉 : Found deployment folder. Using burndown.\n"
		cd deployment
		printf "  🔥 : Running burndown.\n"
		printf "\n"
		./burndown
	else
		printf "  ✔️  : No deployment folder, nothing to burndown.\n"
	fi
	@printf "\n"


# Cleans directory. Notably deletes the deployment folder if it exists
#: Delete service + folders
clean: down
	@printf "Makefile: $(GREEN)clean$(NC)\n"
	printf "  🔍 : Looking to delete deployment folder.\n"
	if [ -d "deployment" ]; then
		rm -rf deployment
		printf "  🧹 : Deployment folder deleted.\n"
	else
		printf "  ✔️  : Deployment folder already deleted.\n"
	fi
	@printf "\n"


# Test setting of environment variables
#@ Info
#: Lists vars
vars:
	@printf "Makefile: $(GREEN)vars$(NC)\n"

	printf "  ℹ️  tag:            $(LCYAN)$(TAG)$(NC)\n"
	printf "  ℹ️  namespace:      $(LCYAN)$(NAMESPACE)$(NC)\n"
	printf "  ℹ️  service_name:   $(LCYAN)$(SERVICE_NAME)$(NC)\n"
	printf "  ℹ️  service_pass:   $(LCYAN)$(SERVICE_PASS)$(NC)\n"

ifeq ($(filter $(DAEMON),minikube docker),)
	printf "  ❌ daemon:         $(RED)DAEMON must be one of ['minikube', 'docker']$(NC)\n"
	exit 1
else
	printf "  ℹ️  daemon:         $(LCYAN)$(DAEMON)$(NC)\n"
endif

ifeq ($(filter $(IMG_SOURCE),local remote),)
	printf "  ❌ img_source:      $(RED)IMG_SOURCE must be one of ['local', 'remote']$(NC)\n"
	exit 1
else
	printf "  ℹ️  img_source:     $(LCYAN)$(IMG_SOURCE)$(NC)\n"
endif

ifeq ($(filter $(DEV_TOOLS),true false),)
	printf "  ❌ dev_tools:      $(RED)DEV_TOOLS must be one of ['true', 'false']$(NC)\n"
	exit 1
else
	printf "  ℹ️  dev_tools:      $(LCYAN)$(DEV_TOOLS)$(NC)\n"
endif

	printf "\n"

ifeq ($(DEV_TOOLS),true)
	@printf "  🔗 : Jupyter Lab URL: $(LCYAN)http://$$(minikube ip):$$(kubectl get service pods-api-jupyter | grep -o -P '(?<=8888:).*(?=/TCP)')$(NC)\n"
else
	@printf "  🔗 : Jupyter Lab URL: dev_tools is set to 'false'\n"
endif
	@printf "  🔗 : API URL: $(LCYAN)http://$$(minikube ip):$$(kubectl get service pods-traefik | grep -o -P '(?<= 80:)\d+(?=/TCP)')$(NC)/v3\n"
	@printf "  🔗 : Docs URL: $(LCYAN)http://$$(minikube ip):$$(kubectl get service pods-api | grep -o -P '(?<=8000:)\d+(?=/TCP)')$(NC)/docs\n"
	@printf "  🔗 : Spec URL: $(LCYAN)http://$$(minikube ip):$$(kubectl get service pods-api | grep -o -P '(?<=8000:)\d+(?=/TCP)')$(NC)/openapi.json\n"
	@printf "  🔗 : Traefik Dash URL: $(LCYAN)http://$$(minikube ip):$$(kubectl get service pods-traefik | grep -o -P '(?<=8080:)\d+(?=/TCP)')$(NC)/dashboard\n"
	@printf "\n"

	printf "\n"


# Directory of tapis-typescript repo relative to this one (override with TAPIS_TS_DIR=...)
export TAPIS_TS_DIR ?= ../tapis-typescript

# alembic/versions is live-mounted when DEV_TOOLS=true; new .py files appear instantly.
#@ Database & spec
#: Apply Alembic migrations (alembic upgrade head) in the pods-api container (needs make up).
migrate:
	@printf "Makefile: $(GREEN)migrate$(NC)\n"
	@printf "  📦 : Running alembic upgrade head in pods-api container.\n"
	kubectl exec deploy/pods-api -- bash -c "cd /home/tapis && alembic upgrade head 2>&1 | grep -E 'Running upgrade|ERROR|already up to date' || true"
	@printf "  ✅ : Migrations complete.\n"
	@printf "\n"

# Read-only: writes no files, makes no schema changes. Runs at the end of make up too.
#: Check whether models have drifted from the DB schema (do you need a new migration?).
check:
	@printf "Makefile: $(GREEN)check$(NC)\n"
	@printf "  🔍 : alembic check in pods-api container.\n"
	kubectl exec deploy/pods-api -- bash -c "cd /home/tapis && alembic check 2>&1 | grep -E 'No new upgrade|New upgrade operations detected' || true"
	@printf "\n"

# Usage: make autorevision msg="add foo column". Review the generated file before committing.
#: Autogenerate a new Alembic revision from model changes (manual dev step).
autorevision:
	@printf "Makefile: $(GREEN)autorevision$(NC)\n"
	@if [ -z "$(msg)" ]; then printf "  ❌ : provide a message, e.g. make autorevision msg=\"add foo column\"\n"; exit 1; fi
	@printf "  📦 : alembic revision --autogenerate -m '$(msg)' in pods-api container.\n"
	kubectl exec deploy/pods-api -- bash -c "cd /home/tapis && alembic revision --autogenerate -m '$(msg)'"
	@printf "  ✅ : Revision generated in alembic/versions/ — review it before committing.\n"
	@printf "\n"


#: Sync OpenAPI spec from running service into tapis-typescript spec.yml. Requires service to be up (make up).
spec:
	@printf "Makefile: $(GREEN)spec$(NC)\n"
	@ABS_TS_DIR=$$(cd $(TAPIS_TS_DIR) && pwd) && \
	DEST="$$ABS_TS_DIR/services/pods/spec.yml" && \
	PODS_PORT=$$(kubectl get service pods-api 2>/dev/null | grep -o -P '(?<=8000:)\d+(?=/TCP)') && \
	PODS_IP=$$(minikube ip) && \
	SPEC_URL="http://$$PODS_IP:$$PODS_PORT/openapi.json" && \
	printf "  📋 : Fetching $$SPEC_URL\n" && \
	curl -sf "$$SPEC_URL" -o /tmp/pods-spec.json && \
	printf "\n  📄 : Destination: $(LCYAN)$$DEST$(NC)\n" && \
	printf "  ❓ : Copy fetched spec to destination? [Y/n] " && \
	read -r confirm && confirm=$${confirm:-Y} && \
	if [ "$$confirm" = "Y" ] || [ "$$confirm" = "y" ]; then \
		kubectl exec -i deploy/pods-api -- python3 -c 'import json,sys,yaml; print(yaml.dump(json.load(sys.stdin), sort_keys=False))' < /tmp/pods-spec.json > "$$DEST" && \
		printf "  ✅ : $$DEST updated\n" && \
		printf "  ℹ️  : Run scripts/dev-pods.sh --build in tapis-ui to rebuild and link\n"; \
	else \
		printf "  ⏭️  : Skipped — spec not updated\n"; \
	fi
