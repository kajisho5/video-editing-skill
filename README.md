# video-editing-skill

A deterministic, verifiable **video editing Skill** for the video-production-agent ecosystem.

It takes a typed edit request (sources, operations, outputs), validates it, builds an operation graph with an
exact source-to-timeline mapping, compiles every operation to a typed call of an
[ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) tool, runs it inside a workspace boundary, validates the
result and reports provenance. It is the *hands* of an editing pipeline, not the head.

```
video-editing-skill ≠ video-production-agent   (no decisions, no plan, no LLM, no policy)
video-editing-skill ≠ ffmpeg-skill             (no ffmpeg command generation, no filter strings)
```

Python ≥ 3.9, standard library only. Requires an ffmpeg-skill 0.9.x checkout and ffmpeg / ffprobe on `PATH`.

## Scope

Provided (capabilities are declared only where an implementation exists):

| Capability | Operation | Executed by |
|---|---|---|
| `video.trim` | `TRIM` keep `[start, end)` of one input | `ffmpeg-skill/cut` |
| `video.cut` | `CUT` keep several ranges, joined in the order given | `ffmpeg-skill/cut` |
| `video.concat` | `CONCAT` join ≥ 2 inputs in order, conform size / fps | `ffmpeg-skill/join` |
| `video.transition` | `CONCAT` with `params.transition` (xfade family) | `ffmpeg-skill/join` |
| `video.reorder` | order of `CONCAT.inputs` / `CUT.keep` | — |
| `video.speed` | `SPEED` constant factor 1/4 … 4, pitch-preserved audio | `ffmpeg-skill/fit` |
| `video.fit` | `FIT` change the aspect, keep every pixel (letterbox / pillarbox with `pad_color`) | `ffmpeg-skill/fit` |
| `video.fill` | `FILL` change the aspect, keep the centre (scale to cover, centre-crop) | `ffmpeg-skill/fit` |
| `video.resize` | `RESIZE` change the size, keep the aspect (`width`, height follows; nothing padded / cropped / stretched) | `ffmpeg-skill/fit` |
| `video.overlay` | `OVERLAY` still image at a position for a time range | `ffmpeg-skill/overlay` |

Plus: timeline assembly (segments with source and timeline ranges, a second track for overlays), deterministic
operation identity, dry run, reuse of identical earlier results, output validation, provenance, an optional typed
encoding profile per output (`crf`, `preset`; everything else is fixed by the engine and reported), exact frame
normalization for RESIZE / FIT / FILL / CONCAT before execution, and a media policy that says what is refused,
normalized or delegated. Design records: [docs/decisions.md](docs/decisions.md).

## Non-goals

No editing judgement (which camera, where to cut, why), no speaker / scene detection, no transcription,
diarization or captions, no colour grading, no audio mastering, no thumbnails, no QC beyond output validation,
no ProductionPlan / Project IR / policy / preference / constraint model, no LLM, no agent loop, no MCP, no cloud,
no scripting, no raw ffmpeg or filter interface. See "Explicit non-goals" in the contract (`not_provided`).

**Not implemented in this version** (declared as gaps in `contract.unsupported`, refused with
`UNSUPPORTED_OPERATION`): `CROP` (pixel rectangle), `FREEZE`, `REVERSE`, `IMAGE_INSERT` (still → clip),
`POSITION` (free video-layer placement). ffmpeg-skill 0.9.x has no tool for them and this skill does not run
ffmpeg itself. Multi-track assembly is limited to one video track plus image overlays.

## Architecture

```
request JSON (stdin)
   ↓  project.parse_request      schema, forbidden keys, path policy, operation allowlist, typed params
EditProject                      sources (sha256), operations (operation_id), outputs, topological order
   ↓  timeline.build_timelines   Clip per operation: duration + segments (source range ↔ timeline range)
   ↓  compiler.compile_project   Step per operation: ffmpeg-skill tool + typed flags (ALLOWED_FLAGS)
   ↓  executor.Executor          probe every source → engine capabilities → ranges → media compatibility →
   ↓                             run steps in order → validate each output → record (reuse re-validated)
   ↓  ffmpeg_skill.run_tool      [python, <ffmpeg-skill>/scripts/<tool>.py, argv, --json]  (process group, timeout)
   ↓  response.check_response    self-check of the document against the contract shape (violation = INTERNAL_ERROR)
response JSON (stdout)           {"ok": true, project, execution: {sources[], operations[], outputs[]}}
```

Modules: `timebase` (exact rational time), `paths` (workspace boundary), `operations` (allowlist, params, media
table), `project` (model + graph), `timeline`, `compiler`, `ffmpeg_skill` (engine boundary), `executor`, `response`
(self-check), `contract`, `contract_check`, `doctor`, `cli`, `errors`, `canonical`.

Details: [docs/operations.md](docs/operations.md) (typed model, graph rules, media compatibility, validation, error
classification), [docs/contract.md](docs/contract.md) (versioning, pinned blocks, drift), [docs/security.md](docs/security.md).

## Contract

`video-editing contract --json` (alias `skill --json`) prints the machine-readable contract;
`video-editing contract --check [FILE|-]` verifies it against the implementation and the docs (operation
allowlist, compiler flags, capabilities, unsupported list, media table, error table, execution guarantees) and,
with a saved copy, reports drift, classifying every difference as `[breaking]` (a pinned block or pinned ToolSpec
field changed, a key removed) or `[additive]` (a key added outside the pinned blocks). CI runs it against
`tests/contract/contract.json`; regenerate that file with `video-editing contract --json > tests/contract/contract.json`
when a contract change is intended. The contract prints:
`skill_id`, `version`, `capabilities`, `unsupported`, `tools[]` (ToolSpec fields aligned with
video-production-agent: `tool_id`, `skill_id`, `version`, `required_capabilities`, `inputs`, `produces_output`,
`deterministic`, `result_keys`, plus `media`), `operations` with parameter docs, `media_compatibility`, `graph`,
`validation`, `engine`, `execution`, `request_shape`, `time`, `formats`, `identity`, `provenance`, `errors` (codes,
exit codes, retryable defaults), `response_shape`, `doctor_shape`, `versioning`.

**Versioning** ([docs/contract.md](docs/contract.md)): the blocks `schema, skill_id, version, operations,
unsupported, errors, execution, capabilities, schemas` and the pinned ToolSpec fields are what agents pin and
compare verbatim; a change in them is breaking and bumps the version. New keys outside them are additive and allowed
within a version — this is how 0.1.0 gained `media_compatibility`, `doctor_shape` and the richer execution report
without invalidating the agent's snapshot.

### Input schema (`video-editing/request@1`)

```json
{
  "schema": "video-editing/request@1",
  "project": {
    "id": "demo",
    "sources": [
      {"id": "camA", "path": "in/a.mp4"},
      {"id": "camB", "path": "in/b.mp4"},
      {"id": "logo", "path": "in/logo.png", "kind": "image"}
    ],
    "operations": [
      {"id": "trimA", "type": "TRIM",   "input": "camA",  "params": {"start": "0:01", "end": "0:03.5"}},
      {"id": "fillA", "type": "FILL",   "input": "trimA", "params": {"aspect": "16:9", "width": 640}},
      {"id": "trimB", "type": "TRIM",   "input": "camB",  "params": {"start": {"frames": 25, "fps": 25}, "end": 4}},
      {"id": "fast",  "type": "SPEED",  "input": "trimB", "params": {"factor": 2}},
      {"id": "cat",   "type": "CONCAT", "inputs": ["fillA", "fast"],
                      "params": {"width": 640, "height": 360, "fps": 30, "transition": {"type": "fade", "duration": 0.5}}},
      {"id": "branded", "type": "OVERLAY", "input": "cat", "params": {"image": "logo", "position": "top-right", "start": 0, "end": 2}}
    ],
    "outputs": [{"id": "final", "operation": "branded", "path": "out/final.mp4"}]
  },
  "options": {"timeout_seconds": 600, "overwrite": false, "reuse": true}
}
```

- `sources[].path`: a file under an allowed input root (absolute, or relative to the workspace).
  Video: `.mp4 .mov .mkv .m4v .webm .mts .m2ts .avi .mxf .ts`; image: `.png .jpg .jpeg`.
- `operations[]`: `id` (label), `type` (allowlist), `input` (single-input types) or `inputs` (`CONCAT`), `params`
  (see `contract.operations`). `OVERLAY.params.image` names an image source.
- `outputs[].path`: **relative to the workspace**, `.mp4 .mov .mkv`; never an input, never an existing file unless
  `options.overwrite`. `outputs[].encoding` (optional): `{"crf": 14..28, "preset": "ultrafast" … "veryslow"}` for the
  operation that produces the output (see `contract.encoding`; codec, bitrate modes, audio, pixel format are fixed
  by ffmpeg-skill and reported, not configured).
- `options`: `timeout_seconds` (per tool call, 1..86400, default 3600), `overwrite`, `reuse`.
- The workspace, the allowed input roots and the ffmpeg-skill location are **CLI flags / environment**, not
  request fields.

Times: `12.5`, `"12.5"`, `"1:30"`, `"00:01:30.250"`, `{"seconds": "2.5"}`, `{"rational": "25/2"}`,
`{"frames": 300, "timebase": "1/30"}`, `{"frames": 300, "fps": "30000/1001"}`. All are exact rationals;
serialised as `{"seconds": "12.500000", "rational": "25/2"}`.

### Output schema (`video-editing/response@1`)

```json
{
  "ok": true, "schema": "video-editing/response@1", "skill": {"id": "video-editing", "version": "0.1.0"},
  "status": "completed | reused", "command": "run",
  "engine": {"ffmpeg-skill": "0.9.0", "ffmpeg": "6.1.1", "ffprobe": "6.1.1"},
  "request_sha256": "…sha256 of the canonical request document…",
  "execution": {
    "status": "completed", "started_at": "...Z", "finished_at": "...Z", "work_dir": ".../.video-editing/work",
    "request_sha256": "…", "reused": false,
    "engine": {"id": "ffmpeg-skill", "version": "0.9.0", "root": "…", "tools": ["cut", "fit", "join", "overlay", "probe", "…"], "version_supported": true, "missing_tools": []},
    "sources": [{"id": "camA", "kind": "video", "path": "…", "sha256": "…", "size": 606688,
                 "observation": {"kind": "media.probe", "provenance": "OBSERVED", "source": "ffmpeg-skill/probe@0.9.0", "data": {…}}}],
    "operations": [{
      "operation": "trimA", "operation_id": "op_…", "type": "TRIM", "capability": "video.trim", "status": "completed",
      "skill": "video-editing", "skill_version": "0.1.0", "tool": "ffmpeg-skill/cut", "tool_versions": {"ffmpeg-skill": "0.9.0", "ffmpeg": "…", "ffprobe": "…"},
      "idempotency_key": "…", "parameters": {"start": "1.000000", "end": "3.500000", "accurate": true},
      "inputs": [{"ref": "camA", "kind": "source", "sha256": "…"}], "output": {"path": "…", "sha256": "…"},
      "probe": {"…ffmpeg-skill probe of the output…"}, "provenance": "OBSERVED",
      "commands": ["…ffmpeg command lines ffmpeg-skill ran…"], "started_at": "…", "finished_at": "…", "seconds": 0.67
    }],
    "outputs": [{
      "id": "final", "operation": "branded", "operation_id": "op_…", "path": "…/out/final.mp4", "delivered": true,
      "sha256": "…", "size": 401765, "container": ".mp4", "reused": false,
      "timeline": {"duration_known": true, "duration": {"seconds": "5.500000", "rational": "11/2"},
                   "tracks": [{"id": "V1", "kind": "video", "segments": [
                       {"source": "camA", "source_range": {"start": {…"1/1"}, "end": {…"7/2"}}, "timeline_range": {"start": {…"0/1"}, "end": {…"5/2"}}, "speed": "1/1"},
                       {"source": "camB", "source_range": {"start": {…"1/1"}, "end": {…"4/1"}}, "timeline_range": {"start": {…"2/1"}, "end": {…"7/2"}}, "speed": "2/1"}]},
                              {"id": "V2", "kind": "overlay", "segments": [{"source": "logo", "kind": "image", "timeline_range": {…}}]}]},
      "observation": {"kind": "media.probe", "provenance": "OBSERVED", "source": "ffmpeg-skill/probe@0.9.0", "data": {…}}
    }]
  },
  "project": {"id": "demo", "project_hash": "…", "sources": [...], "operations": [...], "outputs": [...], "options": {...}},
  "warnings": []
}
```

Failure:

```json
{"ok": false, "error": {"code": "INVALID_TIME_RANGE", "message": "...", "retryable": false, "details": {}}}
```

`run` failures additionally carry `status`, `execution` (the failed record with its error, the operations after
it with `status: "skipped"`, `outputs[]` with `delivered: false`) and `project`. Success is never inferred from
the exit code: a run is `completed` / `reused` only when every output was delivered, hashed, probed and validated,
and the whole document passed the response self-check (`response.check_response`); a document that would violate
the contract shape is replaced by an `INTERNAL_ERROR` failure.

## CLI

```
video-editing skill --json                         # contract (alias: contract --json)
video-editing doctor --json [--workspace DIR] [--allowed-input ROOT]...
video-editing validate <request.json | -> --json --workspace DIR [--allowed-input ROOT]...
video-editing plan     <request.json | -> --json --workspace DIR [--allowed-input ROOT]...   # dry run
video-editing run      <request.json | -> --json --workspace DIR [--allowed-input ROOT]...
   [--ffmpeg-skill-dir DIR] [--verbose]
```

- `doctor` is the capability-discovery endpoint: skill id / version, contract schema ids, the engine (ffmpeg-skill
  location, version, ffmpeg / ffprobe, missing capabilities), one row per operation type with `status`
  `AVAILABLE` or `MISSING` and what is missing, `supported_operations` (exactly the AVAILABLE ones — never a
  guess), the declared `unsupported` gaps, `checks`, `problems`. Exit 1 when anything is missing.
- `validate` needs no engine: schema, paths, graph, ids.
- `plan` probes every source (durations, frame, audio; ranges, media compatibility and engine capabilities are
  checked), prints the timeline per step, the idempotency key, whether the step would be reused, and ffmpeg-skill's
  `--dry-run` command preview. **No media is written.**
- `run` executes; `--verbose` prints progress on stderr.
- `--allowed-input` defaults to the workspace. Repeat the flag for several roots.
- `VIDEO_EDITING_FFMPEG_SKILL_DIR` names the ffmpeg-skill checkout; defaults are `~/.claude/skills/ffmpeg-skill`,
  `./vendor/ffmpeg-skill`, `../ffmpeg-skill`.

## Process boundary

One request document on stdin (`-`) or a file, **exactly one** JSON document on stdout under `--json`
(`validate`, `plan`, `run` imply it), diagnostics on stderr only, exit code from the error table:

| code | exit | retryable | meaning |
|---|---|---|---|
| `INVALID_REQUEST` | 2 | no | malformed document, unknown / forbidden keys, bad parameter |
| `INVALID_INPUT` | 3 | no | source unusable (no video stream, no duration, empty, unreadable) |
| `PATH_NOT_ALLOWED` | 4 | no | traversal, symlink escape, outside roots, reserved name, overwrite |
| `UNSUPPORTED_OPERATION` | 5 | no | type not in the allowlist / not implemented |
| `UNSUPPORTED_FORMAT` | 6 | no | container / extension not supported |
| `MISSING_INPUT` | 7 | no | referenced file does not exist |
| `INVALID_TIME_RANGE` | 8 | no | start ≥ end, negative, beyond the input duration, transition too long |
| `DEPENDENCY_ERROR` | 9 | no | unknown reference, cycle, orphan operation, conflicting outputs |
| `TOOL_ERROR` | 10 | yes | the engine failed: ffmpeg error, ffmpeg-skill exit ≠ 0, could not start (missing ffmpeg is `retryable: false`) |
| `OUTPUT_ERROR` | 11 | no | the engine reported success but no readable, non-empty file exists, or the output could not be written |
| `VALIDATION_ERROR` | 12 | no | a file exists but is not what was requested (no video stream, no duration, wrong frame size, duration off) |
| `CANCELLED` | 130 | yes | SIGINT / SIGTERM or `timeout_seconds` |
| `INTERNAL_ERROR` | 1 | no | a bug; still one JSON document, never a traceback on stdout |

## RESIZE, FIT, FILL and the encoding profile

The three frame operations never overlap (`contract.frame_semantics`, ADR-003): `RESIZE` changes the size and keeps
the aspect (`width`, `height = even(width × sh / sw)`); `FIT` changes the aspect and keeps every pixel (padded);
`FILL` changes the aspect and keeps the centre (cropped). FIT / FILL without `width` keep the source width when the
target aspect is not wider than the source, else `even(sh × aspect)`; `even()` is ffmpeg-skill's rule (round, then up
to even). The target frame is computed from the probed source *before* execution, reported as
`normalized.target_frame` in plan steps and operation records, and the output must match it exactly. Rotation
metadata (a display matrix) is honoured; nothing is ever stretched.

`outputs[].encoding` is the whole encoding surface: `crf` (14..28) and `preset` (x264 vocabulary minus placebo),
typed and closed, part of the operation's identity, refused where a stream copy would ignore it. Codec (h264, or hevc
for HDR sources), audio (AAC 192 kb/s), pixel format and container are the engine's and are reported in
`normalized.encoding` / `normalized.video_codec` (ADR-004).

## Operation graph and media compatibility

Requests are graphs: a source or an operation's result may feed several operations, `CONCAT` takes several
inputs, several outputs may be delivered from one request. Cycles, unknown references, duplicates, orphans and
slot mismatches are refused before anything runs; so is media an operation cannot take (every source is probed
first: a video needs a video stream and a duration, an image must decode; `OVERLAY` needs a video input with an
audio stream because ffmpeg-skill 0.9.x's overlay never terminates without one; HDR and SDR are never joined) and an
engine tool / encoder / filter that ffmpeg-skill's doctor reports missing. VFR sources (conformed to CFR) and HDR
sources (encoded HEVC) are reported as warnings. After each operation the output is validated against the
normalized expectation (frame, codec, frame rate, audio presence, duration). `contract.media_policy` states per
situation whether it is refused, normalized by the Skill or delegated to the engine (ADR-005). Full tables:
[docs/operations.md](docs/operations.md).

## Security

See [docs/security.md](docs/security.md). In one paragraph: the request can name files, operations and typed
parameters and nothing else. `command`, `argv`, `shell`, `executable`, `filter`, `env`, `api_key`, `workspace`,
`allowed_input`, `ffmpeg_skill_dir` … are refused at any level before parsing;
values that reach a filter graph are closed vocabularies or integers; outputs are confined to the workspace and
never overwrite inputs; inputs are confined to allowed roots with `..`, symlink escapes and Windows reserved
names refused; the only process launcher is the ffmpeg-skill adapter, argv lists only, process group, scrubbed
environment, timeout. `tests/test_security.py` proves the static properties and attacks the CLI as a black box.

## Provenance

The chain request → operation → execution → engine → output is traceable from one document:
`request_sha256` (hash of the canonical request), `project` (the validated request with `operation_id`s),
`execution.engine` (which ffmpeg-skill, where, which tools), `execution.sources[]` (every source with its sha256 and
its OBSERVED probe), `execution.operations[]` and `execution.outputs[]`. Every operation record carries `skill`,
`skill_version`, `tool`, `tool_versions` (ffmpeg-skill, ffmpeg, ffprobe), `operation_id`, `type`, `capability`,
`inputs[].sha256` (or upstream `operation_id`), `depends_on`, `output.sha256`, `params` (canonical request
parameters), `normalized` (target frame / fps / duration, audio expectation, codec, encoding), `encoding`,
`parameters` (the exact engine flags), `commands` (the ffmpeg command lines ffmpeg-skill ran — recorded for audit,
never an API to replay them), `started_at`,
`finished_at`, `status` (`completed`, `reused`, `failed`, `skipped`). Every output carries its `sha256`, `size`,
`container`, `reused`, its `timeline` (which source range became which timeline range, at what speed) and an
`observation` of kind `media.probe` marked **`OBSERVED`** with source `ffmpeg-skill/probe@<version>`. Request
values are reported under `project`, never as observations.

## Determinism and idempotency

- `operation_id = "op_" + sha256(type, canonical params, input identities)[:16]`, where a source's identity is
  the sha256 of its bytes and an operation's identity is its `operation_id`. Labels, list order, path strings,
  timestamps and time notation (`"1:30"` vs `{"frames": 2700, "fps": 30}`) do not change it; a changed byte or
  parameter changes it and everything downstream.
- `idempotency_key = sha256(operation_id, tool, tool versions, skill version, container[, encoding])`; an encoding
  profile is part of `operation_id` too, only when one was asked for.
- Intermediates live in `<workspace>/.video-editing/work/<operation_id>.<ext>` with a record; a later run whose
  key, size and hash match re-validates them (probe, duration, frame, audio) and reuses them (`status: "reused"`,
  `execution.reused: true`, `outputs[].reused`); a candidate that no longer validates is run again.
  `options.reuse: false` disables it.
- Nothing machine-specific enters an identity: paths, timestamps, the work directory and the engine location are
  reported, not hashed.
- Output settings are explicit (frame precision re-encodes; CONCAT can pin width / height / fps). Encoded
  bytes are `content_equivalent` across encoder builds, as ffmpeg-skill states; within one build the tests
  check byte equality of reused results.

## Error handling, cancellation, failure

Tools run in their own process group; `timeout_seconds` or SIGINT / SIGTERM kills the group and reports
`CANCELLED`. A tool that exits non-zero, or exits 0 without a file, or produces a file whose probe disagrees with
the request (no video stream, zero duration, wrong frame size, duration off by more than the tolerance) is a
failure: the partial file is deleted, nothing is delivered, the record has `status: "failed"` and the error.
Final outputs are written by copy + atomic rename, so a delivered file is always a validated one.

## Development

```
pip install -e . ruff mypy
ruff check src tests && mypy src && python -m compileall -q src tests
video-editing contract --check tests/contract/contract.json        # implementation ↔ contract ↔ golden copy
video-editing contract --json > tests/contract/contract.json       # only for an intended contract change (see docs/contract.md)
```

## Testing

```
cd tests
python -m unittest test_unit test_paths test_security test_engine_contract     # no ffmpeg needed
VIDEO_EDITING_FFMPEG_SKILL_DIR=/path/to/ffmpeg-skill python -m unittest -v test_integration   # real media
```

- `test_unit.py`: time forms and arithmetic, parameter validation, schema / dependency / cycle / orphan errors,
  operation id determinism, source→timeline mapping (trim, cut with reorder, speed, transition overlap,
  trim of a sped clip, unknown durations), stable serialisation, compiler argv, contract consistency and golden
  drift (breaking vs additive), the media table, profile derivation and pre-execution refusals (audio, engine
  gaps), output validation rules, the response self-check (accepts a conforming document, refuses every
  malformed success it is meant to catch), doctor operation availability.
- `test_paths.py`: component containment (POSIX and Windows via `ntpath`), prefix collisions, drive / UNC
  escapes, traversal, reserved names, symlink escape, output rules; on a real Windows file system (CI's
  windows-latest job) also 8.3 short names, case-insensitive spellings, drive-letter outputs, reserved names.
- `test_security.py`: AST scan (no shell / eval / exec / importlib / network, subprocess with lists only), and
  black-box attacks through the CLI: command keys, shell metacharacters, executable and filter injection, argv
  injection through every typed value, environment / boundary keys (`env`, `workspace`, `allowed_input`,
  `ffmpeg_skill_dir`, `api_key` …) at every level, environment references in paths, unknown keys and wrong types,
  traversal and absolute outputs, request-level workspace override, malformed JSON, oversized input.
- `test_engine_contract.py`: the error contract at the engine boundary with a fake ffmpeg-skill (a test double for
  the *boundary only*, never reported as integration): TOOL_ERROR vs OUTPUT_ERROR vs VALIDATION_ERROR vs
  CANCELLED, no partial left behind, plan writes nothing, reuse / invalidation / tampered intermediate,
  unsupported engine version; media compatibility and engine gaps refused before any tool runs, doctor as
  capability discovery, a stale reuse candidate re-run, downstream operations recorded as skipped after a failure,
  sources / engine / request identity in the execution report, a five-operation multi-input graph, and the
  response self-check on every document.
- `test_integration.py` (skipped, never faked, without ffmpeg + ffmpeg-skill): trim, cut + reorder, concat +
  transition + reorder, fill / resize / fit, speed + overlay, the full pipeline (trim → fill → second source →
  concat → validation, plan before run, reuse, chained invalidation), range beyond duration, transition too
  long, corrupt input, still image as video, timeout → `CANCELLED`, doctor; and one real-media E2E per operation
  (`CUT`, `CONCAT`, `CONCAT` with a silent input, `SPEED`, `RESIZE`, `FIT`, `FILL`, `OVERLAY`) asserting file
  existence, size, sha256, duration, streams, frame and timeline, the overlay-without-audio and corrupt-image
  refusals, a cut → speed → resize → overlay chain with two outputs and full reuse, and doctor availability; and the
  media matrix (A video-only through every single-input operation, B video + audio, C mixed audio in both orders,
  D different resolutions, E different frame rates, F a 0.5 s clip, G a 30 s clip with an encoding profile and
  profile-driven reuse / re-run, H unicode paths, I space-containing paths, J a two-chain graph joined by CONCAT,
  K exact reuse and invalidation, L audio-only / image-as-video, M invalid graphs, N traversal and escapes, O a
  truncated file) plus exact frame targets, a real rotated source and an HDR source (hevc, never mixed).
  Fixtures are generated with ffmpeg lavfi at test time; the suite asserts sources are byte-identical afterwards.

CI (`.github/workflows/tests.yml`, on pull requests, pushes to main and manually): lint (ruff, mypy, compileall,
`contract --check`); unit + paths + security + engine-contract on Linux / Windows / macOS × Python 3.9 / 3.11;
integration on Ubuntu with apt ffmpeg and a checkout of kajisho5/ffmpeg-skill.

## ffmpeg-skill relationship

ffmpeg-skill is the media execution engine. This skill uses five of its tools (`probe`, `cut`, `join`, `fit`,
`overlay`) through their public CLI contract (`python3 scripts/<tool>.py … --json`, `status: completed | failed`,
`error.kind`) and its doctor (`scripts/_contract.py doctor --json`: ffmpeg / ffprobe versions, available encoders
and filters), pinned to versions `>=0.9.0,<1.0.0`, located from the environment, never from the request.
Nothing here builds ffmpeg command lines or filter strings; what ffmpeg-skill ran is reported verbatim in
`commands`. Gaps in ffmpeg-skill (pixel crop, freeze, reverse, image-to-clip) are reported as gaps, not worked
around with a direct ffmpeg call. ffmpeg-skill's `render.py` project format is not used: it has a fixed stage
order, no schema validation and no path policy.

## video-production-agent relationship

The agent owns Observation, Event, Context, Inference, Decision, ProductionPlan, Project IR, Compiler, Execution
orchestration, QA, Artifact and Delivery. This skill is an external execution component it can register from
`contract --json` (SkillPackage / ToolSpec field names match) and drive with `run - --json` on stdin like the
documented adapters for media-analysis-skill and transcription-skill. Its results are shaped so the agent can
hash artifacts itself, store `commands` as provenance and map `error.code` / `retryable` without parsing stderr.
The agent's adapter (video-production-agent PR #19) lowers its `video.trim {asset, keep}` IR op onto `CUT` and can
lower every other type from the contract, but its planner emits only `video.trim` today; `CONCAT` and the others
are vocabulary the agent does not yet generate. No agent code is changed by this skill.

## Current limitations

- Operations beyond ffmpeg-skill 0.9.x: `CROP`, `FREEZE`, `REVERSE`, `IMAGE_INSERT`, `POSITION` are not implemented.
- `OVERLAY` needs a video input with an audio stream (ffmpeg-skill 0.9.x limitation, refused up front).
- The encoding surface is `crf` + `preset`; codec, bitrate modes, audio and pixel format are the engine's.
- Real rotation metadata (a display matrix) is honoured; a legacy `rotate` tag is ignored by ffmpeg ≥ 5 and therefore
  by this Skill's probe-based normalization (the frame is then the stored one).
- One video track plus image overlays; no picture-in-picture of video, no audio-only sources, no per-track audio.
- Encoding parameters are ffmpeg-skill's defaults (x264 CRF 18 medium, AAC 192k; HDR sources become HEVC 10-bit).
  A `keyframe` precision `TRIM` / `CUT` may stream-copy and land on a keyframe (tolerance 1.5 s in validation).
- Source durations come from the container; ranges are checked against them with 0.1 s slack.
- `SPEED` computes its target duration from the timeline; the tool retimes to that duration (audio via `atempo`).
- Reuse records are trusted on key + hash; the work directory is not pruned automatically.
- Windows: reserved names, short (8.3) names, case-insensitive spellings and drive letters are handled and tested
  on CI's Windows runner (unit suites); real-media execution on Windows / macOS is not part of CI (ffmpeg is
  installed only on the Ubuntu integration job).

## Future extensions

Contract 0.2.0 (ADR-002, `contract.versioning.next`): `CROP` (pixel rectangle) and `IMAGE_INSERT` (still → timed
clip) once ffmpeg-skill provides typed tools; `RESIZE.height`; `outputs[].encoding` folded into `request_shape`.
Not planned: `FREEZE` (compose from IMAGE_INSERT), `REVERSE`, `POSITION` (would extend OVERLAY with a video layer).
Also: an `OVERLAY` that tolerates silent inputs once ffmpeg-skill's overlay terminates on them.
