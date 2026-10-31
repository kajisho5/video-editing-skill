#!/usr/bin/env bash
# Decide the version this push should release, without ever executing untrusted text: every value
# either comes from `git` (read at runtime, never spliced into a workflow `run:` block via `${{ }}`)
# or from the trusted `resolved_version` output of release-drafter's dry run.
#
# Rule (see CLAUDE.md "Releases"):
#   - auto-bump fires ONLY when pyproject.toml's current version already equals the latest release
#     tag. In that case the next version is whatever release-drafter's dry run resolved from the
#     labels of PRs merged since that tag (default: patch when a merged PR carries no version label).
#   - otherwise (no tag yet, or the version was already bumped by hand ahead of the last tag) the
#     current pyproject.toml version is respected as-is and released unchanged: this is what makes a
#     manual version bump in a merged PR (this repo's own convention for a breaking contract change,
#     see CLAUDE.md "Contract discipline") stick instead of being overwritten by an automatic bump.
#   - the first run (no tag yet) therefore releases pyproject.toml's version unchanged, never a computed bump.
#
# Inputs (environment):
#   CURRENT_VERSION           required; from pyproject.toml, e.g. "0.3.0"
#   DRAFTER_RESOLVED_VERSION  release-drafter's dry-run `resolved_version` output; only read when
#                             the auto-bump branch is taken. May be unset/empty otherwise.
#
# Output: writes `next_version`, `latest_tag`, `mode` (auto|manual) and `should_release` (true|false)
# to $GITHUB_OUTPUT when set, and always prints them as `key=value` lines on stdout so this script is
# runnable and checkable standalone (tests/test_release_scripts.py runs it against throwaway git repos).
set -euo pipefail

if [ -z "${CURRENT_VERSION:-}" ]; then
  echo "compute_version.sh: CURRENT_VERSION is required" >&2
  exit 1
fi

# Newest vX.Y.Z tag reachable in this repo's tag list (semver sort, not commit-date sort: a hotfix
# tag cut from an older commit must still be treated as "latest" for the equality check above).
latest_tag="$(git tag --list 'v*' --sort=-v:refname | head -n1 || true)"
latest_version="${latest_tag#v}"

if [ -n "$latest_tag" ] && [ "$latest_version" = "$CURRENT_VERSION" ]; then
  mode="auto"
  if [ -z "${DRAFTER_RESOLVED_VERSION:-}" ]; then
    # nothing merged since the last tag resolves to a version: no release, said rather than failed
    echo "compute_version.sh: nothing to release (no version resolved since ${latest_tag})" >&2
    next_version="$CURRENT_VERSION"
  else
    next_version="${DRAFTER_RESOLVED_VERSION#v}"
  fi
else
  mode="manual"
  next_version="$CURRENT_VERSION"
fi

if git rev-parse -q --verify "refs/tags/v${next_version}" >/dev/null; then
  should_release="false"
else
  should_release="true"
fi

{
  echo "next_version=${next_version}"
  echo "latest_tag=${latest_tag}"
  echo "mode=${mode}"
  echo "should_release=${should_release}"
} | tee -a "${GITHUB_OUTPUT:-/dev/null}"
