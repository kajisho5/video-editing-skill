---
name: video-editing-skill
description: Deterministic video editing from a typed edit request (trim, cut, concat with transitions, speed change, fit / fill / resize, still-image overlay) with an exact source-to-timeline mapping and per-operation provenance. Executes through ffmpeg-skill; never takes commands, argv, filter strings or executables. Use when an agent has already decided what to edit and needs the edit performed, verified and traced. Not for deciding what to cut, transcribing, captioning, colour grading or audio mastering.
---

# video-editing-skill

`video-editing run - --json --workspace DIR [--allowed-input ROOT]` reads one request document on stdin and
prints one response document on stdout. Everything else (`skill`, `doctor`, `validate`, `plan`) is documented in
`README.md`; the machine-readable contract is `video-editing contract --json`.

Workflow for a caller:

1. `doctor --json --workspace DIR` once: ffmpeg-skill, ffmpeg and ffprobe must be AVAILABLE.
2. Build the request from the contract's `request_shape`: sources (files under an allowed root), allowlisted
   operations with typed params, outputs (relative paths under the workspace).
3. `plan - --json` to see the operation graph, the timeline mapping, the tool per step and the commands
   ffmpeg-skill would run. Nothing is written.
4. `run - --json`. On success every output carries its sha256, its timeline and an OBSERVED probe; every
   operation carries a provenance record. On failure `{"ok": false, "error": {code, message, retryable}}` and no
   output file is left behind.

Operation types (the allowlist): `TRIM`, `CUT`, `CONCAT` (with `params.transition`), `SPEED`, `FIT`, `FILL`,
`RESIZE`, `OVERLAY`. Anything else (`CROP`, `FREEZE`, `REVERSE`, `IMAGE_INSERT`, `POSITION` included) is refused
with `UNSUPPORTED_OPERATION`; the contract's `unsupported` list says why.

Times are exact: `"1:30"`, `"00:01:30.250"`, `{"frames": 300, "fps": "30000/1001"}` or a number of seconds.

`contract --check tests/contract/contract.json` verifies the live contract against the implementation, the docs
and that saved copy; exit 1 on any problem.
