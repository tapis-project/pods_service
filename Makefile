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
.PHONY: down clean help

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

# DEV_TOOLS bool. Whether or not to start jupyter + mount pods/service folder in pods (main).
# options: "false" | "true"
# default: "false"
export DEV_TOOLS := false



# Got from: https://stackoverflow.com/a/59087509
help:
	@grep -B1 -E "^[a-zA-Z0-9_-]+\:([^\=]|$$)" Makefile \
	| grep -v -- -- \
	| sed 'N;s/\n/###/' \
	| sed -n 's/^#: \(.*\)###\(.*\):.*/\2###\1/p' \
	| column -t  -s '###'


# Gets all remote images and starts pods in daemon mode
#: Deploy service
up: vars build
	@echo "Makefile: $(GREEN)up$(NC)"
	@echo "  🔍 : Looking to run ./burnup in deployment folder."
	rm -rf deployment; mkdir deployment; cp -r deployment-template/* deployment;
	cd deployment
	@echo "  🔨 : Created deployment folder with templates."
	@sed -i 's/"version".*/"version": "$(TAG)",/g' config.json
	@sed -i 's/MAKEFILE_SERVICE_NAME/$(SERVICE_NAME)/g' *
	@sed -i 's/MAKEFILE_SERVICE_PASS/$(SERVICE_PASS)/g' *
	@sed -i 's/MAKEFILE_TAG/$(TAG)/g' *
	@echo "  🔥 : Running burnup."
ifeq ($(DEV_TOOLS),true)
	@sed -i 's/#DEV//g' *
# Delete #DEV lines when DEV_TOOLS is set to false. Config can break b/c it has to be proper JSON.
else
	@sed -i '/#DEV/d' *
	@echo "  🔗 : Jupyter Lab URL: dev_tools is set to 'false'"
endif
	@echo ""
	./burnup
	echo ""

ifeq ($(DEV_TOOLS),true)
	@echo "  🔗 : Jupyter Lab URL: $(LCYAN)http://$$(minikube ip):$$(kubectl get service pods-api-jupyter | grep -o -P '(?<=8888:).*(?=/TCP)')$(NC)"
else
	@echo "  🔗 : Jupyter Lab URL: dev_tools is set to 'false'"
endif
	@echo "  🔗 : API URL: $(LCYAN)http://$$(minikube ip):$$(kubectl get service pods-traefik | grep -o -P '(?<= 80:)\d+(?=/TCP)')$(NC)/v3"
	@echo "  🔗 : Docs URL: $(LCYAN)http://$$(minikube ip):$$(kubectl get service pods-api-nodeport | grep -o -P '(?<=8000:)\d+(?=/TCP)')$(NC)/docs"
	@echo "  🔗 : Spec URL: $(LCYAN)http://$$(minikube ip):$$(kubectl get service pods-api-nodeport | grep -o -P '(?<=8000:)\d+(?=/TCP)')$(NC)/openapi.json"
	@echo "  🔗 : Traefik Dash URL: $(LCYAN)http://$$(minikube ip):$$(kubectl get service pods-traefik | grep -o -P '(?<=8080:)\d+(?=/TCP)')$(NC)/dashboard"
	@echo ""


# Runs pytest in the pods-api container
#: Run tests in pods-api container
test:
	@echo "Tests are a work in progress"
	@echo "Makefile: $(GREEN)test$(NC)"
	@echo "  📝  : Running Tests"
	@echo ""
	kubectl exec -it deploy/pods-api -- pytest --maxfail 1 tests/* --disable-pytest-warnings
	@echo ""


# Builds core locally and sets to correct tag. This should take priority over DockerHub images
#: Build core image
build: vars
	@echo "Makefile: $(GREEN)build$(NC)"
	@echo "  🔨 : Running image build."
	@echo "  🌎 : Using daemon: $(LCYAN)minikube$(NC)"
	@echo ""
	minikube image build -t $(SERVICE_NAME)/pods-api:$$TAG ./
	@echo ""


# Builds core locally with docker Daemon for publish/local-usage
#: Build core image in docker for publishing/develop
build-docker: vars
	@echo "Makefile: $(GREEN)build$(NC)"
	@echo "  🔨 : Running image build."
	@echo "  🌎 : Using daemon: $(LCYAN)docker$(NC)"
	@echo ""
	docker build -t tapis/pods-api:$$TAG ./
	@echo ""


#: Pull core image
pull:
	@echo "Makefile: $(GREEN)pull$(NC)"
	@echo "Not yet implemented"


# Ends all active k8 containers needed for pods
#: Delete service
down:
	@echo "Makefile: $(GREEN)down$(NC)"
	@echo "  🔍 : Looking to run ./burndown in deployment folder."
	if [ -d "deployment" ]; then
		echo "  🎉 : Found deployment folder. Using burndown."
		cd deployment
		echo "  🔥 : Running burndown."
		echo ""
		./burndown
	else
		echo "  ✔️  : No deployment folder, nothing to burndown."
	fi
	@echo ""


# Cleans directory. Notably deletes the deployment folder if it exists
#: Delete service + folders
clean: down
	@echo "Makefile: $(GREEN)clean$(NC)"
	echo "  🔍 : Looking to delete deployment folder."
	if [ -d "deployment" ]; then
		rm -rf deployment
		echo "  🧹 : Deployment folder deleted."
	else
		echo "  ✔️  : Deployment folder already deleted."
	fi
	@echo ""


# Test setting of environment variables
#: Lists vars
vars:
	@echo "Makefile: $(GREEN)vars$(NC)"	

	echo "  ℹ️  tag:            $(LCYAN)$(TAG)$(NC)"
	echo "  ℹ️  namespace:      $(LCYAN)$(NAMESPACE)$(NC)"
	echo "  ℹ️  service_name:   $(LCYAN)$(SERVICE_NAME)$(NC)"
	echo "  ℹ️  service_pass:   $(LCYAN)$(SERVICE_PASS)$(NC)"

ifeq ($(filter $(DAEMON),minikube docker),)
	echo "  ❌ daemon:         $(RED)DAEMON must be one of ['minikube', 'docker']$(NC)"
	exit 1
else
	echo "  ℹ️  daemon:         $(LCYAN)$(DAEMON)$(NC)"
endif

ifeq ($(filter $(IMG_SOURCE),local remote),)
	echo "  ❌ img_source:      $(RED)IMG_SOURCE must be one of ['local', 'remote']$(NC)"
	exit 1
else
	echo "  ℹ️  img_source:     $(LCYAN)$(IMG_SOURCE)$(NC)"
endif

ifeq ($(filter $(DEV_TOOLS),true false),)
	echo "  ❌ dev_tools:      $(RED)DEV_TOOLS must be one of ['true', 'false']$(NC)"
	exit 1
else
	echo "  ℹ️  dev_tools:      $(LCYAN)$(DEV_TOOLS)$(NC)"
endif

	echo ""
