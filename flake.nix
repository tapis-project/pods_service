# flake.nix
{
  description = "Development environment with Python, Docker, Minikube, etc.";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }: {
    devShells.default = let
      pkgs = import nixpkgs {
        system = "x86_64-linux"; # or "aarch64-darwin" for macOS on ARM
      };
    in pkgs.mkShell {
      buildInputs = [
        pkgs.python310
        pkgs.python310Packages.pip
        pkgs.python310Packages.virtualenv
        pkgs.docker
        pkgs.kubectl
        pkgs.minikube
        pkgs.yq
        pkgs.envsubst
        pkgs.git
        pkgs.postgresql
        pkgs.alembic
      ];

      shellHook = ''
        export PIP_DISABLE_PIP_VERSION_CHECK=1
        export PYTHONNOUSERSITE=1
        echo "Nix development environment loaded."
        echo "You may want to run: python -m venv .venv && source .venv/bin/activate"
      '';
    };
  };
}