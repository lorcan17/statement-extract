{
  description = "Bank/credit-card statement PDF parser (BMO, Amex Cobalt, EQ Bank, Coast Capital)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.flake-parts.url = "github:hercules-ci/flake-parts";

  outputs = inputs@{ flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [ "x86_64-linux" "aarch64-darwin" ];

      perSystem = { pkgs, ... }: {
        packages.default = pkgs.python312Packages.buildPythonApplication {
          pname = "statement-extract";
          version = "0.0.1";
          pyproject = true;
          src = ./.;

          build-system = [ pkgs.python312Packages.hatchling ];

          dependencies = with pkgs.python312Packages; [
            pdfplumber
            pydantic
            typer
          ];

          # No tests in the source tree (see Foundry STATUS.md — fixtures and
          # tests are kept locally until the expected.json refactor lands).
          doCheck = false;

          meta = {
            description = "Convert bank/credit-card statement PDFs to structured data";
            mainProgram = "statement-extract";
          };
        };

        devShells.default = pkgs.mkShell {
          packages = [ pkgs.uv pkgs.python312 ];
        };
      };
    };
}
