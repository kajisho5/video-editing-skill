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
- No operation stretches / distorts.

**Consequences.** The target frame is computed before execution (`plan.steps[].normalized.target_frame`,
`execution.operations[].normalized`) and the output must match it exactly (`VALIDATION_ERROR frame_size`
otherwise); "the engine picks" no longer exists in the contract (`frame_semantics`).

**Correction (found by live verification against a real ffmpeg-skill 0.10.0 checkout, not assumed): `RESIZE.height`
is engine-blocked, not a small addition.** This ADR originally listed a `height` alternative to `RESIZE.width`
(exactly one of the two) as a straightforward 0.2.0 candidate. Re-verifying `fit.py` directly (ffmpeg-skill 0.10.0,
the only single-video geometry tool in the skill; `--width` is its sole size flag, `height` "follows the aspect")
shows there is no engine-side way to request a specific output height: this Skill would have to invert the
width→height formula itself and search for an integer `width` whose fit.py-computed height matches the request
exactly, which is not guaranteed to exist for an arbitrary source aspect ratio, and unwinding it via computed
approximation would break the "the target frame is computed before execution and the output must match it exactly"
guarantee this ADR itself established above. `RESIZE.height` is therefore the same shape of gap as `CROP` /
`IMAGE_INSERT` (ADR-002): blocked on ffmpeg-skill adding a typed `--height` flag to `fit.py`, not implementable here
without building the geometry math ADR-001 assigns to the engine boundary. `contract.py`'s `versioning.next` has
been corrected to say so; it is not withdrawn as a future 0.2.0 item, only reclassified from "ready" to "blocked."

**New 0.2.0 candidate from the same verification pass: `FILL`'s crop anchor.** ffmpeg-skill 0.10.0 added
`--crop-x` / `--crop-y` to `fit.py` (0=left/top, 0.5=centre default, 1=right/bottom, range-checked engine-side) so
`--fit crop` no longer always crops from the centre — relevant because `FILL` (this Skill's crop-to-aspect
operation) currently has no way to say "keep the left third" / "keep the top" when cropping a wide shot to a
narrower aspect loses a subject held off-centre. This is a genuine, currently-unclaimed 0.2.0 candidate (a new
optional `FILL` parameter, e.g. `anchor: {x, y}` mapped straight to the existing engine flags — no new engine
capability needed, unlike `RESIZE.height`) — not decided on or scheduled here; a future session or an explicit
request should make that call.

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

## ADR-008 — `dependencies`: publishing the ffmpeg-skill version range we already enforce

**Decision.** The contract gained one additive top-level block, `dependencies`, with exactly one entry:
`{"skill_id": "ffmpeg-skill", "version_range": ">=0.9.0,<1.0.0"}`. The range string is computed once
(`contract.ffmpeg_skill_version_range()`) from `ffmpeg_skill.py`'s `SUPPORTED_MIN` / `SUPPORTED_MAX_EXCLUSIVE` and
shared with `engine.version_range`, so the two can never say something different from each other or from what
`FfmpegSkill.version_supported()` actually enforces at runtime.

**Why.** `kajisho5/AI-video-production-OS`'s `docs/SPEC.md` §1 sketches `dependencies: [{skill_id, version_range}]`
as an aspirational field every Skill would eventually publish, citing this repository's own adapter pattern as the
existing behavior it generalizes (`docs/MIGRATION_STRATEGY.md`'s `video-editing-skill` row names this exact
field, this exact range, as a small, well-defined per-Skill task — "reflecting the `SUPPORTED_MIN`/`SUPPORTED_MAX`
check that already exists in code"). Publishing it does not add a new guarantee: the runtime check
(`doctor.py`'s `operation_availability()`, `ffmpeg_skill.py`'s `version_supported()`) already refuses to run when
the located `ffmpeg-skill` is out of range; this field only makes that existing fact readable from `contract --json`
without invoking `doctor`.

It also moves one entry of the OS registry's conformance suite (`registry/conformance.py`'s
`check_dependency_version_ranges`, `SKILL_SPEC.md` §8) from `NOT_IMPLEMENTED` (no Skill publishes `dependencies`
yet, an honestly empty check, not a stub) to a real `PASS` for this Skill — verified directly against the real
`registry` library, not assumed.

**Why the range names `ffmpeg-skill`'s own release version, not a `contract_version` range (a known, accepted gap).**
`docs/VERSIONING.md` §2's proposed rule is that a dependency range should be expressed against the dependency's
`contract_version`, never its release version, precisely so a wide span of releases can share one range. This
repository's actual runtime check compares against `ffmpeg-skill`'s release version (`skill.version`), because that
is the only version `ffmpeg-skill`'s own doctor output currently exposes to this Skill; `docs/VERSIONING.md` itself
notes the literal range every adapter hardcodes today is `UNKNOWN` against that ideal. Changing the runtime check to
compare against `ffmpeg-skill`'s `contract_version` instead would be a real, separate piece of work (it would need
`ffmpeg-skill` to expose `contract_version` somewhere this Skill's `doctor`/`locate` step reads, and a change to
`ffmpeg_skill.py`'s actual comparison logic, not just contract metadata) — out of scope here. This field publishes
what is real today, not what the two-axis ideal would eventually look like; this ADR says so explicitly rather
than let the field imply a compatibility guarantee it does not carry yet.

## ADR-009 — 0.2.0: `FILL.anchor` and `outputs[].encoding` formalized in `request_shape`

**Decision.** Release 0.2.0 (`version` `"0.1.0"` → `"0.2.0"`, `contract_version` `"1.0"` → `"2.0"`) ships exactly
two changes, both already fully designed in ADR-003 / ADR-004 and both verified against real ffmpeg-skill 0.10.0
and the one known consumer before merging:

- **`FILL` gains an optional `anchor: {x, y}`** (each `0..1`; `0`=left/top, `0.5`=centre — unchanged default when
  omitted, `1`=right/bottom), mapped straight onto `ffmpeg-skill fit.py`'s `--crop-x`/`--crop-y` (added in 0.10.0;
  see ADR-003's correction). No new ffmpeg-skill capability is required — `crop_x`/`crop_y` parametrize the same
  `crop` filter `--fit crop` already used, so `TOOL_REQUIREMENTS["ffmpeg-skill/fit"]` is unchanged. Verified against
  a real ffmpeg-skill 0.10.0 checkout end-to-end: two `FILL` requests differing only in `anchor` (`x: 0` vs `x: 1`)
  on a source with distinguishable left/right content produced different delivered bytes (`tests/test_integration.py`
  `test_fill_anchor`), not just different compiled flags.
- **`outputs[].encoding` is now named directly in `request_shape`** (`contract.py`'s `request_shape.project.outputs[]`),
  not only in the separate `contract.encoding.request_field` pointer ADR-004 used as a stopgap. No behavior changes:
  the key has been accepted since 0.1.0.

**Why both in one release, and why now.** Neither is urgent alone — `anchor` is a genuine new capability but a
small one; the `encoding` move is purely representational. Bundling them pays the one-time cost of a breaking
release (below) once for both, rather than twice.

**The real cost, stated plainly, not glossed over.** Both changes touch blocks this repository's own tooling treats
as pinned (`operations` is a formal `PINNED_BLOCKS` member; `request_shape` is `also_pinned_by_agents`), so
`contract_check.check_saved()` classifies this release as breaking, not additive — correctly. Concretely, this
means `video-production-agent`'s adapter (`SUPPORTED_SKILL_VERSIONS = ("0.1.",)` in
`src/video_agent/tools/video_editing/adapter.py`) will reject this Skill's `0.2.0` contract via `check_contract()`
until that repository's own maintainers widen the accepted range — a real, external, out-of-repo action this
release depends on and cannot itself perform (ADR-001: never edit `video-production-agent` from here). This is the
anticipated, accepted consequence of a deliberate breaking version bump (docs/contract.md), not an oversight; it
was disclosed and explicitly authorized before this release was cut, rather than discovered by the agent's
maintainers after the fact.

**Not included.** `RESIZE.height` stays out (ADR-003: blocked on ffmpeg-skill, `fit.py` has no `--height` flag);
`CROP`, `IMAGE_INSERT` stay out (ADR-002: blocked on ffmpeg-skill); `FREEZE`, `REVERSE`, `POSITION` remain not
planned. `contract.py`'s `versioning.next` now lists these under `"0.3.0"`.

## ADR-010 — OVERLAY no longer requires audio: `SUPPORTED_MIN` bumped to ffmpeg-skill 0.10.0

**Decision.** `ffmpeg_skill.py`'s `SUPPORTED_MIN` moves from `(0, 9, 0)` to `(0, 10, 0)`. `OVERLAY`'s
`MEDIA["OVERLAY"]["requires"]["audio"]` moves from `True` to `False`: an audio-less video input is no longer refused
before execution (`INVALID_INPUT audio_required`, ADR-005's canonical example of the refuse-before-a-hang policy).

**Why.** ADR-005 refused `OVERLAY` on audio-less inputs because ffmpeg-skill 0.9.x's `overlay.py --image` composites
a looped still (`-loop 1`) with `-shortest`, and `-shortest` cannot bound that looped stream's end without an audio
stream in the mix to help the muxer decide when to stop — the documented failure mode was not merely a few seconds
too long (ffmpeg-skill's own CHANGELOG 0.10.0 entry, "`overlay.py --image` length on FFmpeg 7+", describes exactly
that milder symptom) but an unbounded run. ffmpeg-skill 0.10.0 already fixed the milder case by adding an explicit
`-t <video duration>` after the filter graph — a bound that does not depend on audio being present at all.

Verified directly, not assumed: OVERLAY was run through ffmpeg-skill's own `overlay.py` (0.10.0, unmodified) against
one of the user's real, audio-less 1080p60 H.264 clips (no synthetic fixture) composited with a real photo, at the
actual position/fade/enable-window parameters `video-production-agent` generates, with a generous timeout — it
completed in ~41 s (not a hang) with the output landing at the source's exact duration. A first pass at this
investigation mistook a too-short 40 s timeout for a hang on the same command (the process was still transcoding a
1080p60 frame with a fade filter, not stuck); re-running with a longer timeout was what caught the false positive
before a needless code change shipped — a reminder that "didn't finish by my timeout" and "will never finish" are
different claims, and only ffmpeg's own exit code (or a timeout long enough to be conclusive) tells them apart.
`ffmpeg-skill/tests/test_all.py` gained `test_overlay_on_audio_less_video_terminates` to close the coverage gap
(every prior overlay test used a source with audio); this repository's own `tests/test_unit.py`,
`tests/test_integration.py` and `tests/test_engine_contract.py` overlay-without-audio tests were rewritten from
"refused" to "runs and is bounded" for the same reason.

**Consequences.** `engine.version_range` and `dependencies[0].version_range` (ADR-008) move from `>=0.9.0,<1.0.0` to
`>=0.10.0,<1.0.0` — an additive contract change (`engine`/`dependencies` are explicitly non-pinned blocks), but one
that a consumer comparing against a vendored contract snapshot will see as drift on the `engine` key specifically
(`video-production-agent`'s `contract_drift()` lists `engine` in `DRIFT_KEYS`). `video-production-agent`'s vendored
`contract_0.1.0.json` was updated in the same change so its drift check does not start reporting `video-editing` as
MISSING the moment this lands. Anyone still running ffmpeg-skill 0.9.x now fails `version_supported()` outright
(previously they could run every operation except `OVERLAY` on audio-less input); this is intentional — 0.9.x's
`overlay.py` carries the unbounded-run defect for exactly the input shape this ADR now allows through.

## ADR-011 — 0.3.0: a typed `ROTATE` operation (`fit.py --rotate`/`--flip`)

**Context.** `kajisho5/video-editing-skill#11` is a three-item ecosystem-wide gap inventory against ffmpeg-skill
0.11.0/0.12.2's real current capabilities. Item 1 is `fit.py`'s `--rotate 90/180/270` (clockwise) and `--flip h/v`
flags: unlike `CROP`/`IMAGE_INSERT`/`RESIZE.height` (ADR-002/ADR-003), these predate this Skill's ADR process
entirely — `fit.py` already had them when ADR-002 evaluated `fit.py`'s other flags, but rotate/flip themselves were
never put through the same five questions (generic? safe at the boundary? typed model? fits the graph? disjoint
from existing operations?) until now. Items 2 (`OVERLAY` video-layer + chroma-key, `overlay.py --video`/
`--chromakey`) and 3 (capability ownership for `stabilize.py`/`sequence.py`/`background.py`) are explicitly **not**
addressed by this ADR — they are separate, larger decisions (item 2 touches an existing operation's media model;
item 3 is a cross-repository ownership question, the same shape as `AI-video-production-OS#33`/`#34`) left for a
future session or an explicit request, exactly as ADR-003's `FILL.anchor` note and ADR-002's `POSITION` verdict
were once left open rather than decided speculatively.

**The five questions, applied to rotate/flip.**

| Question | Answer |
|---|---|
| Generic? | Yes: correcting a wrongly-tagged orientation, or mirroring a shot, is ordinary production editing, not a specialised effect. |
| Safe at the ffmpeg-skill boundary? | Yes: `fit.py --rotate {90,180,270} --flip {h,v}` already exists (verified directly against `ffmpeg-skill/scripts/fit.py`) — no new engine tool, no raw filter string. `--rotate` compiles to `transpose=1`/`transpose=2`/`transpose=2,transpose=2`; `--flip` to `hflip`/`vflip`; rotate is applied before flip when both are given. |
| Typed model? | Yes, and a small one: `{degrees?: 90 \| 180 \| 270, flip?: h \| v}`, at least one of the two required (the same "nothing to do" refusal `fit.py` itself raises when neither `--rotate` nor `--flip` nor any other flag is given). |
| Fits the operation graph? | Yes: one video in, one out, like `SPEED`/`FIT`/`FILL`/`RESIZE` — all four already share `ffmpeg-skill/fit` as their engine tool without colliding, because each owns a disjoint parameter set. |
| Disjoint from the eight existing operations? | Yes: `RESIZE`/`FIT`/`FILL` all change the frame's *aspect or size*; `ROTATE` changes only *orientation* (a turn and/or a mirror) and touches no pixel's colour or the frame's aspect ratio on its own terms. |

**Decision — a new operation type, `ROTATE`, not new parameters on `FIT`.** Both were open per the issue text; a
new type was chosen because:

- `FIT`'s entire typed model (`aspect`, `pad_color`, `width`) exists to answer "what should the delivery aspect be
  and what happens to pixels that don't fit it" — a question `ROTATE` does not ask at all. Bolting `rotate`/`flip`
  onto `FIT` would force every rotate-only request to also supply (or default) an aspect it does not want, and
  would make `FIT`'s contract entry document two unrelated concerns under one name.
- The existing precedent in this codebase is one type per orthogonal transform sharing one engine tool: `SPEED`
  (retime), `FIT` (pad-to-aspect), `FILL` (crop-to-aspect) and `RESIZE` (resize-keep-aspect) all compile to
  `ffmpeg-skill/fit` already, each with its own disjoint parameter set and its own row in `contract.operations` /
  `operations.MEDIA` / `FRAME_SEMANTICS`. `ROTATE` extends that established one-type-per-concern pattern rather
  than starting a new "modifier parameters on an unrelated type" convention this codebase has never used.
- Degrees and flip are combined into **one** type rather than two (`ROTATE` + `FLIP`) because `fit.py` runs them as
  one filter chain in one tool invocation (rotate before flip) — splitting them into two typed operations would
  force two full re-encodes for a caller who wants both, for no engine-side benefit; the issue's own title groups
  them as a single unit for exactly this reason ("A typed `ROTATE`/`FLIP` operation").

**Media compatibility and frame semantics — the new nuance ADR-003's frame-keeping group didn't need before this
operation existed.** `ROTATE` keeps every source pixel losslessly, like `TRIM`/`CUT`/`SPEED`/`OVERLAY` (no scaling,
padding or cropping, unlike `RESIZE`/`FIT`/`FILL`) — so it joins that group for the `odd_frame` pre-execution
refusal (`fit.py`'s rotate/flip path does not evenize an odd frame the way its aspect/width path does; an odd input
would only fail once inside the engine's encoder, which ADR-005 refuses up front instead). But unlike the other
four frame-keeping operations, `ROTATE`'s **output frame is not always identical to the input's**: a 90 or 270
degree turn swaps width and height (verified directly against `fit.py`: `if args.rotate in (90, 270): sw, sh =
sh, sw`, computed from the *displayed* size — rotation metadata already swapped, same as every other operation in
this Skill). 180 degrees and a flip alone keep the frame exactly as it is. `operations.FRAME_SEMANTICS["ROTATE"]`
and `executor.target_frame()` state and compute this explicitly; the output is verified against it exactly, the
same "promise the frame, verify the frame" discipline ADR-003 established for `RESIZE`/`FIT`/`FILL`.

**Verified directly, not assumed:** `/ffmpeg-skill/scripts/fit.py`'s `--rotate`/`--flip` argument definitions,
their filter compilation (the `transpose`/`hflip`/`vflip` chain, rotate-before-flip ordering), and the width/height
swap for 90/270 were all read from the actual current ffmpeg-skill checkout before this ADR was written, not
inferred from the CHANGELOG or the issue text.

**Decision.** `ROTATE` ships as a ninth operation type, `capability: "video.rotate"`, `tool: "ffmpeg-skill/fit"`,
`arity: "one"`. `version` `0.2.0` → `0.3.0`, `contract_version` `"2.0"` → `"3.0"` — breaking by this repository's own
convention (a new key in the pinned `operations` and `capabilities` blocks), the same disclosed cost ADR-009
accepted for `FILL.anchor`; `video-production-agent`'s `SUPPORTED_SKILL_VERSIONS` needs the same kind of widening
again. `contract.versioning.next` renames its `"0.3.0"` key to `"0.4.0"` and gains explicit, undecided entries for
issue #11 items 2 and 3, alongside the still-blocked `RESIZE.height` / `CROP` / `IMAGE_INSERT` candidates ADR-002 /
ADR-003 already named. `tests/contract/contract.json` is regenerated in the same change.

## ADR-012 — 0.4.0: `SPEED.smooth`, a new optional parameter on an existing operation (`fit.py --smooth`)

**Context.** `kajisho5/video-editing-skill#13` asks for `SPEED` to surface `fit.py`'s slow-motion quality control.
Read directly from the real ffmpeg-skill checkout (`ffmpeg-skill/scripts/fit.py`, the same discipline ADR-011
followed for `--rotate`/`--flip`), not assumed from the issue text: `--smooth {none,blend,interpolate}` (default
`none`) lives in the `duration` argument group, next to `--method` and `--max-speed`. It is applied inside the
`--method speed` branch only, and only on the slow-down leg of it: after `setpts=1/factor*PTS` is appended to the
video filter chain, `fit.py` checks `factor < 1.0` (a slow-down — `factor` there is `src_duration / target`, the
same direction this Skill's own `SPEED.factor` already uses, verified against `compiler.py`'s existing `_compile`
branch: `args["duration"] = input_duration.scale(1 / factor)`) before appending either
`minterpolate=fps=<src_fps>:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1` (`interpolate`) or
`fps=<src_fps>,tblend=all_mode=average` (`blend`) to `vf`. For a speed-*up* (`factor > 1`, or `factor == 1` with no
retiming at all) `fit.py` accepts `--smooth` without complaint but the flag has zero effect — the branch that reads
`args.smooth` is guarded by `factor < 1.0` and nothing else. `--smooth` has no argparse-level `requires=`/mutual
exclusion with `--method`, `--max-speed`, or anything else `SPEED` already sends; `--method trim` simply never
reaches the code path that reads it, the same "accepted, silently inert" shape as the speed-up case.

**Decision — thread `smooth` straight onto `SPEED`, no new operation type, no cross-field refusal added here.**
Unlike `ROTATE` (ADR-011), this is not a new orthogonal transform: `smooth` only changes *how* the existing
`SPEED.factor` retime is filtered, never what frame or duration comes out, so it belongs on `SPEED` itself, the
same way `FILL.anchor` (ADR-009) was added to `FILL` rather than becoming a new type. `operations.py` gains
`SMOOTH_MODES = ("blend", "interpolate")` and validates `SPEED.params.smooth` with the same `_enum()` helper
`ROTATE.flip` and `FILL.anchor`'s vocabulary checks already use; `"none"` is deliberately not part of the accepted
vocabulary here — omitting `smooth` already means `fit.py`'s own default, so this Skill's typed model has one way
to say "default", not two. `compiler.py` emits `--smooth <value>` on the `ffmpeg-skill/fit` call only when
`"smooth" in p`; an all-default `SPEED` request (no `smooth` key) compiles to byte-identical `argv` to what it
produced before this change (`tests/test_unit.py`'s compiler test asserts this explicitly). This Skill does **not**
add a validation-time refusal for `smooth` given alongside a `factor >= 1` (a combination that is accepted but has
no effect): `fit.py` itself makes no such distinction — it is a legitimate, if inert, request the same way asking
`fit.py` directly for `--smooth blend --duration <shorter>` is, and ADR-001 assigns the engine's own filter-graph
judgement calls to ffmpeg-skill, not to invented stricter rules here.

**Why a version bump, not an additive change.** `operations.py`'s `PARAM_DOCS["SPEED"]` gains a `smooth` key, which
`contract.py`'s `tool_specs()` and `operations` block both surface — `operations` is a formal `PINNED_BLOCKS`
member, so `contract_check.check_saved()` classifies this as breaking, exactly the precedent ADR-009 set for
`FILL.anchor` (also an optional parameter added to an already-shipped operation type, also only a pinned-block
`parameters` key change with no removal or narrowing for an existing caller). `version` moves `0.3.0` → `0.4.0`,
`contract_version` `"3.0"` → `"4.0"`. `video-production-agent`'s `SUPPORTED_SKILL_VERSIONS` needs the same kind of
widening ADR-009 / ADR-011 already required — a known, disclosed, accepted cost, not an oversight.
`contract.versioning.next` renames its `"0.4.0"` key to `"0.5.0"` (`RESIZE.height` / `CROP` / `IMAGE_INSERT` /
issue #11 items 2 and 3 remain exactly as undecided as ADR-011 left them). `tests/contract/contract.json` is
regenerated in the same change.

**Verified directly, not assumed:** `fit.py`'s `--smooth` argparse definition and help text, the `factor < 1.0`
guard, the exact `minterpolate` / `tblend` filter strings, and the absence of any `--smooth` interaction with
`--method`/`--max-speed` were all read from the real ffmpeg-skill checkout (`ffmpeg-skill/scripts/fit.py`) before
this ADR was written. A real-media integration test (`tests/test_integration.py`) runs `SPEED` with a slow-down
factor and both `smooth` values against a real ffmpeg-skill checkout and asserts the two outputs' bytes differ from
each other and from the no-`smooth` baseline — the same "prove different flags produce different delivered bytes,
not just different compiled argv" rigor `test_fill_anchor` established for ADR-009.

## ADR-013 — No ffmpeg-skill version window: detect what the installed release offers

**Context.** `ffmpeg_skill.py` refused any ffmpeg-skill outside `>=0.10.0,<1.0.0`, while CI checked out ffmpeg-skill
main. ffmpeg-skill has since shipped 1.x and 2.x, so every real install was refused (`doctor`: "unsupported version")
and the integration job skipped all 46 tests instead of running them. Raising the ceiling would only move the problem
to the next major. What actually differs between releases, measured against real 0.10.0, 0.11.0, 0.16.0, 1.26.0 and
2.10.3 checkouts:

- `fit.py --rotate` / `--flip` do not exist in 0.10.0 (added in 0.11.0), so ROTATE never worked on 0.10.0 even
  inside the old window;
- `--quality` was added in 1.10 and `--crf` removed in 2.0 (`--crf` is an unknown argument there);
- `fit.py`'s aspect-only canvas changed in 0.16.10 to never exceed the source (a defect fix upstream);
- probe's `hdr` was narrowed to PQ/HLG in 2.0, `bt2020_or_hdr` keeps the old meaning the encoders still route on;
- 1.x refuses a dry run into a directory that does not exist; 2.0 refuses an existing output without `--overwrite`.

**Decision.** No upper bound. Each difference is handled where it lives, by detection where a detection exists:

- **Flags** are read from the installed checkout once per process: `scripts/_contract.py --json`
  (`tools[].input_schema.properties[].cli`, present in every release from 0.10.0 to 2.x), else each tool's
  `--help`. `FLAG_SPELLINGS` maps a renamed flag to its spellings (`--crf` → `--quality`, then `--crf`). A flag no
  spelling of which exists is a gap: the operation type is MISSING in `doctor` (for the flags it always passes,
  `compiler.OPERATION_FLAGS`) and a request using it is refused before anything runs (`TOOL_ERROR`,
  `missing: ["flag:--rotate"]`). Where nothing can be detected, flags pass under their historical spelling, as before.
- **Probe keys**: `bt2020_or_hdr` where present, else `hdr` — the same meaning either way.
- **fit.py's canvas** is behaviour, not a flag, so it is the one rule chosen by the release that changed it
  (`FfmpegSkill.fit_bounds_by_source()`, `>= 0.16.10`; an unparseable version gets the current rule).
- **Overwrite**: not needed — every tool writes a fresh `.partial` path the executor removes first, so `--overwrite`
  is never passed (pinned by `tests/test_engine_compat.py::test_rerun_needs_no_overwrite` against a fake that refuses
  an existing output like 2.x).
- **Dry-run directories**: `plan` creates the missing work directory for its previews and removes what it created.
- **The floor stays** (`MIN_VERSION = 0.10.0`): ADR-010's `overlay.py -t` bound is behaviour no flag reveals.
  `contract.engine.version_range` / `dependencies` become `>=0.10.0` (non-pinned blocks: additive), and
  `engine.compatibility` says what is detected.

`doctor --json` reports what was detected per tool (`engine.compatibility.tools.<tool>.flags_from` = `contract` /
`help`, and the spelling `--crf` resolves to), never a version-derived guess.

**CI.** The integration job is a matrix over ffmpeg-skill 0.11.0 (pinned by commit `b388035`: the oldest release
every operation's flags exist in) and ffmpeg-skill main, and runs daily, so a new ffmpeg-skill release is exercised
without a change here. main runs on `ubuntu-latest` (the newest apt ffmpeg, 8.0 on Ubuntu 26.04); 0.11.0 runs on
`ubuntu-24.04` (ffmpeg 6.1), the ffmpeg of its era, because on ffmpeg 8.0 its `join.py` leaves a one-frame hole at a
cut (`test_pipeline_e2e`: 29.82 fps where 30 was asked) — an engine defect current releases do not have, refused here
as below rather than masked. `tests/test_engine_compat.py` covers the detection offline with fake 0.10 / 0.x / 1.x / 2.x /
help-only checkouts whose tools reject flags they do not list.

**Found, not worked around.** ffmpeg-skill 0.12.0–0.12.x's `probe.py` deletes an input it cannot read (a truncated
MP4), which the integration suite's fixture-integrity check catches (`broken.mp4 was modified`); fixed upstream by
0.16.0. From ffmpeg-skill 2.4.1 (PR #302) `join.py` can leave a one-frame timestamp hole at a plain cut whose first
clip's audio outruns its picture; this Skill's output validation refuses the file (`reason: fps`). Both are engine
defects; neither is masked by loosening a check here.

## ADR-014 — FREEZE and REVERSE via ffmpeg-skill's own tools; the `unsupported` list stops describing the engine

**Context.** `ECOSYSTEM_ROADMAP.md` steps 1–3. The contract's `unsupported` list said "ffmpeg-skill has no
pixel-rectangle crop tool", "no freeze-frame tool", "no reverse tool", "no still-image-to-clip tool" — written against
0.9.x (ADR-002) and never re-read. Checked directly against real checkouts, not the CHANGELOG: `reverse.py`, `crop.py`
and `insert.py` are already in 0.11.0 (the commit CI pins), `freeze.py` arrived in 0.16.0, and current main (2.21.0)
has all four. So the static contract was asserting engine gaps the installed engine did not have — the same mistake
ADR-013 removed for flags and versions.

**Decision 1 — a gap is this Skill's, never the engine's.** `operations.UNSUPPORTED` reasons now say what this Skill
does not map (and why), and each entry carries `engine_tool`: the ffmpeg-skill tool that would execute it
(`ffmpeg-skill/crop`, `ffmpeg-skill/insert`, or `null`). Whether the installed checkout has that tool is detected by
`doctor --json` (`unsupported[].engine_tool_status`: `AVAILABLE` / `MISSING`, read from `scripts/<tool>.py`), the
same way every other engine fact is (ADR-013). `contract_check.verify_implementation` refuses an `unsupported` reason
that claims an engine tool is missing, so it cannot come back.

**Decision 2 — `FREEZE` and `REVERSE` become operations**, reversing ADR-002's "not planned" verdict, which rested on
"no tool" (and, for FREEZE, on composing it from a future IMAGE_INSERT). Both now have a typed, single-call engine tool,
so the five questions answer yes: generic (an end-card hold, a beat; a rewind effect), safe at the boundary (typed
flags, no filter string from a request), a small typed model, one video in / one out, disjoint from every existing
type.

- `FREEZE {hold, at?}` → `freeze.py`. `hold` is a time, 0.01..600 s. With `at` (`0 <= at < input duration`, else
  `INVALID_TIME_RANGE` before execution): `--at <at> --hold <hold> --mode insert` — the frame shown at `at` is held
  and the rest plays on `hold` later. Without `at`: `--hold <hold> --mode extend` — the last frame is held past the
  end. `freeze.py`'s other combinations (`--mode extend` with an explicit `at` at the end, `insert` at the end) are
  not separately expressible: one way to say "hold the last frame". Duration = input + hold (the engine rounds an
  inserted hold to whole frames, inside the 0.35 s validation tolerance); audio is kept, silent under the hold (the
  engine's choice). The timeline splits at `at` and inserts a segment of speed `0/1` (one source instant over the
  hold), shifting what follows.
- `REVERSE {}` → `reverse.py` without `--no-audio`: picture and audio backwards, same frame / fps / duration. The
  timeline reports each segment with `reversed: true` (an additive key; `source_range` stays ascending and `speed`
  non-negative, so the response shape's `N/D` speed rule is unchanged); internally a reversed segment carries a
  negative speed, so TRIM / CUT / SPEED downstream map back to source time through the same formula. ffmpeg's
  `reverse` buffers the whole input in memory; the engine deliberately imposes no length limit, and neither does
  this Skill (a limit would be an editing judgement about what is "too long", ADR-001) — the cost is documented.
- Both keep the input frame, so they join the `odd_frame` refusal group (TRIM / CUT / SPEED / OVERLAY / ROTATE).

**Decision 3 — optional engine tools.** `freeze` and `reverse` are `ffmpeg_skill.OPTIONAL_TOOLS`: a checkout
without one (0.11.0 has no `freeze.py`) still runs every other operation. `doctor` marks only that type MISSING
(`tool:ffmpeg-skill/freeze`) and lists it in a new `notes` array, not in `problems`, so `ok` stays true and the
pinned-0.11.0 CI job's `doctor` step still passes; a request using it is refused before anything runs (`TOOL_ERROR`,
not retryable). Flags are detected for them like for the required tools (`ENGINE_TOOLS`). A *missing flag* on a
present tool (ROTATE on 0.10.0) is unchanged: still a problem.

**Not done here (follow-ups, roadmap steps 4 and 5).** `IMAGE_INSERT` via `insert.py` is not the same size: an image
source must feed a video-producing operation and then CONCAT, which changes the graph's image/video slot rules
(`DEPENDENCY_ERROR kind_mismatch`) and the media model. `CROP` via `crop.py` is a one-tool mapping, but
video-production-agent already calls `ffmpeg-skill/crop` itself, so which repository owns pixel crops is an
ownership decision to record first, not a mapping to add silently. Both stay in `unsupported`, now with an
engine-neutral reason and `engine_tool`.

**Versioning.** `operations`, `unsupported` and `capabilities` are pinned blocks, so this is breaking by ADR-007's
convention and needs a `contract_version` / `version` bump and an agent re-pin. The bump is deliberately left to the
release that ships it (this change was asked not to move the package version); the golden copy
`tests/contract/contract.json` is regenerated so `contract --check` describes the code as it is.

**Verified directly, not assumed:** `freeze.py` / `reverse.py` argparse definitions and their `_contract.py --json`
`input_schema` (`--at`, `--hold`, `--mode {insert,extend}`; `--no-audio`), their filter graphs (`tpad` / `trim` /
`concat` and `apad` for freeze; `reverse` / `areverse`), and which releases ship which script were read from the real
checkouts (2.21.0 and the pinned 0.11.0 commit `b388035`). The real-media tests use a luma-ramp clip whose frame
brightness encodes its source time: the held frames are constant and equal to the source frame before `at`, the clip
resumes `hold` later where it stopped, and output frame `n` of REVERSE matches source frame `179 - n`.

## ADR-015 — IMAGE_INSERT via ffmpeg-skill's `insert.py`: a still becomes a clip the graph can join

**Context.** `ECOSYSTEM_ROADMAP.md` step 4 ("a still can join CONCAT"), the follow-up ADR-014 left open because it is
not a one-tool mapping like FREEZE / REVERSE: every slot in the graph was a video slot except OVERLAY's
`params.image`, and an image source given to CONCAT is refused (`DEPENDENCY_ERROR kind_mismatch`). `insert.py` takes
one still and writes a silent, constant-rate clip; read from ffmpeg-skill main's own contract
(`scripts/_contract.py --json`, 2.22.2: positional `input`, `--duration`, `--width`, `--height`, `--fps` default 30,
`--zoom` / `--zoom-amount` / `--pan`, `--quality` / `--preset`) and from the pinned 0.11.0 commit `b388035` (the same
flags with `--crf`, no `--quality`).

**Decision 1 — a new type, `IMAGE_INSERT {duration, width?, height?, fps?}`, arity one, whose input is an image
source.** It is the one operation that produces video from no video: `parse_request` treats its input as an image
slot (an image source, never a video source or an operation, `kind_mismatch` otherwise), and every other slot
accepts its result like any operation's. CONCAT keeps its video-only slots; an image source given to it directly is
still refused, now with a hint naming IMAGE_INSERT. ADR-002's five questions: generic (title card, end slate,
placeholder), safe (typed flags; the image is a positional path that went through the path policy), small model,
one input / one output, disjoint from OVERLAY (a still *over* video) and from every other type.

- `duration`: a time, 0.01..600 s (the FREEZE.hold bounds — both hold a picture). The clip lasts exactly that, and
  the timeline says so: one segment from the image source, `source_range` 0..0, speed `0/1`, over the duration. Every
  downstream mapping (TRIM, SPEED, REVERSE, FREEZE, CONCAT offsets) already handles a speed-0 segment.
- `width` / `height`: even, as everywhere here; the target frame is insert.py's own rule, computed before execution
  and verified after (both → that frame, the still scaled to cover it and centre-cropped; one → the other from the
  image aspect, `even()`; none → the image size, `even()`). Recorded as a `frame_semantics.rules` entry.
- `fps`: 1..240, default 30 — insert.py's default, but stored in the canonical params and always passed, so the
  clip's rate (and its operation_id) never depends on what an engine release defaults to.
- Not mapped: `--zoom` / `--zoom-amount` / `--pan` (Ken Burns). A motion move is a later, additive parameter if a
  caller needs it; leaving it out keeps the type to "a still as a clip", and the zoompan filter out of
  `required_capabilities`.
- The output is silent and SDR H.264 whatever the image is (insert.py's choice): `profile()` derives `audio: false`,
  `hdr: false`; validation expects exactly that. CONCAT already inserts silence for an input without audio, so a
  card in front of footage with sound keeps the sound (verified on real media below).

**Decision 2 — gated on the detected tool, not on a raised floor.** `insert` joins `ffmpeg_skill.OPTIONAL_TOOLS`
(ADR-014's mechanism): a checkout without `insert.py` (0.10.0) still runs every other type, doctor lists IMAGE_INSERT
MISSING as a note, and a request using it is refused before anything runs (`TOOL_ERROR`,
`tool:ffmpeg-skill/insert`). `MIN_VERSION` stays 0.10.0 — ADR-013: no version window, detect instead. The
encoding profile reaches it as `--crf` or `--quality`, whichever the checkout lists (`FLAG_SPELLINGS`).

**Versioning.** `operations`, `unsupported`, `capabilities` change (pinned), and `media_compatibility` gains the
first `requires.video: false` entry. Breaking by ADR-007's convention, shipped in the same unreleased batch as
ADR-014, so one `version` / `contract_version` bump at release covers both; `video-production-agent` re-pins once.
The golden copy `tests/contract/contract.json` is regenerated.

**Verified directly, not assumed:** the flags above from both checkouts' code and ffmpeg-skill main's contract
output; a real-media test (`tests/test_integration.py::OperationE2ETests::test_image_insert`) against ffmpeg-skill
2.22.2: a 120x40 PNG with `height: 120` becomes a 360x120, 30 fps, silent H.264 clip of 2.0 s whose picture does not
change, and an IMAGE_INSERT 640x360 card CONCATed in front of a 6 s clip with audio gives 7.5 s, 640x360, with audio,
the card then the footage. CI runs the same test against the pinned 0.11.0 checkout.

## ADR-016 — CROP is this Skill's: a typed operation on ffmpeg-skill's `crop.py`, not left to the caller

**Context.** `ECOSYSTEM_ROADMAP.md` step 5 ("CROP: implement or leave to the agent", done when there is no duplicate
ownership). ADR-014 left `CROP` in `unsupported` because video-production-agent already calls `ffmpeg-skill/crop`
itself (its `video_crop` SkillSpec, fed by the `cropdetect_analysis` measurement and a CONFIRM decision), so who owns
pixel-rectangle crops had to be decided before a mapping was added. Two facts decide it:
- `AI-video-production-OS`'s `ecosystem.json`, which gives each job to exactly one repository, gives **尺・画角合わせ**
  (duration and framing) to video-editing-skill. Its roadmap says "Work outside this list belongs to its owner and is
  handed over, not re-implemented". Cutting the frame down to a rectangle is framing, like `FILL`'s aspect crop and
  `RESIZE`, which already live here.
- ADR-001 puts judgement with the caller and execution here. Choosing the rectangle (letterbox detection, a face box)
  is judgement, and the agent keeps it: `cropdetect` stays a measurement it runs. Applying a given rectangle is
  execution, which is this Skill's job.

**Decision 1 — implement `CROP`; the agent's direct `ffmpeg-skill/crop` call is the duplicate.**
`CROP {x, y, width, height}` → `crop.py --x --y --width --height` (+ the typed encoding profile). Retiring
`video_crop` in the agent, or making it lower onto `video-editing/crop` the way it lowers `video.trim` onto `CUT`, is
an agent-side change. It is not made here (this repository never edits the agent), but this ADR is the decision the
agent can follow. Until then the agent's path keeps working: this change removes nothing it uses.

**Decision 2 — exactly the rectangle, or a refusal; never rounded.** This follows crop.py's own contract, read from
the code of both checkouts (2.21.0 and the pinned 0.11.0 `b388035`): `x, y >= 0`, `width, height > 0` and even (4:2:0),
and the rectangle inside the source frame *as displayed* (rotation ±90/270 swaps the measured width and height).
- Here: `x`, `y` are integers `0..8192`; `width`, `height` are even integers `2..8192` (`_even`, the same validator as
  every other frame size).
- *Inside the frame* is checked in `Executor._check_media`, before anything runs, against the input's measured or
  expected frame (an operation's output frame when the input is another operation). An outside rectangle is
  `INVALID_REQUEST`, `details.reason: crop_outside_frame`, with the frame and the rectangle. A request is malformed
  for its input, and nothing about the input is unusable, so it is not `INVALID_INPUT`.
- The output frame is `width × height` exactly (`target_frame`, verified after the run like every frame).
- fps, duration and audio are as the input: crop.py adds no `--fps` unless asked, and none is exposed.
- The timeline mapping is unchanged (a frame-only operation, like `RESIZE` / `ROTATE`).
- An odd input frame is not refused: crop.py writes even output whatever the input, so `CROP` is not in the
  `odd_frame` group.

**Decision 3 — an optional engine tool.** `crop` joins `OPTIONAL_TOOLS` (`crop.py` ships from 0.11.0; the 0.10.0
floor has none). A checkout without it makes only `CROP` MISSING (a doctor note, `ok` stays true), and a request using
it is refused before anything runs (`TOOL_ERROR`, `tool:ffmpeg-skill/crop`, not retryable). `outputs[].encoding.crf`
is passed under the detected spelling, `--crf` on 0.11.0 and `--quality` on 2.x (ADR-013).

**Not done.** No `--fps` on CROP; no automatic rectangle of any kind. `cropdetect` is a measurement, and an
aspect-ratio crop is `FILL`. `POSITION` remains the only `unsupported` entry.

**Versioning.** `operations`, `unsupported` and `capabilities` change (pinned), as do `capability_names` and `tools`
(a new `video-editing/crop`): breaking by ADR-007's convention. As with ADR-014 / ADR-015, the `version` /
`contract_version` bump is left to the release that ships it (this change was asked not to move the package version).
`video-production-agent` must re-pin; its `check_contract()` accepts the new contract. The golden copy
`tests/contract/contract.json` is regenerated.

**Verified directly, not assumed:** crop.py's flags and checks from both checkouts' code (`--x --y --width --height`,
`--crf` on 0.11.0, `--quality` on 2.21.0), and a real-media test (`tests/test_integration.py::OperationE2ETests::test_crop`)
against both. On a 320x240 clip whose left half is white and right half black:
- `{176, 40, 120x160}` gives a 120x160 black clip, with audio and the same duration.
- `{0, 0, 160x240}` gives the white half.
- `{200, 0, 160x240}` is refused before anything runs.
