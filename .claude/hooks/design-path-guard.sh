#!/usr/bin/env bash
# The designer agent writes design specs and mockups under docs/design/ only
# (.claude/agents/designer.md). Application code, tests, the tracker and the
# Kubb-generated client are implemented by dev from its specs, never directly.
f=$(jq -r .tool_input.file_path)
f=$(realpath -m "$f")
rel=${f#"$CLAUDE_PROJECT_DIR"/}
case "$rel" in
  docs/design/*) exit 0 ;;
esac
echo "designer agent may write only under docs/design/; blocked: $rel" >&2
exit 2
