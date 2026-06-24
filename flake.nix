{
  description = "OpenC3 COSMOS development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in {
        devShells.default = pkgs.mkShell {
          packages = [
            # Core utilities
            pkgs.git
            pkgs.just
            pkgs.curl
            pkgs.jq

            # Ruby 3.4 — for local `bundle exec rspec`
            pkgs.ruby_3_4

            # Build tools for Ruby's C extensions
            pkgs.gcc
            pkgs.gnumake
            pkgs.pkg-config
            pkgs.libyaml
            pkgs.openssl
            pkgs.zlib
            pkgs.libffi

            # Python toolchain — uv manages the venv, python3 is the base
            pkgs.python312
            pkgs.uv
            pkgs.ruff

            # Node 24 + pnpm 10 — frontend / Playwright
            pkgs.nodejs_24
            pkgs.pnpm_10
          ];

          shellHook = ''
            # Point uv at the nixpkgs Python so it doesn't try to download one
            export UV_PYTHON="${pkgs.python312}/bin/python3"
            export UV_PYTHON_DOWNLOADS=never
            # uv can't hardlink into the nix store, so use copies
            export UV_LINK_MODE=copy

            # Keep bundler gems local to the project tree
            export BUNDLE_PATH="$PWD/.bundle/gems"
            export GEM_HOME="$BUNDLE_PATH"

            echo "OpenC3 COSMOS dev environment loaded"
            echo "  docker: $(docker --version 2>/dev/null || echo 'not found — install via NixOS system config')"
            echo "  ruby:   $(ruby --version)"
            echo "  python: $(python3 --version)"
            echo "  node:   $(node --version)"
            echo "  pnpm:   $(pnpm --version)"
            echo "  uv:     $(uv --version)"
            echo "  just:   $(just --version)"
          '';
        };
      });
}
