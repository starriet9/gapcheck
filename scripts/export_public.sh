#!/usr/bin/env bash
# History-preserving public export. Never modifies the source repository.
#
# Usage: scripts/export_public.sh <out_dir> [<branch>]
#   <out_dir>  a new directory (must not exist)
#   <branch>   branch to export (default: dev); it becomes `main` in the export
#
# Environment (optional):
#   EXPORT_MAILMAP  identity rewrite, git mailmap format
#                   (default: <repo>/private/export.mailmap, not tracked)
#   LEAK_TERMS      private terms for scripts/leak_check.py
#                   (default: <repo>/private/leak_terms.txt, not tracked)
#   PYTHON          interpreter for leak_check.py (stdlib only; default python3)
#
# Steps:
#   1. Clone <branch> with its full history into <out_dir> (new objects, no
#      remote, no tags, no local config).
#   2. List every path that ever existed in that history. If a private path
#      (the ignored set below) ever did, remove it from every commit.
#   3. Rewrite author and committer identities through the mailmap; dates,
#      trees and parents are kept. Abbreviated commit hashes in messages are
#      rewritten to the new hashes. The rewrite is deterministic: the same
#      source history always gives the same export hashes.
#   4. Drop backup refs, reflogs and unreachable objects, so no rewritten-away
#      identity remains in the object store.
#   5. Check: one tree per commit unchanged (unless a path was stripped),
#      no message changed except for hash rewrites, and scripts/leak_check.py
#      on every commit and file of the export.
# Writes <out_dir>/.git/gapcheck-export/{commit-map.tsv,report.txt,leak.json,leak.txt}
# (inside .git, so never part of a commit). Exit status: 0 clean, 1 leak
# findings or a failed check, 2 usage error.
set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 <out_dir> [<branch>]" >&2
  exit 2
fi
SRC="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$1"
BRANCH="${2:-dev}"
MAILMAP="${EXPORT_MAILMAP:-$SRC/private/export.mailmap}"
TERMS="${LEAK_TERMS:-$SRC/private/leak_terms.txt}"
PYTHON="${PYTHON:-python3}"
# Paths that are ignored on purpose; must never appear in the public history.
PRIVATE_RE='^(private/|data/|tools/(jdk|dl|synthea|uv-cache|uv-python|gradle-home|micromamba|r-env|r-home)/|\.claude/|\.venv/)|^(CLAUDE\.md|KICKOFF\.md|env\.local\.sh)$'
PRIVATE_PATHS="private data .claude .venv CLAUDE.md KICKOFF.md env.local.sh tools/jdk tools/dl tools/synthea tools/uv-cache tools/uv-python tools/gradle-home tools/micromamba tools/r-env tools/r-home"

[ -e "$OUT" ] && { echo "$OUT exists; give a new directory" >&2; exit 2; }
[ -f "$MAILMAP" ] || { echo "mailmap not found: $MAILMAP" >&2; exit 2; }
[ -f "$TERMS" ] || { echo "leak terms not found: $TERMS" >&2; exit 2; }
git -C "$SRC" rev-parse --verify -q "refs/heads/$BRANCH" >/dev/null \
  || { echo "no branch $BRANCH in $SRC" >&2; exit 2; }
MAILMAP="$(cd "$(dirname "$MAILMAP")" && pwd)/$(basename "$MAILMAP")"

# 1. Fresh copy with history.
git clone -q --no-local --single-branch --no-tags --branch "$BRANCH" "$SRC" "$OUT"
cd "$OUT"
OUT="$(pwd)"
git remote remove origin
META="$OUT/.git/gapcheck-export"
mkdir -p "$META"
SRC_HEAD="$(git rev-parse HEAD)"
git rev-list --reverse --topo-order HEAD > "$META/old.txt"
N_COMMITS="$(wc -l < "$META/old.txt")"

# 2. Private paths anywhere in the history.
git log --format= --name-only --no-renames HEAD | sort -u | sed '/^$/d' > "$META/all-paths.txt"
grep -E "$PRIVATE_RE" "$META/all-paths.txt" > "$META/private-paths.txt" || true
N_PRIVATE="$(wc -l < "$META/private-paths.txt")"
INDEX_FILTER=()
if [ "$N_PRIVATE" -gt 0 ]; then
  echo "private paths found in history (stripping): $(tr '\n' ' ' < "$META/private-paths.txt")" >&2
  # shellcheck disable=SC2086
  INDEX_FILTER=(--index-filter "git rm -r -q --cached --ignore-unmatch -- $PRIVATE_PATHS")
fi

# 3. Identity and message rewrite.
N_IDENT="$(git log --format='%an <%ae>%n%cn <%ce>' HEAD | sort -u | while read -r id; do
  if [ "$(git -c mailmap.file="$MAILMAP" check-mailmap "$id")" != "$id" ]; then echo "$id"; fi
  done | wc -l)"
N_REAUTHOR="$(git log --format='%H %an <%ae>|%cn <%ce>' HEAD | while read -r h rest; do
  a="${rest%%|*}"; c="${rest#*|}"
  if [ "$(git -c mailmap.file="$MAILMAP" check-mailmap "$a")" != "$a" ] \
     || [ "$(git -c mailmap.file="$MAILMAP" check-mailmap "$c")" != "$c" ]; then echo "$h"; fi
  done | wc -l)"
export EXPORT_MAILMAP_ABS="$MAILMAP"
ENV_FILTER='
canon() { git -c mailmap.file="$EXPORT_MAILMAP_ABS" check-mailmap "$1 <$2>"; }
id="$(canon "$GIT_AUTHOR_NAME" "$GIT_AUTHOR_EMAIL")"
GIT_AUTHOR_NAME="${id% <*}"; e="${id##*<}"; GIT_AUTHOR_EMAIL="${e%>}"
id="$(canon "$GIT_COMMITTER_NAME" "$GIT_COMMITTER_EMAIL")"
GIT_COMMITTER_NAME="${id% <*}"; e="${id##*<}"; GIT_COMMITTER_EMAIL="${e%>}"
export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL
'
# Replace abbreviated hashes of already-rewritten commits (`map` is provided by
# filter-branch; filters run in /bin/sh, so POSIX only). Messages without such a
# hash pass through byte for byte.
MSG_FILTER='
f="$(mktemp)"; cat > "$f"
for tok in $(grep -oE "\b[0-9a-f]{7,40}\b" "$f" | sort -u); do
  full="$(git rev-parse -q --verify "$tok^{commit}" 2>/dev/null)" || continue
  new="$(map "$full")"
  [ -n "$new" ] && [ "$new" != "$full" ] || continue
  abbr="$(printf %s "$new" | cut -c1-${#tok})"
  sed -i "s/\b$tok\b/$abbr/g" "$f"
done
cat "$f"; rm -f "$f"
'
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f "${INDEX_FILTER[@]}" \
  --env-filter "$ENV_FILTER" --msg-filter "$MSG_FILTER" -- HEAD > "$META/filter-branch.log" 2>&1
git branch -m "$BRANCH" main

# 4. No backups, no reflogs, no unreachable objects.
git for-each-ref --format='%(refname)' refs/original/ | while read -r r; do git update-ref -d "$r"; done
git reflog expire --expire=now --all
git gc -q --prune=now

# 5. Checks.
git rev-list --reverse --topo-order HEAD > "$META/new.txt"
paste "$META/old.txt" "$META/new.txt" > "$META/commit-map.tsv"
FAIL=0
[ "$(wc -l < "$META/new.txt")" -eq "$N_COMMITS" ] || { echo "commit count changed" >&2; FAIL=1; }
N_TREE_DIFF=0; N_MSG_DIFF=0
while IFS=$'\t' read -r old new; do
  if [ "$(git -C "$SRC" rev-parse "$old^{tree}")" != "$(git rev-parse "$new^{tree}")" ]; then
    N_TREE_DIFF=$((N_TREE_DIFF + 1))
  fi
  if ! cmp -s <(git -C "$SRC" cat-file commit "$old" | sed '1,/^$/d') \
              <(git cat-file commit "$new" | sed '1,/^$/d'); then
    N_MSG_DIFF=$((N_MSG_DIFF + 1))
    echo "message changed: ${old:0:10} -> ${new:0:10}" >> "$META/message-changes.txt"
  fi
  # Dates must be unchanged.
  [ "$(git -C "$SRC" log -1 --format='%ad|%cd' --date=raw "$old")" = \
    "$(git log -1 --format='%ad|%cd' --date=raw "$new")" ] || { echo "date changed: $old" >&2; FAIL=1; }
done < "$META/commit-map.tsv"
if [ "$N_PRIVATE" -eq 0 ] && [ "$N_TREE_DIFF" -ne 0 ]; then
  echo "trees changed although nothing was stripped" >&2; FAIL=1
fi
# Hashes of the source repository that still appear (they will not resolve in the export).
: > "$META/stale-hash-refs.txt"
while IFS=$'\t' read -r old new; do
  [ "$old" = "$new" ] && continue
  for n in 40 12 10 9 8 7; do
    git grep -n -w -e "${old:0:$n}" HEAD -- . >> "$META/stale-hash-refs.txt" 2>/dev/null || true
    git log --format='%H %B' HEAD | grep -w -e "${old:0:$n}" | sed "s/^/message: /" \
      >> "$META/stale-hash-refs.txt" || true
  done
done < "$META/commit-map.tsv"
sort -u -o "$META/stale-hash-refs.txt" "$META/stale-hash-refs.txt"
N_STALE="$(wc -l < "$META/stale-hash-refs.txt")"

LEAK_STATUS=0
"$PYTHON" -I "$SRC/scripts/leak_check.py" --repo "$OUT" --terms "$TERMS" --refs main \
  --json "$META/leak.json" > "$META/leak.txt" 2> "$META/leak.summary" || LEAK_STATUS=$?

{
  echo "source:              $SRC_HEAD ($BRANCH)"
  echo "export:              $(git rev-parse HEAD) (main)"
  echo "commits:             $N_COMMITS"
  echo "identities mapped:   $N_IDENT"
  echo "commits re-authored: $N_REAUTHOR"
  echo "private paths found: $N_PRIVATE"
  echo "trees changed:       $N_TREE_DIFF"
  echo "messages changed:    $N_MSG_DIFF (abbreviated hashes rewritten)"
  echo "stale hash refs:     $N_STALE (see stale-hash-refs.txt)"
  echo "authors after:       $(git log --format='%an <%ae>' | sort -u | tr '\n' ';')"
  echo "committers after:    $(git log --format='%cn <%ce>' | sort -u | tr '\n' ';')"
  echo "leak_check:          exit $LEAK_STATUS $(cat "$META/leak.summary")"
  echo "checks:              $([ "$FAIL" -eq 0 ] && echo ok || echo FAILED)"
} | tee "$META/report.txt"
cat "$META/leak.txt"
[ "$FAIL" -eq 0 ] && [ "$LEAK_STATUS" -eq 0 ] || exit 1
