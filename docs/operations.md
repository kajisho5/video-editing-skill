# Operations: model, graph, media compatibility, validation

## Typed operation model

Field names come from the contract (`operations[<TYPE>].parameters`); unknown keys are `INVALID_REQUEST`, keys that
name an execution escape hatch (`command`, `argv`, `shell`, `filter`, `env`, `api_key`, `workspace`, …) are
`INVALID_REQUEST` with reason `forbidden_key` at any level of the document.

| Type | Inputs | Parameters (required in bold) | Engine tool |
|---|---|---|---|
| `TRIM` | `input` (video) | **start**, **end**, precision `frame` \| `keyframe` | `ffmpeg-skill/cut` |
| `CUT` | `input` (video) | **keep** `[{start, end}, …]` (output order), precision | `ffmpeg-skill/cut` |
| `CONCAT` | `inputs` (2..100 videos) | transition `{type, duration}`, width, height, fps, mode `pad` \| `crop`, pad_color | `ffmpeg-skill/join` |
| `SPEED` | `input` (video) | **factor** in [1/4, 4], not 1 | `ffmpeg-skill/fit` |
| `FIT` | `input` (video) | **aspect** `W:H`, width, pad_color, fps | `ffmpeg-skill/fit` |
| `FILL` | `input` (video) | **aspect** `W:H`, width, fps | `ffmpeg-skill/fit` |
| `RESIZE` | `input` (video) | **width** (even), fps | `ffmpeg-skill/fit` |
| `OVERLAY` | `input` (video) + `params.image` (image source) | **image**, position (name or `{x, y}`), margin, scale, opacity, start, end, fade | `ffmpeg-skill/overlay` |

Values that reach a filter graph (colours, transition names, positions, aspects) are closed vocabularies or
integers; times are exact rationals. See `contract.operations` for the documented forms.

## Operation graph

```
sources ──▶ operations (DAG, topological order, ties by id) ──▶ outputs
   input ─▶ cut ─▶ speed ─▶ resize ─▶ overlay ─▶ output          input1 ─┐
                                                                          ├─▶ concat ─▶ output
                                                                 input2 ─┘
```

Refused before anything runs (`contract.graph.refused`): cycles, unknown input / operation references, duplicate
ids, operations that lead to no output, two outputs on one path, a video slot fed by an image (or an image slot fed
by a video or an operation), unknown operation types, unknown or forbidden keys, media an operation cannot take,
engine tools / encoders / filters that are missing. An operation's result may feed several operations and several
outputs; an output may be any operation, not only a sink.

Identity is content-derived: `operation_id = op_ + sha256(type, canonical params, input identities)[:16]` where a
source's identity is the sha256 of its bytes and an operation's identity is its own `operation_id`. Labels, list
order of unrelated entries, path strings, timestamps and time notation do not change it.

## Media compatibility (`contract.media_compatibility`, `operations.MEDIA`)

Every source is probed by `ffmpeg-skill/probe` before execution. A video needs a video stream and a duration; an
image must decode to a frame. Per operation:

| Type | Requires | Output keeps / becomes |
|---|---|---|
| `TRIM`, `CUT` | video | frame size, fps and audio as the input |
| `CONCAT` | ≥ 2 videos, any sizes / rates | `width × height` and `fps` from params or the first input; audio: stereo AAC when any input has audio (silence is inserted for the others), none when no input has audio |
| `SPEED` | video | frame size as the input; audio as the input (pitch preserved); duration = input / factor |
| `FIT` | video | aspect as requested (width when given, padded); audio, fps as the input unless `fps` |
| `FILL` | video | aspect as requested (width when given, centre-cropped); audio, fps as the input unless `fps` |
| `RESIZE` | video | `width` as requested, height by the source aspect; audio, fps as the input unless `fps` |
| `OVERLAY` | video **with an audio stream** + image | frame size, fps and audio as the input |

`OVERLAY` requires audio because ffmpeg-skill 0.9.x's overlay (`-loop 1` image + `-shortest`) never terminates on a
video without an audio stream; the skill refuses it (`INVALID_INPUT`, reason `audio_required`) instead of hanging
until the timeout. A `CONCAT` with at least one audio-bearing input produces audio and therefore satisfies a
downstream `OVERLAY`.

Profiles of not-yet-produced intermediates are derived (`EXPECTED`) only where the engine promises the fact
(explicit width / height / fps, audio presence); once an intermediate exists its probe (`OBSERVED`) is used.

## Validation

Before execution: schema and typed parameters → path policy → graph → source probes → ranges against probed
durations (0.1 s slack; a transition needs inputs longer than twice its duration) → media compatibility → engine
tools and capabilities (ffmpeg-skill doctor; a gap is `TOOL_ERROR`, not retryable, naming what is missing).

After each operation (`executor._validate`): the file exists, is non-empty and readable; the probe reports a video
stream, a duration and a frame size; the duration is within tolerance of the timeline (0.35 s, 1.5 s for
`keyframe` precision); the frame is the requested size (`FIT` / `FILL` with width, `CONCAT` with width + height),
at the requested aspect (`FIT` / `FILL` without width, ± 2 px), the requested width (`RESIZE`), or equal to the
input (`TRIM`, `CUT`, `SPEED`, `OVERLAY`); the frame rate is the requested one (± 0.02) when `fps` was given; an
audio stream is present when the input(s) had one. Any failure is `VALIDATION_ERROR` with `details.reason`
(`frame_size`, `aspect`, `fps`, `audio_lost`, or the duration details); the partial file is deleted.

On reuse: a work-directory intermediate whose record matches the idempotency key, size and sha256 is re-validated
with the same probe checks; a candidate that no longer validates is discarded and the operation runs again.

## Error classification per operation

| Situation | Code |
|---|---|
| unknown / forbidden key, wrong type, out-of-range parameter | `INVALID_REQUEST` |
| `start ≥ end`, range beyond the input, transition too long, overlay start / end beyond the input | `INVALID_TIME_RANGE` |
| unknown reference, cycle, orphan, conflicting outputs, image / video slot mismatch | `DEPENDENCY_ERROR` |
| source without video stream / duration, image that does not decode, overlay input without audio, engine says the input is unusable | `INVALID_INPUT` |
| type not in the allowlist (`CROP`, `FREEZE`, `REVERSE`, `IMAGE_INSERT`, `POSITION`, anything unknown) | `UNSUPPORTED_OPERATION` |
| ffmpeg-skill missing / unsupported version / tool or capability missing (not retryable); ffmpeg failed (retryable) | `TOOL_ERROR` |
| tool exited 0 but wrote nothing / empty / unreadable; output cannot be written | `OUTPUT_ERROR` |
| output exists but is not what was requested (stream, duration, frame, aspect, fps, audio) | `VALIDATION_ERROR` |
| timeout (`retryable: true`) or SIGINT / SIGTERM | `CANCELLED` |
| a bug (including a response that fails the self-check) | `INTERNAL_ERROR` |
