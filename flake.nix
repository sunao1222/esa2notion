{
  description = "esa2notion development environment";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pkgsFor = system: import nixpkgs { inherit system; };
    in
    {
      devShells = forAllSystems (system: {
        default = (pkgsFor system).mkShell (import ./shell.nix { pkgs = pkgsFor system; });
      });
      formatter = forAllSystems (system: (pkgsFor system).nixfmt);
    };
}
