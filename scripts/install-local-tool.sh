#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
graphify_package="${CODE_SCOUT_GRAPHIFY_PACKAGE:-graphifyy}"
rlm_requirement="rlm @ git+https://github.com/rawwerks/rlm.git@9a95a2bb38a73499473bc830b5b1b0806e79ce47"

command -v uv >/dev/null 2>&1 || {
  echo "error: uv is not installed or is not on PATH" >&2
  exit 127
}

echo "Installing code-scout from ${project_root}"
echo "Installing Graphify CLI from ${graphify_package}"
uv tool install \
  --force \
  --reinstall \
  --refresh \
  --python 3.12 \
  "${graphify_package}"

echo "Installing code-scout from ${project_root}"
uv tool install \
  --force \
  --reinstall \
  --refresh \
  --python 3.12 \
  --with "${rlm_requirement}" \
  "$@" \
  "${project_root}"

echo
echo "Installed CLI:"
code-scout --help >/dev/null
command -v code-scout
