#!/usr/bin/env bash
# The docs agent writes docs/ and doc comments co-located with code, nothing
# else (.claude/agents/docs.md; decision-log Entry 12). A path check cannot
# tell a doc comment from a logic edit inside one file, so app source stays
# writable and everything outside it is blocked. t-docs-agent-unscoped-grant.
f=$(jq -r .tool_input.file_path)
f=$(realpath -m "$f")
rel=${f#"$CLAUDE_PROJECT_DIR"/}
case "$rel" in
  frontend/src/api/*) ;;  # Kubb-generated: never hand-edited
  docs/*|backend/app/*|backend/tests/*|frontend/src/*) exit 0 ;;
esac
echo "docs agent may write only docs/ and doc comments in app source; blocked: $rel" >&2
exit 2
