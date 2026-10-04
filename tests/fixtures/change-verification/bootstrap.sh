#!/bin/sh
# Create a throwaway workspace for docs/change-verification-workflow.md.
# This fixture is authored for Code Scout; it does not contain adapted ESF code.
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 WORKSPACE" >&2
  exit 64
fi

fixture_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
workspace=$1

if [ -e "$workspace" ]; then
  echo "workspace already exists: $workspace" >&2
  exit 65
fi

mkdir -p "$workspace"
cp -R "$fixture_root/api" "$workspace/api"
cp -R "$fixture_root/client" "$workspace/client"
cp -R "$fixture_root/schemas" "$workspace/schemas"
cp -R "$fixture_root/integration" "$workspace/integration"
cp "$fixture_root/verification.toml" "$workspace/verification.toml"
cp "$fixture_root/single-repo.toml" "$workspace/single-repo.toml"

for repo in api client schemas; do
  git -C "$workspace/$repo" init -q
  git -C "$workspace/$repo" config user.email fixture@example.invalid
  git -C "$workspace/$repo" config user.name "Verification Fixture"
  git -C "$workspace/$repo" add .
  git -C "$workspace/$repo" commit -qm "fixture baseline"
done

# These edits deliberately predate session admission and remain uncommitted.
printf 'api=1\ndirty-before-session=yes\n' > "$workspace/api/src/state.txt"
printf 'client=1\ndirty-before-session=yes\n' > "$workspace/client/src/state.txt"
printf 'legacy baseline content\n' > "$workspace/api/src/legacy.txt"

printf '%s\n' "$workspace"
