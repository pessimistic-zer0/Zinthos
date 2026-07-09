{
  description = "Zinthos development environments";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs, ... }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs {
        inherit system;
        config.allowUnfree = true;
      };
      py = pkgs.python312;
      pyPkgs = pkgs.python312Packages;

      # Libraries required to make pre-compiled PyPI wheels (like PyTorch) work on NixOS
      linuxLibs = with pkgs; [
        stdenv.cc.cc.lib
        zlib
        glib
      ];

    in {
      devShells.${system} = {
        default = pkgs.mkShell {
          name = "zinthos-default";

          packages = with pkgs; [
            # C++ ETL toolchain
            gcc
            cmake
            ninja
            sqlite
            pkg-config
            gnumake

            # Python runtime + nixpkgs-managed ML/API stack
            py
            pyPkgs.pip
            pyPkgs.setuptools
            pyPkgs.wheel
            pyPkgs.numpy
            pyPkgs.pandas
            pyPkgs.scikit-learn
            pyPkgs.tqdm
            pyPkgs.matplotlib
            pyPkgs.fastapi
            pyPkgs.uvicorn
            pyPkgs.sqlalchemy
            pyPkgs.faiss

            # Rust toolchain for the ratatui TUI client (M6)
            rustc
            cargo
            rustfmt
            clippy
            rust-analyzer

            # REMOVED: pyPkgs.torchWithCuda
          ];

          # Bind the standard C libraries AND the host NVIDIA drivers
          LD_LIBRARY_PATH = "${pkgs.lib.makeLibraryPath linuxLibs}:/run/opengl-driver/lib";
          
          shellHook = ''
            # Anchor the venv to the flake/repo root by walking up from $PWD to
            # the directory containing flake.nix. Previously VENV_DIR was ".venv"
            # relative to $PWD, so invoking `nix develop` from a subdir (e.g.
            # backend/) created a SEPARATE .venv there — the source of duplicate,
            # diverging venvs. Pure-shell walk-up keeps this dependency-free.
            FLAKE_ROOT="$PWD"
            while [ "$FLAKE_ROOT" != "/" ] && [ ! -e "$FLAKE_ROOT/flake.nix" ]; do
              FLAKE_ROOT="$(dirname "$FLAKE_ROOT")"
            done
            [ -e "$FLAKE_ROOT/flake.nix" ] || FLAKE_ROOT="$PWD"
            export VENV_DIR="$FLAKE_ROOT/.venv"

            if [ ! -d "$VENV_DIR" ]; then
              echo "Creating $VENV_DIR with --system-site-packages..."
              ${py}/bin/python -m venv --system-site-packages "$VENV_DIR"
            fi

            . "$VENV_DIR/bin/activate"
            export PIP_DISABLE_PIP_VERSION_CHECK=1

            echo "Activated $VENV_DIR."
          '';

        };

        frontend = pkgs.mkShell {
          name = "zinthos-frontend";
          packages = with pkgs; [
            nodejs_22
          ];
        };

        # Rust shell for the ratatui TUI client (M6). Deps are pure-Rust (ratatui,
        # crossterm, ureq no-TLS, serde) so we only need the toolchain + a C compiler.
        tui = pkgs.mkShell {
          name = "zinthos-tui";
          packages = with pkgs; [
            rustc
            cargo
            rustfmt
            clippy
            rust-analyzer
            pkg-config
            gcc

            # figlet: regenerate the Zinthos banner from the vendored
            # "Delta Corps Priest 1" font (tui/assets/). See tui/assets/regen-banner.sh.
            figlet

            # vhs: render the README demo GIFs from the .tape scripts in
            # tui/assets/. See tui/assets/CLAUDE.md for the recording runbook.
            vhs
          ];
          RUST_BACKTRACE = "1";
        };
      };
    };
}
