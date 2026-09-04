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
| `video.fit` | `FIT` letterbox / pillarbox into an aspect ratio | `ffmpeg-skill/fit` |
| `video.fill` | `FILL` centre-crop into an aspect ratio | `ffmpeg-skill/fit` |
| `video.resize` | `RESIZE` scale to a width | `ffmpeg-skill/fit` |
| `video.overlay` | `OVERLAY` still image at a position for a time range | `ffmpeg-skill/overlay` |

Plus: timeline assembly (segments with source and timeline ranges, a second track for overlays), deterministic
operation identity, dry run, reuse of identical earlier results, output validation, provenance.

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
   ↓  executor.Executor          probe sources → check ranges → run steps in order → validate → record
   ↓  ffmpeg_skill.run_tool      [python, <ffmpeg-skill>/scripts/<tool>.py, argv, --json]  (process group, timeout)
response JSON (stdout)           {"ok": true, project, execution: {operations[], outputs[]}}
```

Modules: `timebase` (exact rational time), `paths` (workspace boundary), `operations` (allowlist + params),
`project` (model + graph), `timeline`, `compiler`, `ffmpeg_skill` (engine boundary), `executor`, `contract`,
`doctor`, `cli`, `errors`, `canonical`.

## Contract

`video-editing contract --json` (alias `skill --json`) prints the machine-readable contract:
`skill_id`, `version`, `capabilities`, `unsupported`, `tools[]` (ToolSpec fields aligned with
video-production-agent: `tool_id`, `skill_id`, `version`, `required_capabilities`, `inputs`, `produces_output`,
`deterministic`, `result_keys`), `operations` with parameter docs, `engine`, `execution`, `request_shape`,
`time`, `formats`, `identity`, `provenance`, `errors` (codes, exit codes, retryable defaults), `response_shape`.

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
  `options.overwrite`.
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
  "execution": {
    "status": "completed", "started_at": "...Z", "finished_at": "...Z", "work_dir": ".../.video-editing/work",
    "operations": [{
      "operation": "trimA", "operation_id": "op_…", "type": "TRIM", "capability": "video.trim", "status": "completed",
      "skill": "video-editing", "skill_version": "0.1.0", "tool": "ffmpeg-skill/cut", "tool_versions": {"ffmpeg-skill": "0.9.0", "ffmpeg": "…", "ffprobe": "…"},
      "idempotency_key": "…", "parameters": {"start": "1.000000", "end": "3.500000", "accurate": true},
      "inputs": [{"ref": "camA", "kind": "source", "sha256": "…"}], "output": {"path": "…", "sha256": "…"},
      "probe": {"…ffmpeg-skill probe of the output…"}, "provenance": "OBSERVED",
      "commands": ["…ffmpeg command lines ffmpeg-skill ran…"], "started_at": "…", "finished_at": "…", "seconds": 0.67
    }],
    "outputs": [{
      "id": "final", "operation": "branded", "path": "…/out/final.mp4", "delivered": true, "sha256": "…", "size": 401765,
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

`run` failures additionally carry `status`, `execution` (records up to the failure) and `project`.

## CLI

```
video-editing skill --json                         # contract (alias: contract --json)
video-editing doctor --json [--workspace DIR] [--allowed-input ROOT]...
video-editing validate <request.json | -> --json --workspace DIR [--allowed-input ROOT]...
video-editing plan     <request.json | -> --json --workspace DIR [--allowed-input ROOT]...   # dry run
video-editing run      <request.json | -> --json --workspace DIR [--allowed-input ROOT]...
   [--ffmpeg-skill-dir DIR] [--verbose]
```

- `validate` needs no engine: schema, paths, graph, ids.
- `plan` probes the sources (durations, ranges are checked), prints the timeline per step, the idempotency key,
  whether the step would be reused, and ffmpeg-skill's `--dry-run` command preview. **No media is written.**
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
| `TOOL_ERROR` | 10 | yes | ffmpeg-skill / ffmpeg failed or missing |
| `OUTPUT_ERROR` | 11 | no | output could not be written |
| `VALIDATION_ERROR` | 12 | no | output exists but is not what was requested (duration, frame size, stream) |
| `CANCELLED` | 130 | yes | SIGINT / SIGTERM or `timeout_seconds` |
| `INTERNAL_ERROR` | 1 | no | a bug; still one JSON document, never a traceback on stdout |

## Security

See [docs/security.md](docs/security.md). In one paragraph: the request can name files, operations and typed
parameters and nothing else. `command`, `argv`, `shell`, `executable`, `filter` … are refused before parsing;
values that reach a filter graph are closed vocabularies or integers; outputs are confined to the workspace and
never overwrite inputs; inputs are confined to allowed roots with `..`, symlink escapes and Windows reserved
names refused; the only process launcher is the ffmpeg-skill adapter, argv lists only, process group, scrubbed
environment, timeout. `tests/test_security.py` proves the static properties and attacks the CLI as a black box.

## Provenance

Every operation record carries `skill`, `skill_version`, `tool`, `tool_versions` (ffmpeg-skill, ffmpeg, ffprobe),
`operation_id`, `type`, `capability`, `inputs[].sha256` (or upstream `operation_id`), `output.sha256`,
`parameters` (the exact flags), `commands` (the ffmpeg command lines ffmpeg-skill ran), `started_at`,
`finished_at`, `status`. Every output carries its `sha256`, its `timeline` (which source range became which
timeline range, at what speed) and an `observation` of kind `media.probe` marked **`OBSERVED`** with source
`ffmpeg-skill/probe@<version>`. Request values are reported under `project`, never as observations.

## Determinism and idempotency

- `operation_id = "op_" + sha256(type, canonical params, input identities)[:16]`, where a source's identity is
  the sha256 of its bytes and an operation's identity is its `operation_id`. Labels, list order, path strings,
  timestamps and time notation (`"1:30"` vs `{"frames": 2700, "fps": 30}`) do not change it; a changed byte or
  parameter changes it and everything downstream.
- `idempotency_key = sha256(operation_id, tool, tool versions, skill version, container)`.
- Intermediates live in `<workspace>/.video-editing/work/<operation_id>.<ext>` with a record; a later run whose
  key and hash match reuses them (`status: "reused"`). `options.reuse: false` disables it.
- Output settings are explicit (frame precision re-encodes; CONCAT can pin width / height / fps). Encoded
  bytes are `content_equivalent` across encoder builds, as ffmpeg-skill states; within one build the tests
  check byte equality of reused results.

## Error handling, cancellation, failure

Tools run in their own process group; `timeout_seconds` or SIGINT / SIGTERM kills the group and reports
`CANCELLED`. A tool that exits non-zero, or exits 0 without a file, or produces a file whose probe disagrees with
the request (no video stream, zero duration, wrong frame size, duration off by more than the tolerance) is a
failure: the partial file is deleted, nothing is delivered, the record has `status: "failed"` and the error.
Final outputs are written by copy + atomic rename, so a delivered file is always a validated one.

## Testing

```
cd tests
python -m unittest test_unit test_paths test_security          # no ffmpeg needed
VIDEO_EDITING_FFMPEG_SKILL_DIR=/path/to/ffmpeg-skill python -m unittest -v test_integration   # real media
```

- `test_unit.py`: time forms and arithmetic, parameter validation, schema / dependency / cycle / orphan errors,
  operation id determinism, source→timeline mapping (trim, cut with reorder, speed, transition overlap,
  trim of a sped clip, unknown durations), stable serialisation, compiler argv, contract consistency.
- `test_paths.py`: component containment (POSIX and Windows via `ntpath`), prefix collisions, drive / UNC
  escapes, traversal, reserved names, symlink escape, output rules.
- `test_security.py`: AST scan (no shell / eval / exec / importlib / network, subprocess with lists only), and
  black-box attacks through the CLI: command keys, shell metacharacters, executable and filter injection,
  traversal and absolute outputs, request-level workspace override, malformed JSON, oversized input.
- `test_integration.py` (skipped, never faked, without ffmpeg + ffmpeg-skill): trim, cut + reorder, concat +
  transition + reorder, fill / resize / fit, speed + overlay, the full pipeline (trim → fill → second source →
  concat → validation, plan before run, reuse, chained invalidation), range beyond duration, transition too
  long, corrupt input, still image as video, timeout → `CANCELLED`, doctor. Fixtures are generated with
  ffmpeg lavfi at test time; the suite asserts sources are byte-identical afterwards.

CI (`.github/workflows/tests.yml`, manual trigger like the sibling skills): unit on Linux / Windows / macOS ×
Python 3.9 / 3.11; integration on Ubuntu with apt ffmpeg and a checkout of kajisho5/ffmpeg-skill.

## ffmpeg-skill relationship

ffmpeg-skill is the media execution engine. This skill uses five of its tools (`probe`, `cut`, `join`, `fit`,
`overlay`) through their public CLI contract (`python3 scripts/<tool>.py … --json`, `status: completed | failed`,
`error.kind`), pinned to versions `>=0.9.0,<1.0.0`, located from the environment, never from the request.
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
The agent's current `video.trim {asset, keep}` IR op maps onto `CUT`; `CONCAT` and the others are additive
vocabulary the agent does not yet emit. No agent code was changed for this skill.

## Current limitations

- Operations beyond ffmpeg-skill 0.9.x: `CROP`, `FREEZE`, `REVERSE`, `IMAGE_INSERT`, `POSITION` are not implemented.
- One video track plus image overlays; no picture-in-picture of video, no audio-only sources, no per-track audio.
- Encoding parameters are ffmpeg-skill's defaults (x264 CRF 18 medium, AAC 192k; HDR sources become HEVC 10-bit).
  A `keyframe` precision `TRIM` / `CUT` may stream-copy and land on a keyframe (tolerance 1.5 s in validation).
- Source durations come from the container; ranges are checked against them with 0.1 s slack.
- `SPEED` computes its target duration from the timeline; the tool retimes to that duration (audio via `atempo`).
- Reuse records are trusted on key + hash; the work directory is not pruned automatically.
- Windows: reserved names and semantics are handled; the test suite's Windows paths run through `ntpath` on any
  OS, but real Windows execution has not been verified in this environment.

## Future extensions

Pixel `CROP`, `FREEZE`, `REVERSE`, `IMAGE_INSERT` once ffmpeg-skill (or an equivalent typed engine) provides
them; explicit encoding profiles per output; audio tracks and video-on-video layers; `contract --check` drift
detection like media-analysis-skill; a lowering adapter from the agent's Project IR to this request format.
