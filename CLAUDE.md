# CLAUDE.md — durable state for whoever (human or Claude session) works on this repo next

This file has no runtime effect on the Skill. It exists so a session with no memory of prior
conversations can pick up maintenance here correctly, without re-deriving decisions already made
or re-litigating boundaries already settled. If anything here disagrees with the code, the code
and `docs/decisions.md` win — update this file, don't trust it blindly.

## What this repo is

A deterministic, execution-only video editing Skill (`SKILL.md`, `README.md`). It turns a typed
edit request into an operation graph, compiles it to typed calls of an `ffmpeg-skill` tool, runs it
inside a workspace boundary, and validates the result. **It holds no editing judgement** — what to
cut, which camera, why — that belongs to the caller (`video-production-agent` or anything else).
This is ADR-001 and it is the one rule every other decision in this repo defers to.

## Source of truth, in order

1. **Code** — `src/video_editing_skill/`. `contract.py`'s `skill_contract()` is generated from
   `operations.py` / `errors.py` / `compiler.py` / `ffmpeg_skill.py`, never maintained beside them.
2. **`docs/decisions.md`** — the ADR log (currently ADR-001 through ADR-007). Read it before
   changing anything that looks like a design decision; it explains *why*, not just *what*.
3. **`docs/contract.md`** — contract versioning rules, pinned vs. additive blocks, drift
   classification. Read it before touching `contract.py`'s `PINNED_BLOCKS` or top-level shape.
4. **`docs/operations.md`**, **`docs/security.md`** — per-operation reference, security posture.
5. **`README.md`** / **`SKILL.md`** — the user/agent-facing docs; `contract_check.verify_docs()`
   enforces that every operation type is mentioned in them, so they can't silently drift from code.

## Contract discipline (read `docs/contract.md` and ADR-007 before touching `contract.py`)

Two independent version axes, both on `skill_contract()`:

- **`version`** (currently `"0.1.0"`) — this package's release version. Free to move on any release.
- **`contract_version`** (currently `"1.0"`) — the version of the *pinned shape*
  (`PINNED_BLOCKS` in `contract.py`). Bumps only when a pinned block changes in a breaking way.
  A dependent pins a range against `contract_version`, never `version`.

Any change inside `PINNED_BLOCKS` (`schema, skill_id, contract_version, operations, unsupported,
errors, execution, capabilities, schemas`, plus pinned `ToolSpec` fields) is breaking:
bump `contract_version` (and `version`, since this package does not yet release independently of
its contract), expect `video-production-agent` to re-pin, and regenerate the golden copy:

```
video-editing contract --json > tests/contract/contract.json
video-editing contract --check tests/contract/contract.json   # must report "ok"
```

New top-level keys or `tools[]` keys outside the pinned blocks are additive and don't need a
version bump — that's how 0.1.0 gained `media_compatibility`, `provides`, `contract_version` itself,
and the richer execution report without breaking the one known consumer.

## Testing

```
cd tests && PYTHONPATH=../src python3 -m unittest discover -s . -p "test_*.py" -v
```

Offline suite (unit / paths / security / engine-contract with a fake engine) needs nothing beyond
the standard library. `test_integration.py`'s real-media matrix (scenarios A–O) needs a real
`ffmpeg-skill` checkout reachable via `VIDEO_EDITING_FFMPEG_SKILL_DIR` / `--ffmpeg-skill-dir` /
a well-known relative path — it's skipped, not failed, when one isn't found, which is normal for a
sandboxed session. `.github/workflows/tests.yml` runs both, matrixed across OS/Python, plus
`ruff check`, `mypy`, and `contract --check`.

## Ecosystem relationships (verify live, never assume — these repos evolve independently)

- **`ffmpeg-skill`** (dependency) — the only FFmpeg boundary. Version range pinned in
  `ffmpeg_skill.py` (`SUPPORTED_MIN` / `SUPPORTED_MAX_EXCLUSIVE`, currently `>=0.9.0,<1.0.0`).
  Verified against both 0.9.0 and 0.9.1 as of this writing; re-verify `fit.py` / `join.py` /
  `overlay.py` geometry and behavior before raising the ceiling.
- **`video-production-agent`** (consumer) — the only known caller. Its adapter
  (`src/video_agent/tools/video_editing/adapter.py`, `check_contract()`) range-checks `version`
  against `("0.1.",)` and validates the pinned blocks; it does not (yet) read `contract_version`.
  Never edit that repo from here — verify compatibility by reading its adapter code and, ideally,
  running its `check_contract()` against this repo's live `skill_contract()` before merging a
  contract change (see PR #3's description for the pattern).
- **`AI-video-production-OS`** (parent architecture repo) — defines the cross-repository
  `CapabilityContract` (`provides`, `contract_version`, ...) this repo participates in as one of
  ten Skills. Its own `docs/ROADMAP.md` describes an 8-phase rollout; Phases 1–2 (schema + registry
  + per-Skill `provides`/`contract_version` retrofit) require **zero** changes to
  `video-production-agent` and are this repo's to do independently. Phase 3 onward (collision
  resolution, registry-driven execution) is explicitly the agent maintainers' work, not this
  repo's — do not start it here. Before doing OS-integration work, clone that repo and read
  `docs/SPEC.md`, `docs/VERSIONING.md`, `docs/ROADMAP.md`, and `registry/` directly; don't trust a
  stale summary of them (including this one, over time).

## Explicitly out of scope (do not implement here, ever, regardless of how it's asked for)

AI/LLM-driven editing decisions, automatic editing, a `ProductionPlan` or Decision Engine, a
Context/Inference layer, speaker/scene/slide detection, camera-switching logic, semantic editing,
ranking or scoring of edits, cloud execution, an MCP server, a plugin loader, or an arbitrary
FFmpeg-argument wrapper. All of these belong to `video-production-agent` or a different Skill, if
anywhere. If a request seems to need one of these, the right move is a typed, narrow
`OPERATION`/`CapabilityContract` addition here plus the judgement staying in the caller — not an
exception to ADR-001.

## Status snapshot (update this section, don't let it go stale)

- PR #1 (merged) — 0.1.0 core: 8 operations, contract, doctor, execution, real-media test matrix.
- PR #2 (merged) — `provides`: publishes Capability ids for `AI-video-production-OS` discovery
  (ADR-006).
- PR #3 (merged) — `contract_version`: independent shape-version axis (ADR-007).
- This file (added alongside/after PR #3) — durable maintainer state, previously missing.

## Picking the next task

There is no standing task queue; the next gap is found, not assigned. To find it: re-read this
file and `docs/decisions.md` for anything marked planned-but-undone (e.g. contract 0.2.0 items in
ADR-002), re-check `AI-video-production-OS`'s `docs/ROADMAP.md` / `docs/ECOSYSTEM_CHANGELOG.md` for
this Skill's outstanding per-repo items, and re-run `contract --check` and the test suite to make
sure nothing has silently drifted. Prefer small, additive, independently-testable changes that
don't touch `video-production-agent`; anything that would (Phase 3+ of the OS roadmap, a breaking
contract change) is a proposal to raise, not a change to push.
