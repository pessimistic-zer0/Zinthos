{
  description = "Sonic Something development environments";

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
          name = "sonic-something-default";

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
            export VENV_DIR=".venv"

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
          name = "sonic-something-frontend";
          packages = with pkgs; [
            nodejs_22
          ];
        };

        # Rust shell for the ratatui TUI client (M6). Deps are pure-Rust (ratatui,
        # crossterm, ureq no-TLS, serde) so we only need the toolchain + a C compiler.
        tui = pkgs.mkShell {
          name = "sonic-something-tui";
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
          ];
          RUST_BACKTRACE = "1";
        };
      };
    };
}
