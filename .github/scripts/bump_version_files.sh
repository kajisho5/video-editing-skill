#!/usr/bin/env bash
# Write NEXT_VERSION into every file that carries this repo's version number, and (repo-specific,
# see CLAUDE.md) regenerate the golden contract copy so CI's `contract --check` does not fail on
# the very next run: `version` (top level and every tools[].version) is part of what
# tests/contract/contract.json commits to, and it changes on every bump -- auto or manual.
#
# A no-op (exit 0, nothing written) when CURRENT_VERSION already equals NEXT_VERSION: the "manual"
# path in compute_version.sh means a human already edited these files (and, per CLAUDE.md "Contract
# discipline", already regenerated the golden copy) as part of the merged PR; touching them again here would
# just be a needless empty commit.
#
# Set REGENERATE_CONTRACT=1 to also run `video-editing contract --json > tests/contract/contract.json`
# (requires the package installed, i.e. `pip install -e .` already ran); the release workflow sets it,
# tests/test_release_scripts.py does not (it runs the script on copies of the two version files).
set -euo pipefail

if [ -z "${NEXT_VERSION:-}" ] || [ -z "${CURRENT_VERSION:-}" ]; then
  echo "bump_version_files.sh: NEXT_VERSION and CURRENT_VERSION are required" >&2
  exit 1
fi

if [ "$NEXT_VERSION" = "$CURRENT_VERSION" ]; then
  echo "bump_version_files.sh: version unchanged ($CURRENT_VERSION); nothing to write"
  exit 0
fi

python3 - "$CURRENT_VERSION" "$NEXT_VERSION" <<'PYEOF'
import pathlib
import re
import sys

current, nxt = sys.argv[1], sys.argv[2]

def replace_one(path: pathlib.Path, pattern: str) -> None:
    text = path.read_text(encoding="utf-8")
    compiled = re.compile(pattern.format(v=re.escape(current)), re.MULTILINE)
    matches = compiled.findall(text)
    # subn(..., count=1) caps *replacements* at 1 regardless of how many matches exist, so it can
    # never itself report ambiguity -- count matches with findall first, replace only once we know
    # there is exactly one.
    if len(matches) != 1:
        raise SystemExit(f"{path}: expected exactly one match for the current version ({current}), found {len(matches)}")
    new_text = compiled.sub(lambda m: m.group(0).replace(current, nxt), text, count=1)
    path.write_text(new_text, encoding="utf-8")
    print(f"updated {path}")

replace_one(pathlib.Path("pyproject.toml"), r'^version = "{v}"$')
replace_one(pathlib.Path("src/video_editing_skill/__init__.py"), r'^VERSION = "{v}"$')
PYEOF

if [ "${REGENERATE_CONTRACT:-0}" = "1" ]; then
  video-editing contract --json > tests/contract/contract.json
  echo "regenerated tests/contract/contract.json for v${NEXT_VERSION}"
fi
