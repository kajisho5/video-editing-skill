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

### RESIZE, FIT, FILL (contract.frame_semantics, ADR-003)

| Type | Changes | Keeps | Target frame |
|---|---|---|---|
| `RESIZE` | size | the source aspect; nothing padded, cropped or stretched | `width = params.width`; `height = even(width × sh / sw)` |
| `FIT` | aspect | every source pixel (scaled to fit inside, padded with `pad_color`) | `width = params.width`, else `sw` if `aspect ≤ source_aspect` else `even(sh × aspect)`; `height = even(width / aspect)` |
| `FILL` | aspect | the centre (scaled to cover, centre-cropped); edges are lost | same rule as FIT |

`even(n) = round(n)`, +1 when odd (ffmpeg-skill fit.py). A source with rotation metadata (±90 / 270 display matrix)
is measured as displayed. No operation stretches the picture. The target is reported in `plan.steps[].normalized`
and `execution.operations[].normalized` and verified on the output exactly. Examples: 1280×720 → `RESIZE 300` =
300×170; 640×360 → `FILL 1:1` = 640×640; 1280×720 → `FIT 9:16` = 1280×2276; 640×360 → `FIT 21:9` = 840×360.

### Encoding profile (contract.encoding, ADR-004)

`outputs[].encoding` (optional): `{"crf": 14..28, "preset": ultrafast | superfast | veryfast | faster | fast | medium |
slow | slower | veryslow}`. It applies to the operation that produces the output, is part of its identity, is
refused on keyframe-precision TRIM / CUT (`encoding_needs_reencode`) and when two outputs of one operation disagree
(`conflicting_encodings`). Everything else is fixed by ffmpeg-skill and reported in `normalized.encoding` /
`normalized.video_codec`: h264 (libx264, yuv420p, bt709 tags) for SDR sources, hevc (libx265 10-bit) for HDR
sources, AAC 192 kb/s, container by extension. Not configurable: codec choice, bitrate modes, pixel format, colour
tags, audio codec / bitrate / sample rate, GOP, two-pass, hardware encoders.

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

Profiles of not-yet-produced intermediates are derived (`EXPECTED`) with the same rules the engine applies (frame
semantics, audio presence, fps, HDR); once an intermediate exists its probe (`OBSERVED`) is used.

### Media policy (contract.media_policy, ADR-005)

| Situation | Handling |
|---|---|
| no video stream (audio-only file, corrupt container) | refused: `INVALID_INPUT no_video_stream` |
| video source without a duration (still image / broken container as video) | refused: `INVALID_INPUT no_duration` |
| image that does not decode | refused: `INVALID_INPUT image_undecodable` |
| OVERLAY on a video input without audio | refused: `INVALID_INPUT audio_required` (ffmpeg-skill 0.9.x never terminates) |
| CONCAT of HDR and SDR inputs | refused: `INVALID_INPUT hdr_mismatch` |
| unsupported extension / container | refused: `UNSUPPORTED_FORMAT` |
| ranges beyond the input, transition longer than half an input | refused: `INVALID_TIME_RANGE` |
| engine tool / encoder / filter missing | refused: `TOOL_ERROR` (not retryable) |
| times, target frames, SPEED duration, audio expectation, encoding flags | normalized by the Skill, reported, verified |
| scaling / padding / cropping maths, CFR conform of VFR sources (warning), silence insertion and stereo layout in CONCAT, HDR → HEVC 10-bit (warning), AAC | delegated to ffmpeg-skill, verified on the output |

By stream: video-only sources go through every operation but OVERLAY and produce video-only outputs (verified);
video + audio keeps its audio (verified); audio-only sources are refused; images serve OVERLAY only; mixed audio
presence, different resolutions and different frame rates in CONCAT are conformed by the engine (frame / fps from
params or the first input, silence for silent inputs); VFR sources are conformed to CFR; HDR sources are encoded
HEVC and never mixed with SDR; rotation metadata is honoured.

## Validation

Before execution: schema and typed parameters → path policy → graph → source probes → ranges against probed
durations (0.1 s slack; a transition needs inputs longer than twice its duration) → media compatibility → engine
tools and capabilities (ffmpeg-skill doctor; a gap is `TOOL_ERROR`, not retryable, naming what is missing).

After each operation (`executor._validate`): the file exists, is non-empty and readable; the probe reports a video
stream, a duration and a frame size; the duration is within tolerance of the timeline (0.35 s, 1.5 s for
`keyframe` precision); the frame equals the normalized target exactly (RESIZE / FIT / FILL / CONCAT by the rules
above, the input's frame for TRIM / CUT / SPEED / OVERLAY); the video codec is the engine's for the source (h264,
hevc for HDR) when the operation re-encodes; the frame rate is the requested one (± 0.02) when `fps` was given; an
audio stream is present when the input(s) had one. Any failure is `VALIDATION_ERROR` with `details.reason`
(`frame_size`, `aspect`, `codec`, `fps`, `audio_lost`, or the duration details); the partial file is deleted.

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
