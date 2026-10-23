{ pkgs }:
{
  packages = [
    pkgs.python312
    pkgs.uv
  ];

  # Keep the interpreter under Nix control; uv owns only Python dependencies.
  UV_PYTHON = "${pkgs.python312}/bin/python3.12";
  UV_PYTHON_DOWNLOADS = "never";
}
