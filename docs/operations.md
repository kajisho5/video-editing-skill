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
| `SPEED` | `input` (video) | **factor** in [1/4, 4], not 1; smooth `blend \| interpolate` (optional) | `ffmpeg-skill/fit` |
| `FIT` | `input` (video) | **aspect** `W:H`, width, pad_color, fps | `ffmpeg-skill/fit` |
| `FILL` | `input` (video) | **aspect** `W:H`, width, anchor `{x, y}` each 0..1, fps | `ffmpeg-skill/fit` |
| `RESIZE` | `input` (video) | **width** (even), fps | `ffmpeg-skill/fit` |
| `OVERLAY` | `input` (video) + `params.image` (image source) | **image**, position (name or `{x, y}`), margin, scale, opacity, start, end, fade | `ffmpeg-skill/overlay` |
| `ROTATE` | `input` (video) | degrees `90 \| 180 \| 270`, flip `h \| v` (at least one required) | `ffmpeg-skill/fit` |
| `FREEZE` | `input` (video) | **hold** time 0.01..600 s, at time `< input duration` (optional) | `ffmpeg-skill/freeze` (optional tool) |
| `REVERSE` | `input` (video) | none | `ffmpeg-skill/reverse` (optional tool) |
| `IMAGE_INSERT` | `input` (image source) | **duration** time 0.01..600 s, width (even), height (even), fps 1..240 (default 30) | `ffmpeg-skill/insert` (optional tool) |

`FREEZE` (ADR-014): with `at`, the frame shown at `at` is held for `hold` and the rest of the clip plays on after it
(`freeze.py --at --hold --mode insert`; the hold is rounded by the engine to whole frames); without `at`, the last
frame is held for `hold` past the end (`--mode extend`). Duration = input + hold; audio is kept and silent under the
hold. To hold the last frame, omit `at` (`at` at or beyond the input duration is `INVALID_TIME_RANGE`). The timeline
inserts a segment of speed `0/1` (one source instant over the hold) and shifts what follows.

`REVERSE` (ADR-014): the input played backwards, audio reversed with it (`reverse.py`, without `--no-audio`). Same
frame, fps and duration as the input. The timeline reports each segment with `reversed: true`: its `source_range`
stays ascending and plays from `end` back to `start`. ffmpeg's `reverse` buffers the whole input in memory; keep it
short (TRIM first). Both tools are optional: `doctor` detects them in the checkout, and without one only that type is
MISSING (refused up front with `TOOL_ERROR`, `missing: ["tool:ffmpeg-skill/<name>"]`).

`IMAGE_INSERT` (ADR-015): one image source (`kind: "image"`) becomes a silent, constant-rate video clip of exactly
`duration` seconds (`insert.py <image> --duration --fps [--width] [--height]`; `--fps` is always passed, 30 when not
asked, so the clip never depends on the engine's default). Frame, as insert.py computes it: both `width` and
`height` → that frame (the still is scaled to cover it and centre-cropped, never stretched); one → the other from
the image aspect, `even()`; none → the image size, `even()`. Its input must be an image source, never a video or an
operation (`DEPENDENCY_ERROR kind_mismatch`); a CONCAT given an image source directly is refused the same way, with a
hint to use `IMAGE_INSERT`. The clip then goes anywhere a video does — typically into `CONCAT`, which inserts silence
under it next to inputs with audio. The timeline holds the still: one segment from the image source, `source_range`
0..0, speed `0/1`, over `duration`. Not mapped: insert.py's Ken Burns `--zoom` / `--zoom-amount` / `--pan`.

Values that reach a filter graph (colours, transition names, positions, aspects) are closed vocabularies or
integers; times are exact rationals. See `contract.operations` for the documented forms.

### RESIZE, FIT, FILL, ROTATE (contract.frame_semantics, ADR-003 / ADR-011)

| Type | Changes | Keeps | Target frame |
|---|---|---|---|
| `RESIZE` | size | the source aspect; nothing padded, cropped or stretched | `width = params.width`; `height = even(width × sh / sw)` |
| `FIT` | aspect | every source pixel (scaled to fit inside, padded with `pad_color`) | `width = params.width`, else `sw` if `aspect ≤ source_aspect` else `even(sh × aspect)`; `height = even(width / aspect)` |
| `FILL` | aspect | the centre by default (scaled to cover, cropped); edges are lost | same rule as FIT |
| `ROTATE` | orientation only (no scaling, padding or cropping) | every source pixel losslessly | `width, height = sw, sh`; swapped when `params.degrees` is 90 or 270 |

`FILL.anchor` (docs/decisions.md ADR-009, 0.2.0): `{x, y}` each `0..1` picks which edge the crop keeps instead of
always the centre (`0`=left/top, `0.5`=centre — the default when `anchor` is omitted, `1`=right/bottom); maps
directly to `ffmpeg-skill fit.py`'s `--crop-x`/`--crop-y` (0.10.0). It changes *what part* of the frame survives,
never the target frame size — the size rule above is unaffected.

`SPEED.smooth` (docs/decisions.md ADR-012, 0.4.0): optional `blend | interpolate`, mapped directly to `ffmpeg-skill
fit.py`'s pre-existing `--smooth` flag (`--smooth none` is `fit.py`'s own default and is never emitted; omitting
`smooth` compiles to the exact argv it always did). `fit.py` only applies it when the computed speed factor is a
slow-down (`factor < 1`, i.e. `SPEED.factor < 1`, a wider `setpts`) — `blend` frame-blends, `interpolate`
motion-interpolates (slower to encode, more fluid); for a speed-up (`factor > 1`) `fit.py` accepts the flag but its
effect is a no-op, exactly as passing it to `fit.py` directly would be. This Skill does not add its own refusal for
that combination: it reproduces `fit.py`'s own behaviour rather than inventing a stricter rule the engine itself
does not enforce.

`ROTATE` (docs/decisions.md ADR-011, 0.3.0): `{degrees?: 90 | 180 | 270, flip?: h | v}`, at least one of the two
required, mapped directly to `ffmpeg-skill fit.py`'s pre-existing `--rotate`/`--flip` flags (rotate applied before
flip, ffmpeg-skill's own convention). Unlike `RESIZE` / `FIT` / `FILL` it changes no pixel's colour, only its
position — a turn and/or a mirror, never a scale, pad or crop — so it groups with `TRIM` / `CUT` / `SPEED` /
`OVERLAY` for the odd-frame refusal below even though its *target frame* can differ from the input's (the
width×height swap a 90/270 turn implies).

`even(n) = round(n)`, +1 when odd (ffmpeg-skill fit.py). `CONCAT` (join.py): `params.width × params.height`; when
only one is given the other follows the first input's aspect (rounded); when none is given the first input's frame;
then floored to even. A source with rotation metadata (a ±90 / 270 display matrix) is measured as displayed; a
legacy `rotate` tag is ignored by ffmpeg ≥ 5 and therefore by the probe. No operation stretches the picture. The
target is reported in `plan.steps[].normalized` and `execution.operations[].normalized` and verified on the output
exactly. Examples: 1280×720 → `RESIZE 300` = 300×170; 640×360 → `FILL 1:1` = 640×640; 1280×720 → `FIT 9:16` =
1280×2276; 640×360 → `FIT 21:9` = 840×360; 641×361 → `FIT 1:1` = 642×642; `CONCAT width 300` of a 640×360 first
input = 300×168; 640×360 → `ROTATE degrees:90` = 360×640; 640×360 → `ROTATE degrees:180` (or `flip:h`) = 640×360.

An input whose frame has an odd width or height (some screen recordings) is refused up front by `TRIM`, `CUT`,
`SPEED`, `OVERLAY` and `ROTATE` (`INVALID_INPUT odd_frame`): they keep the input frame (`ROTATE` only swaps its
two dimensions for a 90/270 turn) and the engine's encoders (libx264 / libx265, yuv420p) need even sizes. `RESIZE`,
`FIT`, `FILL` and `CONCAT` normalize such a source to an even frame.

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
| `OVERLAY` | video + image | frame size, fps and audio as the input |
| `ROTATE` | video | frame size as the input, width/height swapped for a 90/270 `degrees`; audio, fps as the input |
| `FREEZE` | video | frame size, fps as the input; audio as the input, silent under the hold; duration = input + hold |
| `REVERSE` | video | frame size, fps, duration as the input; audio as the input, reversed |
| `IMAGE_INSERT` | one image (no video) | frame by insert.py's rule (params or the image, even); `fps` (default 30); duration = `duration`; no audio |

`OVERLAY` no longer requires audio (ADR-009): ffmpeg-skill 0.9.x's overlay (`-loop 1` image + `-shortest`) could run
unbounded on a video without an audio stream, but ffmpeg-skill >=0.10.0 bounds it with an explicit `-t <video
duration>` instead, verified against real audio-less footage. The floor (`MIN_VERSION`, formerly `SUPPORTED_MIN`) is `0.10.0` accordingly.

Profiles of not-yet-produced intermediates are derived (`EXPECTED`) with the same rules the engine applies (frame
semantics, audio presence, fps, HDR); once an intermediate exists its probe (`OBSERVED`) is used.

### Media policy (contract.media_policy, ADR-005)

| Situation | Handling |
|---|---|
| no video stream (audio-only file, corrupt container) | refused: `INVALID_INPUT no_video_stream` |
| video source without a duration (still image / broken container as video) | refused: `INVALID_INPUT no_duration` |
| image that does not decode | refused: `INVALID_INPUT image_undecodable` |
| TRIM / CUT / SPEED / OVERLAY / ROTATE / FREEZE / REVERSE on an input with an odd width or height | refused: `INVALID_INPUT odd_frame` (RESIZE / FIT / FILL / CONCAT normalize it) |
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
`keyframe` precision); the frame equals the normalized target exactly (RESIZE / FIT / FILL / CONCAT / ROTATE by the
rules above, the input's frame for TRIM / CUT / SPEED / OVERLAY / FREEZE / REVERSE); the video codec is the engine's for the source (h264,
hevc for HDR) when the operation re-encodes; the frame rate is the requested one (± 0.02) when `fps` was given; an
audio stream is present when the input(s) had one. Any failure is `VALIDATION_ERROR` with `details.reason`
(`frame_size`, `aspect`, `codec`, `fps`, `audio_lost`, or the duration details); the partial file is deleted.

On reuse: a work-directory intermediate whose record matches the idempotency key, size and sha256 is re-validated
with the same probe checks; a candidate that no longer validates is discarded and the operation runs again.

## Error classification per operation

| Situation | Code |
|---|---|
| unknown / forbidden key, wrong type, out-of-range parameter | `INVALID_REQUEST` |
| `start ≥ end`, range beyond the input, transition too long, overlay start / end beyond the input, FREEZE `at` at or beyond the input / `hold` outside 0.01..600 s | `INVALID_TIME_RANGE` |
| unknown reference, cycle, orphan, conflicting outputs, image / video slot mismatch | `DEPENDENCY_ERROR` |
| source without video stream / duration, image that does not decode, overlay input without audio, engine says the input is unusable | `INVALID_INPUT` |
| type not in the allowlist (`CROP`, `POSITION`, anything unknown) | `UNSUPPORTED_OPERATION` |
| ffmpeg-skill missing / unsupported version / tool or capability missing (not retryable); ffmpeg failed (retryable) | `TOOL_ERROR` |
| tool exited 0 but wrote nothing / empty / unreadable; output cannot be written | `OUTPUT_ERROR` |
| output exists but is not what was requested (stream, duration, frame, aspect, fps, audio) | `VALIDATION_ERROR` |
| timeout (`retryable: true`) or SIGINT / SIGTERM | `CANCELLED` |
| a bug (including a response that fails the self-check) | `INTERNAL_ERROR` |

## Engine versions and what the tests prove

Originally verified against ffmpeg-skill 0.9.0, 0.9.1 and 0.10.0 with ffmpeg 6.1.1; since ADR-013 there is no upper
bound (`>=0.10.0`) and the real-media suite runs against 0.11.0 and ffmpeg-skill main (see "Across ffmpeg-skill
releases" below). 0.9.1
adds audio-only cut / join modes and a three-state capability doctor (`available` / `missing` / `unknown`); this
Skill uses none of the audio-only modes and treats a capability absent from `available` as missing (conservative: a
detection failure refuses rather than guesses). 0.10.0 bounds `overlay.py --image` with an explicit `-t <video
duration>` instead of `-shortest`, verified (ADR-009) to terminate correctly on a real, audio-less clip — the
reason the `MIN_VERSION` floor no longer allows 0.9.x, which lacks that fix.

### Across ffmpeg-skill releases (ADR-013)

What changed between ffmpeg-skill releases and how this Skill handles each, detected rather than version-gated:

| Change | Where | Handling |
|---|---|---|
| `fit.py --rotate` / `--flip` | added in 0.11.0 | flags read from the checkout; without them `ROTATE` is MISSING in doctor and refused (`TOOL_ERROR`, `flag:--rotate`) before anything runs |
| `reverse.py`, `insert.py`, `freeze.py` | added in 0.11.0, 0.11.0 and 0.16.0 | optional tools (ADR-014, ADR-015): without one, only `REVERSE` / `IMAGE_INSERT` / `FREEZE` is MISSING in doctor (a note; `ok` stays true) and refused (`TOOL_ERROR`, `tool:ffmpeg-skill/<name>`) before anything runs; the `>=0.10.0` floor is not raised for them |
| `insert.py --crf` → `--quality` | as for every tool (2.0) | the same detected spelling: `outputs[].encoding.crf` reaches `IMAGE_INSERT` as `--crf` on 0.11.0, `--quality` on 2.x |
| `crop.py` | present in 0.11.0 and later | not used yet (`CROP` is unsupported here); `doctor` reports whether the checkout has it (`unsupported[].engine_tool_status`) |
| `--crf` → `--quality` (same CRF scale and default) | `--quality` added in 1.10, `--crf` removed in 2.0 | `outputs[].encoding.crf` is passed as `--quality` where the tool lists it, else as `--crf` |
| `fit.py`'s aspect-only canvas (no `width`) | 0.16.10: never exceeds the source (9:16 from 1280x720 is 406x720, was 1280x2276) | behaviour, not a flag: the expected frame follows the rule of the installed release (`fit_bounds_by_source()`) |
| probe `hdr` narrowed to PQ/HLG; `bt2020_or_hdr` keeps the old meaning | 2.0 | `bt2020_or_hdr` is read where present, `hdr` where not: a BT.2020 SDR source still expects HEVC |
| an existing output refused without `--overwrite` | 1.10 warned, 2.0 refuses | never reached: every tool writes a fresh `.partial` path the executor removes first, so `--overwrite` is not passed |
| a dry run refuses an output directory that does not exist | by 1.26.0 (not in 0.16.0) | `plan` creates the work directory for its previews and removes it again (a plan still writes nothing) |
| `_common.py` → `_common/` package | 2.0 | not used: this Skill only runs `scripts/<tool>.py` and `scripts/_contract.py` |

Known upstream defect, not worked around: from ffmpeg-skill 2.4.1 (`join.py`, ffmpeg-skill PR #302, commit d6f3318) a plain-cut
CONCAT whose first clip's audio outruns its picture by under a frame can leave a one-frame timestamp hole at the cut
(166 frames over 5.567 s at `--fps 30`, `avg_frame_rate` 29.82). This Skill's output validation refuses that file
(`VALIDATION_ERROR`, `reason: fps`) instead of delivering it; `tests/test_integration.py::test_pipeline_e2e` shows it.

The HDR fixture in `tests/test_integration.py` is an SDR test pattern **flagged** as HDR (BT.2020 primaries, PQ
transfer tags): it exercises the engine's HDR detection and HEVC path and this Skill's codec / mixing rules, not
real HDR10 / HLG content or tone mapping. The rotation fixture carries a real display matrix (`-display_rotation`);
the VFR path was verified with a frame-dropped source (engine conforms to CFR, a warning is reported).
