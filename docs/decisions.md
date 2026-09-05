# Decisions (ADR)

Short records of the design decisions that shape this Skill. Newer entries refine, never silently reverse, older ones.

## ADR-001 — An execution Skill, never an editor with opinions

**Decision.** video-editing-skill executes typed edit requests; it holds no editing judgement, no AI, no plan, no
policy. The caller (video-production-agent or anything else) decides *what*; this Skill decides only *whether it is
valid* and *how to run it deterministically* through ffmpeg-skill, the single FFmpeg boundary.
**Consequences.** Every input is an allowlisted, typed operation; every FFmpeg detail lives in ffmpeg-skill; gaps in
the engine are reported as gaps (`UNSUPPORTED_OPERATION`, `TOOL_ERROR`), never bridged with a direct ffmpeg call.

## ADR-002 — Contract 0.2.0 candidates: what is worth adding, what is not

**Context.** Five operation types are declared `NOT_IMPLEMENTED` in the 0.1.x contract: `CROP`, `FREEZE`, `REVERSE`,
`IMAGE_INSERT`, `POSITION`. Each was evaluated against five questions: is it a *generic* editing operation, is it
safe at the ffmpeg-skill boundary (a typed tool exists or can exist), can it be a typed model, does it fit the
operation graph, and is it disjoint from the eight existing operations.

| Type | Generic? | Engine boundary (0.9.x) | Typed model | Graph | Disjoint? | Verdict |
|---|---|---|---|---|---|---|
| `CROP` (pixel rectangle) | yes: reframing, removing burnt-in UI, screen-recording regions | **no tool** (fit.py crops only to an aspect ratio) | yes: `{x, y, width, height}` in source pixels, even sizes, inside the frame | one video in, one out | yes (FILL crops to an *aspect*, CROP to a *rectangle*) | **candidate for 0.2.0**, blocked on a typed `crop` tool in ffmpeg-skill |
| `IMAGE_INSERT` (still → timed clip) | yes: title cards, slides, holding frames | **no tool** (join.py needs video inputs; overlay.py composites only) | yes: `{image, duration, frame, fps, audio: silent}` | source image → video-producing operation, then CONCAT | yes | **candidate for 0.2.0**, blocked on a typed still-to-clip tool |
| `FREEZE` (hold a frame) | sometimes: emphasis, end frames | no tool; expressible as a still extracted from a frame + IMAGE_INSERT | yes, but it is a composition of two future primitives | fine | overlaps IMAGE_INSERT | **not planned as a type**; compose it once IMAGE_INSERT exists |
| `REVERSE` | rarely in production editing (effects, not conference / corporate delivery) | no tool | trivial | fine | yes | **not planned**: low value, engine gap |
| `POSITION` (place a video layer, PiP) | yes for PiP / multicam layouts | no tool (overlay.py composites *images*); would need a typed video-on-video compositor | yes: `{layer, position, scale, opacity, start, end}` | two video inputs, one out | overlaps OVERLAY unless OVERLAY grows a video layer | **not planned in this Skill's scope** until ffmpeg-skill has a typed layer tool; then extend OVERLAY rather than add a type |

**Decision.** No new operation type ships in 0.1.x: none can be implemented without building FFmpeg commands here,
which ADR-001 forbids. `CROP` and `IMAGE_INSERT` are the two with clear generic value and clean typed models; they
are the 0.2.0 candidates, each conditional on a typed ffmpeg-skill tool. `FREEZE`, `REVERSE`, `POSITION` are not
planned. Adding a type changes the pinned `operations` / `unsupported` blocks, so 0.2.0 is a version bump with agent
re-pinning (docs/contract.md). The contract's `versioning.next` lists the candidates.

## ADR-003 — RESIZE, FIT and FILL: three disjoint meanings, one normalization rule

**Context.** `RESIZE` (width only), `FIT` and `FILL` (aspect, optional width) all end up in ffmpeg-skill's `fit.py`,
whose frame computation was implicit. An execution Skill must promise the frame before it runs and verify it after.

**Decision.**
- `RESIZE` changes the **size** and keeps the **aspect**: `width = params.width`, `height = even(width * sh / sw)`.
  Nothing is padded, cropped or stretched.
- `FIT` changes the **aspect** and keeps **every pixel**: scale to fit inside the target frame, letterbox /
  pillarbox with `pad_color`.
- `FILL` changes the **aspect** and keeps the **centre**: scale to cover, centre-crop; edges are lost.
- `FIT` / `FILL` target: `width = params.width`, else `sw` when `aspect <= source_aspect`, else `even(sh * aspect)`;
  `height = even(width / aspect)`. `even(n) = round(n)`, +1 when odd — ffmpeg-skill's own rule, reproduced in
  `operations.even`.
- A source with rotation metadata (±90 / 270, a real display matrix) is measured as displayed (`sw, sh` swapped),
  as the engine does.
- No operation stretches / distorts. A `height` for `RESIZE` (exactly one of width / height) is a 0.2.0 candidate:
  it changes the pinned `operations` block.

**Consequences.** The target frame is computed before execution (`plan.steps[].normalized.target_frame`,
`execution.operations[].normalized`) and the output must match it exactly (`VALIDATION_ERROR frame_size`
otherwise); "the engine picks" no longer exists in the contract (`frame_semantics`).

## ADR-004 — Encoding profile: typed, closed, minimal

**Context.** Callers need some control over output quality / speed; ffmpeg-skill 0.9.x exposes exactly `--crf` and
`--preset` on every re-encoding tool and fixes everything else (libx264 High yuv420p bt709, or libx265 10-bit for
HDR sources; AAC 192 kb/s; container by extension).

**Decision.** `outputs[].encoding` is an optional object with two closed parameters: `crf` (integer 14..28) and
`preset` (the x264 preset vocabulary minus `placebo`). It applies to the operation that produces the output, is
part of `operation_id` and `idempotency_key` when present, is refused on outputs of keyframe-precision TRIM / CUT
(stream copy would ignore it) and when two outputs of one operation disagree. Codec, bitrate modes, pixel format,
colour tags, audio codec / bitrate / sample rate, GOP, two-pass and hardware encoders are `not_configurable` and
listed as such in `contract.encoding`. Resolution and frame rate stay with RESIZE / FIT / FILL / CONCAT and their
`fps`.

**Why not more.** Anything beyond crf / preset would either need new engine flags (not in 0.9.x) or turn the Skill
into a generic FFmpeg wrapper (forbidden by ADR-001). Since agents pin `request_shape`, the field is documented in
`contract.encoding.request_field` rather than in `request_shape`; 0.2.0 folds it in.

## ADR-005 — Media policy: refuse, normalize, or delegate — never guess

**Decision.** Every source is probed before execution. The contract's `media_policy` names, per situation, which of
three things happens: **refused before execution** (no video stream, no duration, undecodable image, OVERLAY on a
video without audio, HDR + SDR in one CONCAT, unsupported formats, ranges beyond the input, missing engine
capability), **normalized by the Skill** (times, target frames, SPEED duration, audio expectation, encoding
flags), or **delegated to the engine** (the filter maths, CFR conform of VFR sources, silence insertion and stereo
layout in CONCAT, HEVC 10-bit for HDR sources, AAC). Delegated conversions that change what the caller might expect
(VFR conform, HDR → HEVC) are reported as `warnings`; the output is then verified against the normalized expectation
(frame, fps, audio presence, codec, duration).

An input frame with an odd width or height is refused for the frame-keeping operations (`odd_frame`) because the
engine's encoders need even sizes and the failure would otherwise surface only inside ffmpeg; the frame-changing
operations normalize it. **Why.** A hang or a late FFmpeg failure is the worst outcome for an execution Skill; the overlay-without-audio hang
in ffmpeg-skill 0.9.x is the canonical example. What cannot be promised is refused; what the engine converts on its
own is stated, not hidden.

## ADR-006 — `provides`: publishing our Capability ids for cross-repository discovery

**Decision.** The contract gained one additive top-level block, `provides`, listing this Skill's eight operations
by the same `capability` string `operations.OPERATIONS`/`tool_specs()` already carry (`video.trim`, `video.cut`,
`video.concat`, `video.speed`, `video.fit`, `video.fill`, `video.resize`, `video.overlay`), each paired with its
`tool_id` and a `lifecycle` of `EXPERIMENTAL`. It is derived from `OPERATIONS` — the same source `tool_specs()`
already reads — so it cannot independently drift, and it deliberately excludes `capability_list()`'s two synthetic
entries (`video.transition`, `video.reorder`): those describe a property of `CONCAT`/`CUT`, not something a caller
can request on its own, and `provides` is meant to list only capabilities a planner could target directly.

**Why.** A companion project, `kajisho5/AI-video-production-OS`, is defining a cross-repository contract
(`docs/SPEC.md` `CapabilityContract.provides`) so that a Capability id like `video.trim` can be resolved to a
Provider — this Skill — without an orchestrator hardcoding which of the ten Skill repos implements it. This
Skill already had the hard part: every operation is tagged with exactly this dotted, cross-Skill-shaped
`capability` string in `operations.py` (that field predates this ADR and was never advertised at the top level).
`provides` is the mechanical step of surfacing what already existed, not a new capability or a new judgement about
what this Skill can do.

`lifecycle: EXPERIMENTAL` is not a comment on the operations themselves — `TRIM`, `CUT`, `CONCAT`, and the rest are
the same tested, already-in-production behavior `tool_specs()` has always described, exercised by
`video-production-agent`'s real integration suite. It reflects that the *Capability id* concept this field
publishes is new: as of this ADR, no registry or Agent is known to consume `provides` yet. It should move to
`STABLE` once one does.

This block is additive, not pinned (`contract.py`'s `PINNED_BLOCKS`): nothing outside this repository is known to
depend on it yet, so it is free to change shape without a version bump if the companion project's schema changes
before anything consumes it in practice — exactly the same latitude `media_compatibility`, `graph`, and the other
0.1.x additive blocks already have.

## ADR-007 — `contract_version`: a shape axis independent of the skill's release version

**Decision.** The contract gained a second, independent version field, `contract_version` (starts at `"1.0"`),
alongside the existing `version` (the package's own release version, `"0.1.0"`). `contract_version` moved into
`PINNED_BLOCKS` in place of `version`; `version` moved out of it (and out of `contract_check.py`'s
`PINNED_TOP_FIELDS`). Concretely:

- `version` — this package's release version. Free to change on any release, including one that changes nothing a
  dependent needs to react to (an internal refactor, a bug fix, a new non-breaking operation). `contract_check.py`'s
  `check_saved()` now reports a `version`-only change as *additive* drift, never *breaking*.
- `contract_version` — the version of the *shape* the pinned blocks publish (`operations`, `capabilities`,
  `errors`, `execution`, `schemas`, `unsupported`, `schema`, `skill_id`). Changes only when one of them changes in a
  way a dependent (an Agent, a registry) would need to react to; a dependent pins a range against
  `contract_version`, never `version`.

`contract_version` starts at `"1.0"`, not `"0.1.0"`: the contract shape established when this Skill reached 0.1.0
has had no breaking change since (PR #2's `provides` field and the audit-fix commit were both additive), so there
is exactly one contract shape to number, and `"1.0"` names it — the same convention `ffmpeg-skill` already uses
(`skill.version` 0.9.1, `contract_version` "1.0"; see `kajisho5/AI-video-production-OS` `docs/VERSIONING.md` §1).

**Why.** Before this ADR, `version` did double duty: it was both this package's release identity and the only
signal a change to a pinned block had happened, forcing every future non-breaking release (a bug fix, a new
`0.2.0` operation added additively) to either bump `version` and *look* like a contract-breaking event, or leave
`version` frozen and give dependents no way to tell "the package moved" from "the contract shape moved". This is
exactly the two-axis pattern `kajisho5/AI-video-production-OS` documents as already proven in `ffmpeg-skill`
(`docs/VERSIONING.md` §1) and names as the one per-Skill gap most of the ecosystem still has
(`docs/ROADMAP.md`: "7 of 10 Skills don't publish this field at all today"). Adopting it here needs no change to
`video-production-agent`: its adapter (`check_contract()`) range-checks `version` against `("0.1.",)` and never
reads `contract_version`, so publishing the new field is purely additive to every known caller, and `version`
continuing to report `"0.1.0"` keeps that range check passing unchanged.

`versioning.rule` in the contract states the two-axis rule in full; `doctor --json`'s `contract` block also carries
`contract_version` alongside `version`, since doctor is the other place a caller checks compatibility before
`contract --json`.
