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
            pkgs.bash

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

            # Native libs required by pre-built manylinux Python wheels
            pkgs.stdenv.cc.cc.lib  # libstdc++, libgcc_s
            pkgs.zlib              # libz
            pkgs.openssl           # libssl, libcrypto (already listed above, kept for clarity)
            pkgs.libffi            # libffi (already listed above)

            # Node 24 + pnpm 10 — frontend / Playwright
            pkgs.nodejs_24
            pkgs.pnpm_10
          ];

          shellHook = ''
            # Capture the project root once, so it stays correct even after `cd`
            export COSMOS_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

            # Point uv at the nixpkgs Python so it doesn't try to download one
            export UV_PYTHON="${pkgs.python312}/bin/python3"
            export UV_PYTHON_DOWNLOADS=never
            # uv can't hardlink into the nix store, so use copies
            export UV_LINK_MODE=copy

            # Keep bundler gems local to the project tree.
            # Anchored to COSMOS_ROOT so `bundle exec` works from any subdirectory.
            export BUNDLE_PATH="$COSMOS_ROOT/openc3/.bundle/gems"
            export GEM_HOME="$BUNDLE_PATH"

            # Pre-built manylinux wheels need several native libs that Nix doesn't
            # expose on LD_LIBRARY_PATH by default (libstdc++, libz, libssl, etc.).
            export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [
              pkgs.stdenv.cc.cc.lib
              pkgs.zlib
              pkgs.openssl
              pkgs.libffi
            ]}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

            # openc3.sh and scripts/linux/*.sh all have #!/bin/bash shebangs,
            # which don't exist on NixOS. Rewrite them to the real bash path.
            patchShebangs --host "$COSMOS_ROOT/openc3.sh" "$COSMOS_ROOT/scripts/" 2>/dev/null || true

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
