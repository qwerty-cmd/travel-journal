#!/usr/bin/env bash
# qa must never mutate the live working tree (see .claude/agents/qa.md).
# Fingerprints the tree (tracked + untracked, .gitignore respected) before and
# after each qa Bash call via a throwaway index, so the real index is never
# touched. Detects and reports; it cannot prevent a change made and reverted
# inside one command. t-qa-mutation-hook.
set -euo pipefail
in=$(cat)
P="$CLAUDE_PROJECT_DIR"
snap="${TMPDIR:-/tmp}/qa-guard-$(jq -r .tool_use_id <<<"$in")"
fp() {
  local i
  i=$(mktemp)
  cp "$(git -C "$P" rev-parse --absolute-git-dir)/index" "$i"
  GIT_INDEX_FILE=$i git -C "$P" add -A >/dev/null 2>&1
  GIT_INDEX_FILE=$i git -C "$P" write-tree
  rm -f "$i"
}
if [ "$1" = pre ]; then fp >"$snap"; exit 0; fi
[ -f "$snap" ] || exit 0
before=$(cat "$snap"); rm -f "$snap"
[ "$(fp)" = "$before" ] && exit 0
echo "LIVE TREE CHANGED during this command. Do not restore anything. Report the command and \`git status --porcelain\` to your caller. (If another agent is editing this checkout in parallel, the change may be theirs: say so rather than assume.)" >&2
exit 2
