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

# TEST_ABACO_SERVICE_PASSWORD to use throughout. Must be filled for testing (it has ability to create tokens)
export TEST_ABACO_SERVICE_PASS := changeme

# STATIC_NFS_IP to use throughout. Must be filled.
export STATIC_NFS_IP := 10.96.175.175

# DEV_TOOLS bool. Whether or not to start jupyter + mount pods/service folder in pods (main).
# options: "false" | "true"
# default: "false"
export DEV_TOOLS := false



# Got from: https://stackoverflow.com/a/59087509
help:
	@awk ' \
		BEGIN { GREEN = "\033[0;32m"; NC = "\033[0m"; } \
		/^#:/ { desc=$$0; getline; if ($$0 ~ /^[a-zA-Z0-9_-]+:/) { \
			sub(/^#:[ ]*/, "", desc); \
			sub(/:.*/, "", $$0); \
			printf "%s%s%s\t%s\n", GREEN, $$0, NC, desc; \
		}}' $(MAKEFILE_LIST) | column -s $$'\t' -t
# Gets all remote images and starts pods in daemon mode
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


#: Initialize a few templates
init-data:
	@printf "Not yet implemented\n"


# Runs pytest in the pods-api container
#: Run tests in pods-api container
test:
	@printf "Tests are a work in progress\n"
	@printf "Makefile: $(GREEN)test$(NC)\n"
	@printf "  📝  : Running Tests\n"
	@printf "\n"
	kubectl exec -it deploy/pods-api -- pytest tests/*.py --disable-pytest-warnings
	@printf "\n"


# Builds core locally and sets to correct tag. This should take priority over DockerHub images
#: Build core image
build: vars
	@printf "Makefile: $(GREEN)build$(NC)\n"
	@printf "  🔨 : Running image build.\n"
	@printf "  🌎 : Using daemon: $(LCYAN)minikube$(NC)\n"
	@printf "  🏃 : Building: This part takes a while if it takes a while.\n"
	@printf "\n"
	minikube image build -t $(SERVICE_NAME)/pods-api:$$TAG ./
	#minikube image build -t $(SERVICE_NAME)/pods-api-remote:$$TAG -f Dockerfile.remote ./
	@printf "\n"


# Builds core locally with docker Daemon for publish/local-usage
#: Build core image in docker for publishing/develop
build-docker: vars
	@printf "Makefile: $(GREEN)build$(NC)\n"
	@printf "  🔨 : Running image build.\n"
	@printf "  🌎 : Using daemon: $(LCYAN)docker$(NC)\n"
	@printf "\n"
	docker build -t tapis/pods-api:$$TAG ./
	docker build -t tapis/pods-api-remote:$$TAG -f Dockerfile.remote ./
	@printf "\n"


#: Pull core image
pull:
	@printf "Makefile: $(GREEN)pull$(NC)\n"
	@printf "Not yet implemented\n"


# Ends all active k8 containers needed for pods
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
