# Future work prompt — history cleanup

Hand this to a Claude Code session opened in this repo.

## Read first
- `CLAUDE.md` — agent pipeline, finding-triage gate, off-limits list.
- Commits carry the owner's name only: author `qwerty-cmd
  <58459738+qwerty-cmd@users.noreply.github.com>`, no `Co-Authored-By: Claude`, no
  `Claude-Session:` trailer, no "Generated with Claude Code" line.
- Never write a trip slug, a live URL containing a slug, or any secret into the repo.
- Not needed for the 2026 trip.

---

## Rewrite git history: owner-only authorship (`t-history-rewrite-owner-only`)

### Goal
Every commit on every branch authored and committed by `qwerty-cmd
<58459738+qwerty-cmd@users.noreply.github.com>`, with no Claude attribution, so Claude no
longer appears as a contributor and the old personal email leaves public history.

### State found on 2026-09-30 (re-inventory before starting; it will have grown)
- Authors: 69 `Claude <noreply@anthropic.com>`, 100 `qwerty-cmd <diamondheader@Hotmail.com>`,
  6 `qwerty-cmd <58459738+qwerty-cmd@users.noreply.github.com>`.
- Committers: also `GitHub <noreply@github.com>` on 6 merge commits made in the web UI.
- Trailers: `Co-Authored-By: Claude <model> <noreply@anthropic.com>` (several model
  names) and `Claude-Session: https://claude.ai/code/session_...`.
- Remote branches: `main`, `chore/m1-m2-review-fixes`, `chore/m1-m2-review-fixes-gqmh99`,
  `feat/trip-onboarding-access`. Also `refs/claude-teleport/*` and `refs/pull/1..7/head`.
- Short commit hashes are cited ~125 times in tracked files (mostly
  `docs/progress-notes.md`, `docs/progress.json` "commits" arrays, `docs/decision-log.md`,
  `docs/api-contract.md`, the runbook and the handover).

### Steps (owner approves each destructive step in-session)
1. `pip install git-filter-repo`. Work on a fresh `git clone --mirror` backup first, kept
   until the owner confirms the result.
2. Rewrite ALL branches with `git filter-repo`:
   - mailmap: every author/committer identity → the owner's noreply identity (decide with
     the owner whether GitHub-UI merge committers are rewritten too).
   - message callback: delete lines matching `^Co-Authored-By: .*noreply@anthropic\.com`
     and `^Claude-Session: `, plus any "Generated with Claude Code" line; trim trailing
     blank lines.
   - filter-repo rewrites abbreviated hashes inside commit messages automatically.
3. Remap hashes inside tracked files using `.git/filter-repo/commit-map` (old → new),
   matching 7+ char prefixes; commit that as one follow-up `docs:` commit. Verify zero
   stale hashes remain (`git grep` every old short hash from the map).
4. Verify: `git log --all --format='%an <%ae>|%cn <%ce>' | sort -u` shows only the owner;
   `git log --all --format=%B | grep -ciE 'anthropic|claude-session'` is 0; the backend
   test suite and frontend tests still pass on the rewritten `main`.
5. Force-push every branch (`git push --force-with-lease`), delete stale remote branches
   the owner no longer needs, and tell the owner every other clone/session must re-clone.
6. Residue the push cannot remove: GitHub keeps `refs/pull/*` for PRs #1–#7, so closed PR
   pages still show the old commits and Claude. Full removal needs a GitHub Support
   request to purge cached refs/views, or recreating the repo (loses PR history). Present
   both to the owner; do neither unprompted. The contributors graph may take time to
   refresh.

### Acceptance
Only the owner's identity in `git log --all`; no Claude trailers; no stale hashes in
tracked files; tests pass; remote branches force-pushed; owner told about PR-ref residue.
