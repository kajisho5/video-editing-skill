#!/usr/bin/env bash
# Build the release notes for this version and prepend them to CHANGELOG.md, reading every piece of
# commit/PR text through `git log` at runtime. Nothing here interpolates `${{ github.event.* }}` (or
# any other untrusted text) into a shell script: that is the known GitHub Actions script-injection
# pattern (a PR title containing e.g. `$(curl ...)` or backticks, spliced in via `${{ }}`, becomes
# part of the script *before* the shell parses it). Reading the same text back out via `git log`
# instead makes it plain data: it is only ever assigned to a variable or redirected into a file, never
# re-evaluated as code.
#
# Inputs (environment): NEXT_VERSION (required), LATEST_TAG (optional; empty = first release).
# Output: overwrites ./release_notes.md (the range's log, used as the GitHub Release body and as this
# version's CHANGELOG.md entry) and prepends the same entry to ./CHANGELOG.md (created if missing).
set -euo pipefail

if [ -z "${NEXT_VERSION:-}" ]; then
  echo "render_changelog.sh: NEXT_VERSION is required" >&2
  exit 1
fi

range="HEAD"
if [ -n "${LATEST_TAG:-}" ]; then
  range="${LATEST_TAG}..HEAD"
fi

release_date="$(date -u +%Y-%m-%d)"

{
  echo "## v${NEXT_VERSION} - ${release_date}"
  echo
  # %s (subject) only: PR bodies can contain arbitrary Markdown that would break CHANGELOG.md's own
  # structure; the subject line is what squash-merge already reduces every PR to (its title, in this
  # repo's convention).
  if ! git log "$range" --no-merges --pretty='format:- %s (%h)' 2>/dev/null; then
    echo "- (no commits since ${LATEST_TAG:-the initial commit})"
  fi
  echo
} > release_notes.md

# a range with zero commits (e.g. a re-run with nothing new) still gets a heading; don't leave a
# body with only blank lines under it
if ! grep -q '^- ' release_notes.md; then
  sed -i '/^## /a \\n_No changes recorded for this range._' release_notes.md
fi

# Keep "# Changelog" as the file's first line always: prepending naively (new entry, then the whole
# old file) would bury that header under the first entry on every run after the first.
header="# Changelog"
if [ -f CHANGELOG.md ] && [ "$(head -n1 CHANGELOG.md)" = "$header" ]; then
  existing_body_file="$(mktemp)"
  tail -n +2 CHANGELOG.md > "$existing_body_file"
elif [ -f CHANGELOG.md ]; then
  existing_body_file="CHANGELOG.md"   # no recognized header: keep the whole prior file as the body, don't lose it
else
  existing_body_file=""
fi

{
  echo "$header"
  echo
  cat release_notes.md
  [ -n "$existing_body_file" ] && cat "$existing_body_file"
} > CHANGELOG.md.new
mv CHANGELOG.md.new CHANGELOG.md
