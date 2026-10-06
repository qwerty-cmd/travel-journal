#!/usr/bin/env bash
# Snapshot the uncommitted working tree (tracked + untracked, honouring .gitignore)
# to origin/wip/<branch> without touching HEAD, the index or the
# feature branch. Restore with: git fetch origin wip/<branch> &&
#   git checkout origin/wip/<branch> -- . (then review).
set -euo pipefail
cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
branch=$(git rev-parse --abbrev-ref HEAD)
if [ -z "$(git status --porcelain)" ]; then echo "checkpoint: tree clean, nothing to save"; exit 0; fi
tmp_index=$(mktemp)
trap 'rm -f "$tmp_index"' EXIT
cp .git/index "$tmp_index"
GIT_INDEX_FILE="$tmp_index" git add -A
tree=$(GIT_INDEX_FILE="$tmp_index" git write-tree)
msg="wip checkpoint: ${1:-in-progress task} ($(date -u +%FT%TZ))"
commit=$(git commit-tree "$tree" -p HEAD -m "$msg")
for i in 1 2 3 4; do
  git push -q -f origin "$commit:refs/heads/wip/$branch" && { echo "checkpoint: saved $commit ($msg)"; exit 0; }
  sleep $((2**i))
done
echo "checkpoint: push failed" >&2; exit 1
