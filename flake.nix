{
  description = "Tapis documentation";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        ### For rootless podman user must set subuid/subgid
        ## echo "username:100000:65536">>/etc/subuid
        ## echo "username:100000:65536">>/etc/subgid
        ### Rootless podman does not work though as we need root for bind mount
        ### Leaving rootful for now
        commonPackages = [
          ### we must minikube mount as root, so must be installed by host OS.
          #pkgs.podman
          #pkgs.docker
          #pkgs.k3s
          pkgs.kubectl
          pkgs.gnugrep
          pkgs.gnumake
          pkgs.minikube
          pkgs.toybox # pgrep
        ] ++ pkgs.lib.optional pkgs.stdenv.isDarwin pkgs.darwin.cctools; # Darwin needs to be tested and made to work.
        pythonPackages = with pkgs; [
          ## These are currently only for debugging and env, haven't tested full deployments
          ## This should probably refer to requirements somehow instead.
          python310
          #python310Packages.pip###Doesn't seem to work
          #python310Packages.virtualenv
          #python310Packages.requests
          #python310Packages.sqlalchemy
        ];

        # Using builtins.replaceStrings to convert newlines to their escaped form for the shell
        helpText = ''Useful to have help text to refer to in nix. I just use scripts now, but this example stays.'';
        tapisuiHelpMsg = builtins.replaceStrings ["\n"] ["\\n"] helpText;

        # Create a welcome script package with an optional version parameter
        tapisWelcome = pkgs.writeScriptBin "welcome" ''
          #!${pkgs.bash}/bin/bash
          echo -e "Entering Tapis Pods development environment..."

          # if input $1 is version or --version, show relevant pkg versions
          if [[ "$1" == "version" || "$1" == "--version" ]]; then
            PYTHON_VERSION=$(${pkgs.python3}/bin/python --version 2>&1 | awk '{print $2}')
            MINIKUBE_VERSION=$(minikube version --short) # from user env, not nix
            DOCKER_VERSION=$(docker --version | awk '{print $3}' | sed 's/,//') # from user env, not nix
            echo -e "python: $PYTHON_VERSION"
            echo -e "minikube: $MINIKUBE_VERSION"
            echo -e "docker: $DOCKER_VERSION"
          fi

          echo -e "\nAvailable make commands:
          ==========================
            - make help: default make target; shows all available make commands
            - make up: Deploy Tapis Pods service with kubectl
            - make down: Teardown Tapis Pods deployment
            - make vars: Shows input vars, users must set passwords according to docs
          
          Common commands:
          ==========================
            - welcome: callable from nix shell, shows this help message
            - welcome --version: shows npm and node version + welcome
            - nix develop -i: --ignore-environment to isolate nix shell from user env
            - nix develop .#welcome: runs welcome version in nix shell
          "
        '';
      in {
        devShells = {
          default = pkgs.mkShell {
            packages = commonPackages ++ [ tapisWelcome ];
            shellHook = ''
              alias k=kubectl
              welcome
              export STORAGE_DRIVER=vfs
              export MINIKUBE_DRIVER=docker
              export MINIKUBE_ROOTLESS=false
              export MINIKUBE_WANTUPDATENOTIFICATION=false
              export MINIKUBE_PROFILE=podsflake
              kubectl config use-context podsflake
              echo "Using minikube profile: $MINIKUBE_PROFILE"
              # Ensure the podsflake profile exists and is started with podman
              if ! minikube profile list | grep -q "$MINIKUBE_PROFILE"; then
                echo "Creating minikube profile $MINIKUBE_PROFILE..."
                minikube start --profile="$MINIKUBE_PROFILE"
              else
                # Start the profile if not running
                if ! minikube status --profile="$MINIKUBE_PROFILE" | grep -q "host: Running"; then
                  echo "Starting minikube profile $MINIKUBE_PROFILE..."
                  minikube start --profile="$MINIKUBE_PROFILE"
                fi
              fi
              echo "Starting minikube features..."
              # Start minikube dashboard and mount as background processes if not already running
              if ! pgrep -f "minikube dashboard" >/dev/null; then
                nohup minikube dashboard --profile="$MINIKUBE_PROFILE" --url > .minikube-dashboard.log 2>&1 &
                echo "Started minikube dashboard - see .minikube-dashboard.log"
              else
                echo "minikube dashboard already running - see .minikube-dashboard.log"
              fi
              if ! pgrep -f "minikube mount --uid=4872 --gid=4872 --msize=2048576000 --port 22423 /home/cgarcia/Code/pods_service:/pods_service" >/dev/null; then
               nohup minikube mount --profile="$MINIKUBE_PROFILE" --uid=4872 --gid=4872 --msize=2048576000 --port 22423 /home/cgarcia/Code/pods_service:/pods_service > .minikube-mount.log 2>&1 &
               echo "Started minikube mount - see .minikube-mount.log"
              else
                echo "minikube mount already running: /home/cgarcia/Code/pods_service:/pods_service - see .minikube-mount.log"
              fi
              echo ""
              echo "minikube profile list:"
              minikube profile list
              echo ""
            '';
          };
          python = pkgs.mkShell {
            packages = commonPackages ++ pythonPackages;
            shellHook = ''
              echo -e "Entering Tapis Pods (+Python) development environment...
              Python version: $(${pkgs.python310}/bin/python --version 2>&1 | awk '{print $2}')
              Use 'make help' to see available make commands.
              This hasn't been tested yet, it's essentially just python, no packages, permissions might be crazy, stick to k8 + live reload for full dev"
            '';
          };
          help = pkgs.mkShell {
            packages = commonPackages;
            shellHook = ''
              echo "Tapis Pods make commands:"
              echo "========================="
              make help
            '';
          };
          # k3 = pkgs.mkShell {
          #   packages = commonPackages;
          #   shellHook = ''
          #     alias k=kubectl

          #     # Start k3s server if not running
          #     if ! grep -f "k3s server" >/dev/null; then
          #       echo "Starting k3s server..."
          #       nohup k3s server > .k3s-server.log 2>&1 &
          #       sleep 3
          #       echo "Started k3s server (see .k3s-server.log)"
          #     else
          #       echo "k3s server already running."
          #     fi

          #     export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
          #     echo "KUBECONFIG set to $KUBECONFIG"
          #     echo "Use 'kubectl' or 'k' to interact with your cluster."
          #   '';
          # };
          # server = pkgs.mkShell {
          #   packages = commonPackages;
          #   shellHook = ''
          #     echo "Entering Tapis docs nix shell..."
          #     echo "Building docs and starting HTTP server..."
          #     echo "  - use output url to view docs"
          #     echo "  - use Ctrl+C to stop the server"
          #     echo "=========================="
          #     make html
          #     echo "Serving docs via python at: http://localhost:8000/"
          #     cd build/html && python -m http.server
          #   '';
          # };
        };
      }
    );
}

