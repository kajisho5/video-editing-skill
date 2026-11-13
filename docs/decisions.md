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

## ADR-017 — ffmpeg-skill 3.0.0: cut.py's new defaults against TRIM / CUT

**Context.** ffmpeg-skill 3.0.0 (`1dc2f52`, its CHANGELOG "3.0.0" and `docs/contract.md` "What 3.0 changed") drops
Python 3.9 and FFmpeg 5.x and changes four `cut.py` defaults; `contract_version` stays 2.0 and no tool or flag this
Skill passes was removed. TRIM and CUT are `cut.py` calls, so each change was checked against them, on real media with
ffmpeg 6.1.1, against 3.0.0, 2.24.1 and the pinned 0.11.0:

- **SDR HEVC is re-encoded as HEVC** (2.x: x264; `--no-keep-hevc` restores x264). A frame-precision TRIM / CUT of an
  SDR HEVC clip (a phone recording) came out HEVC, and output validation refused it: `VALIDATION_ERROR`, "video codec
  hevc is not the engine's h264 for this source" (`reason: codec`). Every other re-encoding tool still writes H.264
  for SDR, so a 3.x TRIM followed by FIT would also have changed codec mid-graph. **This one needed a change.**
- **The MP4 edit list is kept on a stream copy** (2.x shifted timestamps and showed the keyframe's pre-roll). Only a
  keyframe-precision TRIM / CUT stream-copies. Its output now starts at the requested time instead of the earlier
  keyframe. That is the timeline this Skill already reports, and the duration check (1.5 s tolerance for keyframe)
  passes either way. Kept, no flag.
- **`--vfr-guard sampled`**: a copy is judged on measured packet timing instead of the average rate. This is the
  engine's call about whether it may copy. Re-encoding instead of copying is always within a keyframe TRIM's contract
  ("lossless when possible"). Kept, no flag.
- **`precision` / `keyframe_snapped` result keys**: this Skill reads neither from cut.py's reply (it probes the output
  itself). No effect.

**Decision.** Keep this Skill's codec rule (H.264 for SDR, HEVC for HDR, never mixed) rather than adopt cut.py's new
default for one operation type. A re-encoding (frame-precision) TRIM / CUT passes `--no-keep-hevc`, but only where the
installed cut.py offers it (`FfmpegSkill.cut_keeps_hevc_by_default()`: the flag as detected per ADR-013, or, where
flags were not detected, version `>= 3.0.0`). 2.x cut.py has no such flag and would reject it as an unknown argument, so it
is never passed there. A keyframe-precision TRIM / CUT does not get it: a stream copy keeps the source codec in every
release, and any re-encode there is the engine's fallback. The flag is added at argv time (`Executor._engine_args`,
held to `compiler.ALLOWED_FLAGS`), not in the compiled step, because it depends on the checkout and not on the request.
`--no-edit-list` / `--vfr-guard average` are not passed: 3.0's behaviour is the better one for both, and nothing here
depends on 2.x's.

**Version range.** `>=0.10.0` with no ceiling (ADR-013) already admits 3.x, so the range and `dependencies` are
unchanged and video-production-agent need not re-pin for this. `engine.compatibility.detected` (non-pinned) gains one
line; the golden `tests/contract/contract.json` is regenerated.

**Python / FFmpeg.** This Skill stays Python 3.9+. Its unit jobs never run ffmpeg-skill, and its integration jobs
already run Python 3.11 with the runner's apt ffmpeg (6.1 on ubuntu-24.04, 8.0 on ubuntu-latest), both inside 3.0's
floors. No job combined 3.0 with Python 3.9 or FFmpeg 5.x, so no job had to be split.

**CI.** The integration matrix adds 2.24.1 (`a2c8433`, the last 2.x) and 3.0.0 (`1dc2f52`), each pinned by commit, next to
0.11.0 and main. `tests/test_integration.py::MediaMatrixE2ETests::test_sdr_hevc_source_is_reencoded_h264_on_every_engine`
(a libx265 SDR fixture) fails on 3.0.0 without this change and passes on all three. `tests/test_engine_compat.py` gains
a fake `3.x` layout whose cut.py keeps HEVC unless `--no-keep-hevc` is passed, and a 2.x case where the flag must not
be passed.

## ADR-018 — Segment cache and partial re-render for CUT (`options.segment_cache`)

**Context.** ECOSYSTEM_ROADMAP step 8 (field review 2026-11-05): fixing one cut in a long programme re-encoded the whole
CUT, because a frame-precision CUT is one `cut.py --segments ... --accurate` call and its result is reused only as a whole
(ADR-001's operation-level reuse: a changed range is a new `operation_id`).

**Decision.** A new request option, `options.segment_cache` (bool, default false). With it, a frame-precision CUT is
rendered part by part:

1. every keep range is cut alone, `cut.py <input> --start a --end b --accurate` (plus the output's encoding profile and
   `--no-keep-hevc` where the release has it, ADR-017);
2. each part is validated (video stream, duration within the frame tolerance of `b - a`, audio kept when the input has
   it) and stored as `<work>/segments/seg_<key>.<ext>` with a record `{key, sha256, size, probe}`;
3. the parts are joined by stream copy, `join.py <parts...> --transition none --copy` (one part is copied as is), and
   the joined file is validated as every operation output is.

A part's key is `sha256(input identity, range start / end as rationals, tool, tool versions, skill version, container,
encoding, engine release flags)`. The rest of the keep list is not in it, so after one range changes the other parts
keep their keys and are taken from the cache (`status: reused`); only the changed one is encoded. A part is reused when
its file's size and sha256 match its record. The operation's own idempotency key gains `render: segments`, so a segment
render and a single-pass render never stand in for each other. Running the same request twice reuses the operation
itself and writes nothing in the work directory.

**Why an option, default off.** The segment render butts the joins: cut.py's `--segments` declick fade (20 ms either side
of each join) is not applied to a stream-copy join, and the bytes differ from the single-pass render. Turning it on for
everyone would change every existing CUT's output. A caller that edits long programmes iteratively asks for it.

**Fallback.** ffmpeg-skill refuses a copy join whose parts' picture and sound do not line up within 50 ms (found live:
16 kHz AAC parts start their sound 64 ms early, the encoder's priming; at 44.1 / 48 kHz it is 21-23 ms and the join is
accepted). Then the operation is rendered in one pass as without the cache; the parts stay cached, and the record says
`join: single_pass_fallback` with the engine's reason. Nothing is hidden and no tolerance is widened.

**Engine.** `join.py --copy` exists from ffmpeg-skill 2.24.0. It is detected (ADR-013), not version-guessed. On a checkout
without it (the pinned 0.11.0) a request asking for the cache is refused before anything runs (`TOOL_ERROR`,
`flag:ffmpeg-skill/join --copy`). The floor is not raised. `compiler.ALLOWED_FLAGS["join"]` gains `copy`, which only the
executor sets for a segment join.

**CONCAT.** The roadmap names CUT and CONCAT. CONCAT's own join stays one re-encode: it conforms size, frame rate and the
audio layout (stereo), and a transition needs the pixels, so a copy join would change its contract. A CONCAT's partial
re-render comes from its inputs: each input is an operation reused as a whole, or a segment-cached CUT. A copy-join mode
for CONCAT when its inputs already match is left as a follow-up. It is not claimed here.

**Report.** `execution.operations[].segments`: `{mode: segments, join: stream_copy | single | single_pass_fallback,
parts: [{index, range, key, status: encoded | reused, sha256, seconds}], encoded, reused, fallback_reason?}`. In
`plan.steps[].segments`: `[{index, range, key, cached}]`. The contract describes all of it in a new additive block,
`segment_cache`.

**Versioning.** `request_shape.options` gains `segment_cache`. `request_shape` is pinned by agents
(`versioning.also_pinned_by_agents`), so this is breaking by ADR-007's convention. As with ADR-014 to ADR-016, the
`version` / `contract_version` bump is left to the release, and video-production-agent re-pins. A request without the
option compiles and runs exactly as before. The golden copy is regenerated.

**Verified, not assumed.** `tests/test_integration.py::SegmentCacheLongFormTests` builds a 1-hour source (160x90 at 10
fps, 48 kHz AAC) and cuts three ranges (at 10, 30 and 58 minutes). After the middle range changes, the run encodes
exactly one part (`encoded: 1, reused: 2`). The output's framemd5 (picture and sound) equals that of the same changed
request rendered from an empty cache, and a third run is `reused`. Against ffmpeg-skill main (3.2.0); against 0.11.0
the refusal is tested instead. Offline: `tests/test_engine_contract.py::SegmentCacheTests` (fake engine) covers
counting, the repeated request, `reuse: false`, the plan's `cached` flags and the refusal.

## ADR-019 — Keyframe-aware cuts: `precision: auto`, the engine's choice reported per cut

**Context.** ECOSYSTEM_ROADMAP step 9: trimming the head and tail of a 2-8 hour recording should not re-encode it. Until
now a TRIM / CUT was either `frame` (always re-encoded) or `keyframe` (cut.py's lossless mode with its default 0.5 s
tolerance: an in point may move to a keyframe up to 0.5 s away, and validation allows 1.5 s). Neither copies only when the
copy is exact.

**What the engine offers, measured (ffmpeg 6.1.1; a 12 s, 30 fps clip with a closed 30-frame GOP).** `cut.py --tolerance T`
without `--accurate` stream-copies, then measures how far the copy's length lands from the asked one. Past `T` it
re-encodes that cut ("hybrid", `reencode_reason: tolerance`). It judges the length, not the keyframe positions:
- On a keyframe, 2 to 5 s: 3.2.0 lands 0.067 s long (two B-frames past the out point) and 0.11.0 lands 0.147 s long. So
  `T` must be above about 0.15 s for a copy on a keyframe to stay a copy on every release.
- Half a GOP off, 2.5 to 5 s: 0.11.0 / 2.x snap the start to 2.0 and re-encode. 3.x keeps the MP4 edit list (the
  picture starts at 2.5 with the pre-roll hidden) and lands 0.167 s long, so with `T = 0.2` it is a lossless copy.
- No public tool lists keyframe positions. cut.py reports `nearest_keyframes` only after a tolerance miss.

**Decision.** A third precision, `auto`: cut.py's lossless mode with `--tolerance 0.2` (never `--accurate`). A cut whose
copy lands within 0.2 s of the range is a stream copy, and an in point on a keyframe gives the source's packets byte for
byte. Any other cut is re-encoded frame-accurately. Validation uses the frame tolerance (0.35 s), not keyframe's 1.5 s.
Like `keyframe`, it may stream-copy, so an encoding profile on its output is refused (`encoding_needs_reencode`), and
`--no-keep-hevc` / the codec check do not apply. Every keyframe or auto TRIM / CUT record now carries `cuts`: cut.py's
own measured keys (`mode`, `reencoded`, `reencode_reason`, `precision`, `segment_precision` and
`segment_end_snap_seconds` per range, `duration_delta_seconds`, `nearest_keyframes`, `edit_list`, `keyframe_snapped`),
OBSERVED with `source: ffmpeg-skill/cut@<version>`. A key a release does not report is null. 0.11.0, for one, does not
name `mode` for every case.

**Not done (roadmap step 9's second condition).** "One cut between keyframes re-encodes at most two GOPs" is not met.
cut.py re-encodes the whole cut, not the GOPs at its edges. A smart render here would need the keyframe positions
(Velisa ROADMAP, proposal 1 to ffmpeg-skill: cut-point candidates). It would also need to splice an H.264 re-encode into
copied packets with matching parameter sets, which is the engine's job and no tool offers it. Building it here would
mean reading packets and running ffmpeg outside ffmpeg-skill, which ADR-001 / ADR-013 rule out. Proposal to
ffmpeg-skill: a `cut.py` smart-render mode that re-encodes only the boundary GOPs, with its keyframe candidates in
the result. Until then `auto` is the lossless-first choice and says per cut what it did.

**Versioning.** `operations.TRIM` / `CUT.parameters.precision` gain a value (pinned): breaking by ADR-007's convention.
The bump is left to the release (ADR-014 to ADR-016), and video-production-agent re-pins. `compiler.ALLOWED_FLAGS["cut"]`
gains `tolerance`.

**Verified.** `tests/test_integration.py::KeyframeAwareCutTests`, against ffmpeg-skill 3.2.0 and 0.11.0:
- 2 to 5 s with `auto`: every video packet of the output is the source's, in order, byte for byte (packet md5s), and
  `mode` is `copy` where the engine reports it.
- 2.5 to 5 s: the report agrees with the packets (copy exactly when the packets are the source's, `reencoded` otherwise).

## ADR-020 — PUNCH_IN: a hard-cut zoom on a subject box the caller gives

**Context.** ECOSYSTEM_ROADMAP step 10: the usual punch-in on the speaker, a zoom on the face for a range, following
detection-skill's face box. ADR-001 still holds. Detection, and choosing which jump cuts get a punch-in ("used at a
share of jump cuts"), are the caller's: video-production-agent reads detection-skill and decides. This Skill takes the
box and the range as typed values.

**Decision.** A new operation type `PUNCH_IN {start, end, scale (1, 4], subject {x, y, width, height}}` (capability
`video.punch_in`). The window is `floor_even(W / scale) x floor_even(H / scale)`. It is centred on the subject box, then
moved only as far as needed to stay inside the frame, with even offsets where the box allows (`operations.punch_window`).
A box outside the frame (`subject_outside_frame`) or larger than the window (`subject_larger_than_window`, since it could
not stay in view) is refused before execution. The engine calls, each an ffmpeg-skill tool:
1. `cut.py --accurate` cuts out the part before `start`, the range, and the part after `end`;
2. `crop.py` crops the range to the window;
3. `fit.py --aspect W:H --fit crop --width W` scales it back to the input frame (the window's aspect is the frame's to
   within one even step, so at most a pixel row is cropped and nothing is stretched);
4. `join.py --transition none --width W --height H --fps <input fps>` joins the three parts.
The intermediate files are removed. The frame size, frame rate and duration are the input's. The timeline mapping is
unchanged (as for CROP). The record and the plan carry `punch_in: {window, frame, subject, scale, transition: cut,
range}`.

**Not done.** "Or a short ease" is not provided, because an ease is a crop window that moves over time. `crop.py` takes
one rectangle and `fit.py --fit crop` one position, which is Velisa's proposal 2 to ffmpeg-skill (a time-varying crop).
Until it exists, the punch-in is a hard cut in and out, the common form. The sound passes through `join.py`, so it comes
out stereo AAC (as from CONCAT), not the input's layout. That is stated in `media_compatibility.PUNCH_IN`.

**Engine.** It needs crop.py (optional tool, ffmpeg-skill 0.11.0+). On a checkout without it, doctor reports PUNCH_IN
MISSING as a note, like CROP, and a request is refused before execution. cut / fit / join flags are checked per request.

**Versioning.** A new type in `operations`, `capabilities` and `tools` (pinned): breaking by ADR-007's convention. The
bump is left to the release, and video-production-agent re-pins and needs a lowering for it.

**Verified.** `tests/test_integration.py::PunchInTests`, against ffmpeg-skill 3.2.0 and 0.11.0. A 320x180 clip has a red
40x40 "face" at (40, 60), left of centre. A 2x punch-in from 1 s to 3 s gives the window (0, 34, 160x90). In decoded
frames at 1.05, 1.5, 2.0, 2.5 and 2.95 s, the box is about 80x80 at (80, 52), fully inside the frame and away from every
edge. At 0.5 and 3.5 s the source picture is unchanged. The output stays 320x180 and 4 s.

## ADR-021 — One edit, several aspects: a request graph, not a new type

**Context.** ECOSYSTEM_ROADMAP step 11: ship the landscape programme and the vertical / square versions from one edit,
in one request. Each shot of the vertical version is cropped with one window per shot taken from detection-skill's
subject path.

**Decision.** No new operation type. The graph already expresses it, and a dedicated type would duplicate CROP and
CONCAT (ADR-016's "no duplicate ownership"). The cut list is one TRIM (or CUT) per shot. Each shot is shared by every
version: the graph runs it once, and its intermediate is reused. Per aspect, one CROP per shot takes the window the
caller computed from the subject path (ADR-001: choosing the window is the caller's), and a CONCAT with that aspect's
`width` / `height` makes the delivery. The outputs are three `outputs[]` of one request. `examples/multi-aspect.json`
is the template (16:9, 9:16 and 1:1 from a 1920x1080 talk).

**What holds.** Every window is checked against its shot's measured frame before anything runs (`crop_outside_frame`).
The three outputs come from the same shots, so they have the same duration to within a frame. The sound is the same
source ranges in every version, but each CONCAT encodes it again. It is not bit-identical across outputs (decoded
frames differ, measured). A single bit-identical track shared by all three would need a mux step (picture from one
file, sound from another), and no typed operation does that yet. One window per shot is the rule: a window that moves
within a shot needs a time-varying crop (Velisa proposal 2 to ffmpeg-skill).

**Versioning.** Nothing in the contract changes. This ADR, an example, and a real-media test.

**Verified.** `tests/test_integration.py::MultiAspectTests`, against ffmpeg-skill 3.2.0 and 0.11.0. A 6 s 320x180 clip is
cut into three shots (5 s in all) and rendered as 320x180, 180x320 (90x160 windows) and 180x180 (180x180 windows):
- the three outputs are equal in duration to within one frame;
- the three TRIMs run once;
- every CROP window lies inside its measured source frame;
- a window past the right edge is refused before anything runs.

## ADR-022 — AI-generated clips: declared by the caller, conformed by CONCAT, marked downstream

**Context.** ECOSYSTEM_ROADMAP step 12: generated clips (24 fps, untagged, silent, any pixel shape) join edits. They need
to arrive without a format mismatch and without a missing AI label.

**Decision.**
- **The declaration.** A source takes an optional `ai_generated: {generator, licence}`, each 1 to 200 printable
  characters. It is the caller's statement and is never inferred from the media (ADR-001: no detection here). It is
  reported with `provenance: DECLARED`.
- **The mark.** Every operation record and delivered output whose result contains a declared source carries
  `marks: ["AI_GENERATED"]` and `ai_generated: [{source, generator, licence, provenance: DECLARED}]`. The list comes from
  the operation's reachable sources (`source_refs`). `execution.sources[]` repeats the declaration. Results without such
  a source carry neither key.
- **Not part of identity.** Identity is the bytes, and the declaration is metadata about them. The mark is recomputed on
  every run, so a reused result is marked by the current request. Writing the mark into the container metadata of the
  delivered file (ffmpeg-skill `metadata`) is not done: the contract's provenance is the record of truth, and a later
  step can add a container tag.
- **Normalisation is CONCAT's.** `join.py` already conforms every input:
  - frame size: `params.width` / `height`, with `mode` pad or crop;
  - frame rate: `params.fps`, else the first input's;
  - square pixels: `setsar=1`;
  - BT.709 tags on the H.264 output;
  - a silent track for an input without sound when another input has sound.

  No separate normalise step is added. The caller should set `width` / `height` / `fps` on the CONCAT, so that the edit's
  format decides and not whichever clip comes first.

**Engine finding (not masked).** `join.py` writes its frame rate with `fps={fps:g}`, so 30000/1001 (given, or taken from
the first clip) comes out as `2997/100`. The frame count and duration are right: 0.003 % apart from 30000/1001, about 4
ms an hour. The rate tag is not the NTSC rational a broadcast delivery expects. Proposal to ffmpeg-skill: carry the
rate as a rational (`--fps 30000/1001`, and the probe's `r_frame_rate`) through to the filter. This Skill passes what
the engine accepts (a float).

**Versioning.** `request_shape.project.sources` gains `ai_generated` (agent-pinned), so this is breaking by ADR-007's
convention. The bump is left to the release, and video-production-agent re-pins. There is a new additive contract block,
`ai_generated`.

**Verified.** `tests/test_integration.py::GeneratedClipTests`, against ffmpeg-skill 3.2.0 and 0.11.0:
- **Input.** A 3 s 480x360 clip at 24 fps with 4:3 pixels, no colour tags and no sound, joined between two 29.97 fps
  BT.709 clips with sound, with CONCAT `640x360 @ 30000/1001`.
- **Format.** 640x360, square pixels, BT.709 tags, a sound track throughout.
- **No drift.** Duration and frame count equal the sum of the measured inputs to within one frame, and the reported
  timeline places the third clip exactly there.
- **Marks.** The output and the CONCAT record carry the mark with the declared generator and licence.
`tests/test_engine_contract.py::GeneratedSourceMarkTests` checks that the mark reaches only results that contain the
source, and that the declaration does not change operation ids.

## ADR-023 — Stinger transition: butt join, the stinger composited over each join, the switch at its cut point

**Context.** ECOSYSTEM_ROADMAP step 13. A stinger is a graphic with alpha that covers the whole picture for a moment, and
the edit cuts underneath it. It is an editing transition here, not live switching or playout. Velisa does neither.

**Decision.** CONCAT gets `transition: {type: stinger, stinger: <video source id>, cut_point: time, cover: {start, end}}`.
- **The stinger file.** A source file, not an operation: an encode would lose the alpha. It must have an alpha plane
  (refused otherwise: `stinger_no_alpha`), and the edit's aspect and frame rate. Its length is the file's. As OVERLAY's
  image is, the stinger is the operation's last input, so its bytes are part of the operation's identity and provenance.
- **`cut_point` and `cover`.** Data from the stinger's maker. stinger.json is what hyperframes-skill step 15 will write
  beside the file: the cut point and the fully covered window. The caller reads stinger.json and passes both values;
  this Skill does not open sidecar files. Without either, the request is refused by schema. A cut point outside the
  cover is `stinger_not_covering`, and one that is not on a frame of the stinger is `cut_point_not_on_frame`.
- **Placement.** The clips are butt-joined. At each join, the cut is the first frame of the next clip, and the stinger
  starts `cut_point` before it, so the stinger's covered frame lies on the cut frame. Each input must be at least as
  long as the lead-in (before its join) and the tail (after it).
- **Engine calls**, each an ffmpeg-skill tool:
  1. `join.py`, no transition, with the CONCAT's size / rate / mode;
  2. `overlay.py --video <stinger> --place 0-<len>@<start>`, once per join (alpha respected, measured below);
  3. with sound, `broll.py --insert <stinger> --at <start> --duration <len> --audio mix` per join, because overlay.py drops
     a layer's sound;
  4. then `audio.py --replace`, which puts that mix onto the composite.

  `broll` and `audio` join `OPTIONAL_TOOLS`, so their flags are detected. A checkout without overlay `--place` (0.11.0)
  or without broll is refused before execution.
- **Report.** The record and the plan carry `stinger: {source, cut_point, cover, duration, audio, joins: [{cut_at,
  stinger_start, stinger_end}]}`. The output timeline gets an overlay-track segment `{kind: stinger, cut_at,
  timeline_range}` per join, so the timeline reports the cut at that frame.

**Not done (left unchecked in the roadmap).** Superseded by ADR-032: the cover at the cut point is now measured on the stinger's
own frame (ffmpeg-skill look.py writes it with its alpha), and the step is ticked.
- **Full cover is declared, not measured.** No ffmpeg-skill tool reports a stinger's alpha at one frame. export.py's
  alpha statistics are internal to its own verification, and measuring here would mean running ffmpeg outside
  ffmpeg-skill. So "a stinger that does not cover the frame there is refused" holds only against the declared cover
  window.
- **No qc-skill check exists yet.** "qc-skill's stinger cut-point check passes on the result" cannot be met: qc-skill
  (1.0.0) has no stinger cut-point check yet (Velisa ROADMAP lists it as a qc-skill step). The real-media test makes the
  same measurement itself, on decoded pixels.

**Versioning.** `operations.CONCAT.parameters.transition` changes (pinned), and the `engine.tools_optional` list gains
broll and audio (agent-pinned): breaking by ADR-007's convention. The bump is left to the release, and
video-production-agent re-pins. There is a new additive block, `stinger`.

**Verified.** `tests/test_integration.py::StingerTests`, against ffmpeg-skill 3.2.0 (0.11.0: refused, tested).
- **Fixture.** A red and a blue 2 s clip at 30 fps, and a 1.2 s ProRes 4444 stinger with sound. The stinger covers the
  left half throughout and the whole frame only at its frame 15 (0.5 s). The cut point is 0.5 s and the cover is [0.5,
  16/30).
- **Before and after.** Frame 44 is all red and frame 81 all blue: the clips are untouched outside the stinger.
- **Around the cut.** Frames 45 and 59 show the bar over red. Frame 60, the cut at 2.0 s, is all stinger. Frames 61 and
  80 show the bar over blue. So the switch is at exactly frame 60 and happens under full cover.
- **Report.** The timeline reports `cut_at` 2/1 and the stinger start 3/2.
- **Sound.** The mix is louder under the join than in the clip alone.
- **Refusals.** A cut point outside the cover is refused before anything runs. Offline (fake engine), the no-alpha,
  off-frame, too-long, and operation-as-stinger refusals are covered too.

## ADR-024 — Retake proposals: rules over word timings, proposed with reasons, never applied

**Context.** ECOSYSTEM_ROADMAP step 14 (added on the owner's instruction, 2026-11-06, with steps 15 to 18). Talks and
interviews are cleaned up by cutting retakes, broken-off words, repeats and hesitation words. ADR-001 keeps editing
judgement out of this Skill. This step therefore produces proposals only: deterministic rules, each finding stated with
its reason. A person (or the agent, on a person's behalf) approves them by id, and only approved proposals reach an edit,
as CONDENSE segments (step 16). This repository never applies a proposal by itself.

**Decision.** A new command, `video-editing propose` (request `video-editing/propose@1`, response
`video-editing/proposals@1`, contract block `proposals`). With `retakes: {transcript, profile?, min_confidence?}`, it
reads a transcript with word timings from an allowed input root (`{words: [{word | text, start, end}]}`, in time order,
validated). It writes nothing; the response says `applied: false`. The rules:
- **repeat:** the same 1 to 4 words twice in a row. The first saying is proposed, up to where the second starts.
- **restart:** a phrase of 2+ words said again within 8 words and 4 s, with only hesitation words or broken-off words in
  between. A 3+ word opening said again after at most 3 words also counts. "in the morning and in the evening" is not a
  restart.
- **fragment:** a broken-off word (ending in -, — or ~), or a short false start that the next word begins with
  ("st store").
- **filler:** a hesitation word of the profile.

Each rule has a fixed confidence (`contract.proposals.confidence`). It is a stated weight, not a probability.
`min_confidence` (default 0.5) decides what is proposed, and how many findings fell below it is reported. Overlapping
findings merge, and the higher-confidence one names the merged proposal. A one-word repeat of a word people double on
purpose (very very, はい はい) scores 0.3, so by default it is seen but not proposed.

**Language.** English is the default profile. Japanese (`profile: ja`: えー, えっと, あのー, …, with あの and まあ scored
below the default because they are also ordinary words) is a selectable regional profile, per the ecosystem's
internationalisation rule. There is no language detection: the caller names the profile.

**Versioning.** It is a new command and new schemas, outside the pinned blocks. The contract gains an additive
`proposals` block. The canonical `run` invocation is unchanged. It is additive by ADR-007's rules, and the agent needs a
wrapper to use it.

**Verified.** `tests/test_unit.py::RetakeProposalTests`:
- **English.** A fixture with a filler, a one-word repeat, a four-word repeat, a cut-off word, a false start and a
  restart after "uh": all six are proposed, in order, with reasons.
- **Clean speech.** A clean fixture, with "very very", "had had", "that that" and "in the morning and in the evening",
  gives no proposal at the default threshold. The emphatic doubles appear only at 0.2.
- **Japanese.** A Japanese fixture gives its two fillers and two repeats under `profile: ja`.
- **Refusals and the CLI.** Bad timings and unknown profiles or ids are refused. The CLI writes nothing to the
  workspace.

## ADR-025 — Spans by motion and sound: the engine measures, `propose` combines

**Context.** ECOSYSTEM_ROADMAP step 15: find the dead spans of a fixed-camera recording (nobody in frame) from the
picture, and combine them with the sound.

**Decision.** `propose` takes `source` (a video under an allowed root) together with:
- `static: {}`: the shots that `ffmpeg-skill scenes --shots` labels `static`. That label comes from a measured
  optical-flow proxy, and the shot boundaries come from the engine's scene detection.
- `silence: {threshold_db, min_seconds}`: `ffmpeg-skill silence --list` with `--threshold` and `--min-silence`.

Both are measurements by ffmpeg-skill tools; this Skill runs no frame differencing of its own (ADR-001 / ADR-013). The
edges are snapped to the source's frame grid. `combine: {mode, min_seconds}` joins them:
- `and` keeps only the time where every asked measurement holds, labelled `silence+static`.
- `or` keeps every span where any holds, labelled with each measurement that touches it.

Each span is a proposal (`kind: span`, `labels`, `action: cut`, `reason`). As with retakes, nothing is applied: approved
spans become CONDENSE segments, and the rule for each label (cut, keep, speed) is chosen there (step 16). `silence` and
`scenes` join `OPTIONAL_TOOLS`, so their flags are detected. 0.11.0's scenes.py has no `--shots`, so a static request is
refused there (`TOOL_ERROR`).

**Limit, stated in the contract.** The static span's edges are the shot boundaries scene detection finds. A span whose
picture goes still without a visible change at its edges (a person walking slowly out of a fixed shot) is found only as
far as the engine's shots go. Finer motion spans would need a motion-measurement mode in ffmpeg-skill (a proposal there,
not a change from here).

**Versioning.** It is additive: the `proposals` block gains `spans` and the request shape gains keys. `engine.tools_optional`
gains silence and scenes. The agent pins that block, so the agent re-pins.

**Verified.**
- `tests/test_integration.py::MotionSpanTests`, against ffmpeg-skill 3.2.0 (0.11.0: refused, tested). The fixture has 2 s of
  motion, a 3 s still picture and 2 s of motion, with the sound silent from 2.5 to 4.5 s:
  - the still span is found at 2.0 to 5.0 s, to within one frame;
  - `and` with the silence gives 2.5 to 4.5 s (within 50 ms);
  - `or` gives 2.0 to 5.0 s, labelled `silence+static`;
  - the response stays `applied: false`.
- `tests/test_unit.py::SpanCombineTests` covers the and / or arithmetic.

## ADR-026 — CONDENSE: one rule per labelled span (cut, keep, speed)

**Context.** ECOSYSTEM_ROADMAP step 16: once proposals are approved (retakes, silences, static spans; ADR-024 / ADR-025),
each kind of span needs its own treatment. Retakes are cut, silences may be kept at double speed, and the rest is kept.

**Decision.** A new operation type, `CONDENSE {segments: [{start, end, label}], rules: {label: cut | keep | {speed:
factor}}, default}` (capability `video.condense`).
- **The parameters.** `segments` are spans of the input; they may not overlap, and they are sorted. Labels are lower-case
  tokens (`retake`, `silence`, `silence+static`, ...). Every label used needs a rule (`missing_rule`). `default` (keep
  by default) is the rule for the time that no segment covers. A speed factor is SPEED's range [1/4, 4], and not 1. An
  edit that keeps nothing is refused (`nothing_kept`).
- **Pieces.** The input is split into pieces in order (`operations.condense_pieces`), and neighbours with the same rule
  merge.
- **Engine calls.**
  - Each kept piece is cut with `cut.py --start --end --accurate` (`--no-keep-hevc` where the release has it, ADR-017).
  - A sped piece is then retimed with `fit.py --duration <len / factor> --method speed --max-speed 4`, the same call as
    SPEED. The sound keeps its pitch (atempo).
  - The pieces are joined with `join.py --transition none` at the input's size and rate. The sound comes out as stereo
    AAC.
- **Timeline and report.** The timeline maps each kept piece at speed 1 and each sped piece at speed `factor`. Cut pieces
  leave the timeline. The record and the plan carry `condense: {pieces: [{start, end, label, action, factor,
  output_seconds}], removed, input_duration}`.

The judgement stays outside, as ADR-001 requires: the caller decides which proposals are approved and which rule each
label gets.

**Versioning.** A new type in `operations`, `capabilities` and `tools` (pinned): breaking by ADR-007's convention. The
bump is left to the release, and the agent re-pins and needs a lowering.

**Verified.**
- `tests/test_integration.py::CondenseTests`, against ffmpeg-skill 3.2.0 and 0.11.0:
  - **Fixture.** A 6 s clip: a 440 Hz tone, then 2 s at -50 dBFS (an 880 Hz tone), then the tone again.
  - **Proposal.** `propose` finds the 2 s silence. The test approves it and runs CONDENSE with `silence: {speed: 2}`.
  - **Result.** The output lasts 5 s (within 0.1 s). The tones measure 440 / 880 / 440 Hz, so the sped span keeps its
    pitch. The timeline shows speeds 1 / 2 / 1.
- `tests/test_unit.py::CondenseParamTests` (pieces, timeline, refusals) and `test_engine_contract.CondenseFakeTests` (the
  calls, `nothing_kept`).

## ADR-027 — Review of the planned cuts: CONDENSE `output: removed`

**Context.** ECOSYSTEM_ROADMAP step 17: before cutting, check what will be cut.

**Decision.** CONDENSE takes `output: result | removed` (default `result`, the edit). With `removed`, it writes a review
file instead: only the pieces its rules cut, in order, at speed 1, each cut frame-accurately and joined. The engine
calls are the same as for the edit (`cut.py --accurate`, `join.py`). The record and the plan carry `condense.review`:
every removed span with its label, its source range and its range in the review file, plus `removed` (the total). A
review with nothing cut is refused (`nothing_removed`). One request can carry the review and the edit as two CONDENSE
operations with the same segments and rules, so both are made from one approved list.

**Versioning.** `operations.CONDENSE.parameters` gains `output` (pinned). It ships in the same unreleased batch as
ADR-026, with the bump left to the release, and the agent re-pins.

**Verified.**
- `tests/test_integration.py::CondenseTests::test_review_file_lasts_the_removed_spans`, against 3.2.0 and 0.11.0: two
  retakes of 0.5 s and 1.2 s (and a silence kept at 2x) give a review file of 1.7 s (within 0.1 s). The review list
  places the second span at 0.5 s. The edit from the same request lasts 6 - 1.7 - 0.25 s, within the frame tolerance.
- `tests/test_unit.py::CondenseReviewTests` covers the timeline, and the fake-engine tests cover `nothing_removed`.

## ADR-028 — Target file size: ffmpeg-skill export --max-mb, measured and retried

**Context.** ECOSYSTEM_ROADMAP step 18: deliveries with a size cap (mail, chat, upload limits). Given a limit such as 25
MB, the file should land at or just under it on the first request.

**Decision.** An output takes `max_size: {mb: 25}`: exactly one of `bytes`, `kb`, `mb`, `gb` (10^3 steps) or `kib`,
`mib`, `gib` (2^10 steps), from 100 kB to 50 GB.
- **The encode.** The output is delivered through `ffmpeg-skill export <intermediate> --preset youtube --no-scale
  --allow-long --max-mb <target MiB>`. That is a two-pass x264 encode at the video bitrate the running time leaves after
  the audio (AAC 192k), at the produced frame size and rate. The engine does the bitrate arithmetic; this Skill does not
  run ffmpeg.
- **Measure and retry.** The written size is measured. Over the limit, the next target is `target x limit / size x 0.97`.
  Under 90 % of the limit, it is `target x 0.95 x limit / size`. This repeats up to 5 encodes.
- **A picture that cannot use the bits.** When a larger target stops growing the file (two sizes within 1 %), the picture
  needs fewer bits than the limit allows. The file is delivered under the limit with `under_floor: true` and a note. It
  is never padded.
- **Failure.** A limit the engine refuses fails with `OUTPUT_ERROR` `size_limit_unreachable`, with the attempts and the
  engine's own reason. The engine refuses a video bitrate under 100 kb/s ("not watchable") and anything past 400 Mb/s.
  So does a run of 5 encodes that never lands in range. Nothing is delivered then.
- **Not done.** Nothing is lowered automatically to make a small limit fit, neither the frame size nor the audio
  bitrate. The failure says why, and the caller decides (RESIZE first, or a shorter cut).
- **Refusals before execution.** `max_size` with `encoding` is refused (a quality and a size: give one;
  `size_and_encoding`), as is a path that is not `.mp4` (`size_needs_mp4`). So is an ffmpeg-skill without
  `export --max-mb` (2.x+, detected; `export` joins `OPTIONAL_TOOLS`).
- **Report.** `execution.outputs[].size_limit: {limit_bytes, bytes, ratio, attempts, two_pass, under_floor}`.

**Versioning.** `request_shape.outputs` gains `max_size` (agent-pinned), and there is a new additive block,
`size_limit`. The bump is left to the release, and the agent re-pins.

**Verified.** `tests/test_integration.py::SizeLimitTests`, against ffmpeg-skill 3.2.0. On 0.11.0 the refusal is tested.
- **Three fixtures in range.** Each lands in [90 %, 100 %] of its limit:
  - 640x360, 20 s with sound, at 2 MB;
  - 1280x720, 8 s, no sound, at 3 MB;
  - 320x180, 60 s with sound, at 4 MB.
- **Too small a limit.** 60 s into 300 kB fails with the engine's "not watchable" reason, and no file is delivered.
- **Too large a limit.** At 40 MB the 20 s clip stays under the limit; if it lands under 90 %, it says `under_floor`.
- **Offline.** `tests/test_unit.py::SizeLimitParamTests` covers the units, the refusals and the retry arithmetic.


## ADR-029 — `contract_version` 5.0 for the shape 0.4.8 .. 0.4.18 shipped

**Context.** ADR-018 to ADR-028 changed blocks a dependent pins: `operations` gained `PUNCH_IN` (ADR-020) and
`CONDENSE` (ADR-026), TRIM / CUT gained `precision: auto` (ADR-019), CONCAT gained the `stinger` transition (ADR-023);
`capability_names`, `request_shape` (`options.segment_cache`, `sources[].ai_generated`, `outputs[].max_size`) and
`response_shape` changed with them. Each ADR left the bump to the release, but the release workflow only bumps
`version` (patch / minor from labels), so 0.4.8 to 0.4.18 all published `contract_version` "4.0". By ADR-007 that is
wrong: a change inside a pinned block is breaking and bumps `contract_version`.

**Decision.** `contract_version` "4.0" → "5.0", and `version` 0.4.18 → 0.5.0 by hand in the same change (CLAUDE.md
"Contract discipline": this package does not release independently of its contract; the release workflow releases a
hand-bumped version as written). Nothing else changes: no operation, parameter or argv differs from 0.4.18. The golden
copy `tests/contract/contract.json` is regenerated. `versioning.next["0.5.0"]` described candidates that 0.5.0 does not
ship; it moves to `next["0.6.0"]` unchanged, and `contract_check` asks for "0.6.0".

**Consequence.** `video-production-agent` widens its version gate to 0.5.x and re-pins from the v0.5.0 tag.

## ADR-030 — Stinger CONCAT: every clip joined at whole frames

**Context.** A CONCAT with a stinger failed its own validation when its clips had been cut by stream copy: "frame rate
29.955 is not the requested 30" (reproduced with two clips cut `-ss 0.85 -t 2.018 -c copy` from mid-GOP: 29.764). Such a
clip keeps its pre-roll behind an MP4 edit list, and the length probe reports (2.116992 s, 63.5 frames at 30 fps) is the
edit list's: not whole frames, and its last frames can end before it. join.py trims each clip's picture and sound to that
length, so the next clip started between frames and the joined picture had a hole of one frame at the join. Its average
frame rate (frames / duration) fell below 30, overlay.py took the joined file for variable-frame-rate and conformed the
composite to that average (`-r 29.955`), and the output really ran at 29.955 fps. A plain CONCAT of the same clips is
refused the same way when it names `fps` (its joined output is measured, not conformed). The stinger was also placed from
the clips' planned lengths, which a stream-copied clip's measured length differs from.

**Decision.** In a stinger CONCAT, each clip at the edit's frame rate takes its planned length rounded down to whole
frames of that rate; the plan's joins (`cut_at`, `stinger_start`) are computed from those lengths, so every cut is on a
frame. Before the join, a clip whose measured length differs from that (by more than 1 ms) is cut to it with
`ffmpeg-skill/cut --start 0 --end <length> --accurate` (the operation's crf / preset); a clip already whole frames long is
joined as it is, with no extra encode. A clip at another frame rate is joined as before (cutting it at its own rate
cannot put its end on the edit's frames). The rest of the stinger path is unchanged. The plain CONCAT path is not
changed here. (Extended by ADR-033: a plain CONCAT follows the same rule, and a clip at another rate is conformed to the
edit's rate first, in both.)

**Tests.** Fake engine: a source of 2.116992 s plans and reports its join at 21/10 (63 frames) and is cut once before the
join; the standard 2 s fixtures run with no cut. Real media: two clips stream-copied from mid-GOP join with the stinger,
the output is 30/1 fps, and the cut frame (A's length rounded down to frames) is the fully covered one with A before it and
B after it.


## ADR-031 — Smart cut: `precision: smart`, only the partial GOPs at each cut point re-encoded

**Context.** ECOSYSTEM_ROADMAP step 9's second condition, left open by ADR-019: a cut between keyframes should re-encode
at most two GOPs, not the whole cut. ADR-019 ruled out building it here (reading packets and splicing an H.264
re-encode into copied packets is the engine's job, ADR-001 / ADR-013). ffmpeg-skill 3.2.3 now offers it:
`cut.py --smart` (H.264 only) re-encodes from each cut point to the next keyframe and from the last keyframe to the
end point with libx264 matched to the source (profile, level, pixel format, reorder depth, size, SAR, colour tags),
stream-copies the whole GOPs between, cuts the sound on the picture's frame grid and encodes it once (AAC), and checks
the output against the source (frame count and grid, copied packet MD5s, framemd5 of the re-encoded parts, stream
parameters and time base) before moving it into place. When it cannot match the source or a check fails, it
re-encodes the whole cut as `--accurate` would and says why (`mode: accurate`, `smart: {applied: false, reason,
reason_code}`, `diagnostics.fallbacks`). `--smart` with `--accurate` is refused. `probe.py --keyframes-near` lists the
keyframes around a time.

**Decision.** A fourth precision, `smart`, chosen by the caller; the default stays `frame`.
- `smart` compiles to `cut.py --smart` (TRIM `--start` / `--end`, CUT `--segments`), never with `--accurate` or
  `--tolerance`. `--no-keep-hevc` is passed where offered, as for `frame` (ADR-017), so the engine's whole re-encode of
  an SDR HEVC source writes H.264. Validation is `frame`'s: duration within 0.35 s, the input frame, audio kept and the
  re-encode codec rule (the copied GOPs are H.264, which is that rule's SDR codec, and the engine only copies H.264).
- Capability gate (ADR-013): `--smart` is used only where the checkout's detected `cut.py` flags list it. Elsewhere
  (any release before 3.2.3, or a checkout whose flags were not detected) the step is compiled as `frame` (`--accurate`):
  the same frames, re-encoded whole. The request is not refused, because the cut asked for is still made exactly. The
  record says so (`cuts.smart: {applied: false, reason_code: engine_without_smart, reason}`), the plan step says
  `smart.engine_flag: --accurate`, a warning is added, and doctor reports `engine.compatibility.smart_cut: false`.
- Report: `execution.operations[].cuts` (ADR-019's record) gains `smart` (the engine's document passed through,
  OBSERVED, with `source`) and `fallbacks` (the engine's `diagnostics.fallbacks`, `[]` when none). `mode` is the
  engine's (`smart` or `accurate`), so a fallback is visible without reading `smart`.
- An `outputs[].encoding` profile on its output is refused (`encoding_needs_reencode`), as for `keyframe` / `auto`:
  the copied GOPs keep the source's quality, so a profile would apply to a few frames at the edges only.

**Why not the default, and why not inside `auto`.** `frame`'s promise is a full re-encode at the output's encoding
profile, in the re-encode codec; `smart` keeps most of the source's bitstream, so it changes what a caller who asked
for a profile or a uniform encode gets. `auto`'s promise (ADR-019) is lossless first: a cut on a keyframe is the
source's packets byte for byte, and on 3.x a cut whose edit-list copy lands within 0.2 s is a copy too. `--smart`
re-encodes the sound and carries the parameter sets in band, so routing `auto` through it would turn results that are
lossless today into partial re-encodes, and choosing per cut would need a second engine pass. Both stay as they were;
the caller who wants frame accuracy without a whole re-encode asks for `smart`. A later change can revisit the default
once the agent has used `smart` on real long-form material.

**Not applied.** `options.segment_cache` stays a `frame`-precision CUT feature (ADR-018): its parts are joined by
`join.py --copy`, and copy-joining parts that each carry their own in-band parameter sets is not verified by the engine.
The cuts CONDENSE, PUNCH_IN and a stinger CONCAT make inside themselves keep `--accurate`: their result is re-encoded by
the next step (fit / crop / join / overlay) anyway, so a smart cut there saves nothing.

**Versioning.** `operations.TRIM` / `CUT.parameters.precision` gain a value (pinned): `contract_version` "5.0" → "6.0"
and `version` 0.5.1 → 0.6.0, by hand in this change (ADR-029: the release workflow does not bump `contract_version`).
`versioning.next["0.6.0"]` moves to `next["0.7.0"]` unchanged. Additive: the `smart_cut` block,
`engine.compatibility.detected`, doctor's `engine.compatibility.smart_cut`, `plan.steps[].smart`.
`compiler.ALLOWED_FLAGS["cut"]` gains `smart`. video-production-agent widens its version gate and re-pins.

**Verified.** `tests/test_integration.py::SmartCutTests` against the real engine (CI: ffmpeg-skill 0.11.0, 2.24.1, 3.0.0
and main), on an 8 s, 30 fps H.264 clip with a closed GOP of 30:
- TRIM 2.5 to 6.5 s: 120 frames. With `--smart`: `mode: smart`, parts re-encode 2.5-3.0 and 6.0-6.5 and copy 3.0-6.0
  (the keyframes `probe.py --keyframes-near` reports), 30 frames re-encoded (at most two GOPs), and the 90 packets of
  the GOPs from 3 to 6 s are the source's in order (MD5 with the parameter-set and delimiter NAL units removed on both
  sides). Without it: `reason_code: engine_without_smart` and the warning.
- CUT of two ranges: each range re-encodes at most two GOPs; at least three GOPs copied.
- An MPEG-4 Part 2 source: the engine falls back (`mode: accurate`, `reason_code: codec`, `smart_fallback` in
  `fallbacks`) and the output is H.264.
Fake-engine tests (`tests/test_engine_compat.py`): argv, record, plan, the engine fallback, the degraded path on a
checkout without `--smart`, and the refused encoding profile.

## ADR-032 — Stinger cover measured at the cut point: the stinger's own frame, every pixel's alpha

**Context.** ECOSYSTEM_ROADMAP step 13 says a stinger "that does not cover the frame there is refused". ADR-023 checked
only the declared cover window against the cut point. It said no ffmpeg-skill tool reports a stinger's alpha at one
frame, which is still true. But `look.py --at T` writes the frame at T as a PNG, and its alpha is kept: RGBA, or 16-bit
RGBA for a ProRes 4444 stinger. That holds for every release that has `overlay.py --place` (the same `--at` code in
2.24.1, 3.0.0 and main; run against main). So a stinger.json that names the wrong frame, or a graphic with a hole at the cut, was joined as if covered.

**Decision.** Before anything runs, for each stinger CONCAT that will run:
- **The frame.** The cut point at the stinger's rate gives frame n. `look.py <stinger> --at <(n - 1/4) / rate> --width
  <stinger width> --no-timecode -o <work>/<op>.cover.partial.<pid>.png` writes that frame. A seek shows the first frame at
  or after the time, and look.py rounds the time to milliseconds. A quarter frame before frame n lands on n either way,
  up to 480 fps.
- **The measurement.** `alpha.alpha_coverage` reads the PNG with the standard library (zlib, all five scanline filters).
  It accepts RGBA and grey + alpha at 8 / 16 bits, and a palette with tRNS. A pixel covers when its alpha is at least
  253 of 255. That is ffmpeg-skill's own alpha tolerance (2 of 255). A 16-bit alpha is compared on the same scale (257
  per step), since ffmpeg writes ProRes's opaque 10-bit alpha as 65520 (254.94). Nothing here runs ffmpeg or decodes
  video. The engine writes the frame, and this Skill only reads the image file.
- **Refused before anything runs.**
  - A pixel under the threshold is refused as `INVALID_INPUT stinger_not_covering`, with `details.measured` (frame,
    pixels, pixels_below, min_alpha, ...).
  - A frame whose alpha cannot be read (a PNG without an alpha channel, an interlaced one, no file) is refused as
    `INVALID_INPUT stinger_cover_unmeasured`. An alpha that was not read is never taken as opaque.
  - A missing `cut_point` or `cover` now has its own reason, `stinger_no_cut_point` / `stinger_no_cover`
    (`INVALID_REQUEST`). It used to be refused only by the generic missing-parameter check.
  - The declared check (cut point inside the cover window, `INVALID_REQUEST stinger_not_covering`) is unchanged.
- **Once per stinger and cut point** in a request. A CONCAT whose result is reused was measured when it was made. A
  reuse candidate that no longer validates is measured before its join.
- **Report.** `stinger.cover_measured` in the record and the plan: `{tool, frame, at, measured, covered, pixels,
  pixels_below, min_alpha, threshold, opaque_within, frame_size, provenance: OBSERVED, commands}`. A plan says
  `measured: false` (a plan writes no media). `stinger.cover_check` (a description) now says "declared and measured". The
  PNG is removed in every case.
- **Engine.** `look` joins `OPTIONAL_TOOLS` (`engine.tools_used` / `tools_optional`, non-pinned).
  `compiler.ALLOWED_FLAGS["look"] = (at, width, no_timecode)`. A stinger CONCAT on a checkout without `look.py --at /
  --width / --no-timecode` is refused before execution (`TOOL_ERROR`), as one without `overlay.py --place` already was.

**What is not measured.** Only the cut frame is measured, not the whole declared cover window. That frame is where the
clips switch. A window that is generous at its edges does not change the cut.

**qc-skill.** qc-skill (1.1.0) still has no stinger cut-point check. The measurement that matters for the switch is now
made here, on the input before the join. On the result, `StingerTests` checks the decoded frames around the cut. qc-skill's
own check of a delivered file belongs to qc-skill (handed over), and the roadmap step is ticked on that understanding.

**Versioning.** Additive only (`contract --check`: `engine` and `stinger` are non-pinned blocks, and new `details.reason`
values). `contract_version` stays "6.0". The release is a minor one, as a new refusal is new behaviour. Through
video-production-agent the request is the same, and the agent can map the new reasons.

**Verified.**
- `tests/test_integration.py::StingerTests` against ffmpeg-skill main (3.5.0, FFmpeg 6.1).
  - The fixture's frame 15 (the cut point 0.5 s) measures 57600 pixels, none below. The switch test also asserts
    `cover_measured`.
  - A request whose declared cover [14/30, 16/30) contains the cut point 14/30 is refused, because frame 14 is covered
    only on its left half. It reports 28800 pixels below, and nothing is joined or left in the work directory.
- `tests/test_engine_contract.py::StingerTransitionTests` (fake look.py writing a real PNG) covers:
  - the record and the plan;
  - a stinger with a hole, refused with no other engine call;
  - an unreadable alpha;
  - missing `cut_point` / `cover`;
  - a checkout without look.py.
- `tests/test_unit.py::PngAlphaTests` checks every filter type, 8 / 16 bits, grey + alpha, a palette with and without
  tRNS, and the files that give None.

## ADR-033 — Plain CONCAT at whole frames: every input on the edit's frame grid, at any input rate

**Context.** ADR-030 held a stinger CONCAT's clips to whole frames, but only those at the edit's rate. It left the plain
CONCAT as it was ("A plain CONCAT of the same clips is refused the same way when it names `fps`"). That case was
reproduced, then widened to mixed rates. The clips were tiny generated sources at 23.976, 25, 29.97, 30 and 60 fps, plus a
variable-rate one (every fifth frame dropped). All are 2 s long (`-t 2`): 48 frames at 23.976 fps last 2.002 s, and so do
60 frames at 29.97 fps. The runs used ffmpeg-skill main (3.5.0) and FFmpeg 6.1.1.

- **30 fps edit, 20 clips at 23.976 fps between two 30 fps clips.** `r_frame_rate` 30/1 but `avg_frame_rate` 39600/1321
  (29.977). There were 1320 frames over 44.033 s, with one missing frame (a 2-frame interval) at 20.0 s. The timeline said
  44.040 s and placed the last clip at 42.040 s. With `fps: 30` the same request failed: `VALIDATION_ERROR` "frame rate
  29.976 is not the requested 30".
- **23.976 fps edit, 20 clips at 30 fps.** Each 2.0 s clip came out 48 frames long (2.002 s), so the file was 42.042 s against a
  timeline of 42.002 s. The timeline fell 2 ms behind per clip, and the clips after the first sat off the frames it reported.
- **60 fps edit [60, 30, 25, 29.97].** No hole yet, but the timeline said 8.002 s and the file was 8.000 s.

join.py (2.4.1 and later) conforms each clip to the output rate, then trims picture and sound to the clip's own length
(`trim=duration` / `atrim`, to the millisecond). The concat filter starts the next clip after the longer of the two.
Frames are placed on the output grid. A length that is not whole frames of the output rate therefore moves every later
clip by its remainder. Once the remainders add up to half a frame, a grid slot is skipped: that is the hole. This happens
for any clip at another rate, and for a same-rate clip whose length is not whole frames (ADR-030's stream-copied segment).
The timeline was the sum of the exact lengths, so it diverged from the file by the same amount. The contract promises the
first input's rate when `fps` is not given, and that rate was not validated.

**Decision.** A CONCAT without a transition joins every input at whole frames of its rate, exactly like a stinger
CONCAT. Both now share one path (`Executor._whole_frame_inputs`). The rate is `params.fps`, else the first input's. The
rules:
- An input at that rate keeps ADR-030's rule. Its planned length is rounded down to whole frames. A length within 1 ms of
  the next frame keeps that frame: the allowance is 1 ms in seconds, the same as the measured-length check. ADR-030 had
  0.001 of a frame. It is cut to that length (`cut --start 0 --end <length> --accurate`) only when its measured length
  differs by more than 1 ms. Otherwise it is joined as it is.
- **New:** an input at another rate, or one flagged variable-rate, is conformed first: `fit --fps <rate> --duration <length>
  --method trim`. The length is rounded to the nearest frame, which is what the engine's fps conversion does itself, so no
  content is lost for it. fit.py cuts at `-t` to the millisecond, so the duration passed is the last whole millisecond at
  or before the length (a time rounded up would let one more frame in). A clip rounded up by under half a frame holds its
  last instant for the rest. The timeline shows that as a segment of speed 0, the way FREEZE reports a hold.
- The timeline is built from these lengths (`build_timelines(..., joined_length=...)`). The plan and the output therefore
  report each clip where it lands. A stinger's joins come from the same lengths, which extends ADR-030 to clips at another
  rate.
- **Report.** `whole_frames: {rate, inputs: [{input, fps, length, joined_length, action, measured_length}]}` in the record
  and the plan. In a plan, `action` is `none`, `cut_if_needed` or `conform`. There is a new additive contract block,
  `whole_frames`.
- **Validation.** The output must also run at the first input's rate when `fps` is not given (`VALIDATION_ERROR fps`), as
  the contract promised. An input whose whole-frame length is no frame at all (shorter than half a frame at another rate,
  or than one frame at the rate) is refused before anything runs (`INVALID_TIME_RANGE shorter_than_a_frame`).
- **Unchanged.** A CONCAT with an xfade transition (its inputs overlap), and inputs already whole frames of the rate (no
  extra encode, the same join argv). (Extended by ADR-034: an xfade CONCAT takes the same path, its transition held to
  whole frames too.)

**Cost.** One extra encode per input at another rate, and per same-rate input whose length is not whole frames. The latter
includes a frame-precision cut that kept a partial last frame: cut.py keeps every frame that starts before the end, so
1.00 to 1.25 s at 30 fps is 8 frames, and it is cut to 7. That is the price of a constant-rate output and a timeline that
matches the file. The stinger path already paid it.

**Not fixed: the engine's millisecond lengths.** join.py writes each clip's length to the millisecond, and
starts the next clip after the longer of picture and sound. A clip of whole frames whose length rounds *up* still starts
the next clip that much late. At 30 fps that is a frame count 2 more than a multiple of 3, up by 1/3 ms. At 29.97 fps it is
up to 0.5 ms. So some 35 to 50 such clips in one edit can still leave a one-frame hole. Measured: 60 clips of 5 frames at
30 fps, every length rounding up from 0.166667 to 0.167 s, gave holes from 8.3 s on, with the timeline exact. This Skill
cannot place a clip more precisely than the length join.py takes. It is a proposal to ffmpeg-skill (frame-exact clip
lengths in join.py, e.g. `trim=end_frame` and sample-exact `atrim`), documented in `contract.whole_frames.engine_note`, and
not worked around here. Choosing frame counts so that the millisecond rounds down would tie this Skill to a formatting
detail of one engine release.

**Engines before 2.4.1.** join.py of 0.11.0 concatenates each clip's picture and sound as they are, with no trim to one
length. An intermediate's AAC sound runs a few milliseconds past its picture (fit and cut encode whole AAC frames), and the
concat filter starts the next clip after it. Measured on 0.11.0 with FFmpeg 6.1:
- a 60 fps edit of [60, 30, 25, 29.97] switched to its fourth clip one frame late (6.0167 s against 6.000 s), with 481
  frames for a 480-frame timeline;
- ten 23.976 fps clips in a 30 fps edit gave 2 frames more than the timeline;
- a short 30 fps edit was exact.

The whole-frame inputs still remove the larger error, but the timeline is exact only from 2.4.1 on. `ConcatFrameRateTests`
runs there (2.24.1, 3.0.0 and main in CI) and is skipped on 0.11.0. The contract's `whole_frames.engine_note` says so.

**Versioning.** Additive (`contract --check`):
- a new `whole_frames` block;
- the `media_compatibility` / `tools[].media` CONCAT output text;
- the `stinger` and `validation` texts;
- `plan.steps[].whole_frames` and `execution.operations[].whole_frames`.

`contract_version` stays "6.0". A bug fix, so the release is a patch.

**Verified.**
- `tests/test_integration.py::ConcatFrameRateTests` against ffmpeg-skill main (2.4.1+ only, see above). Every clip is a flat colour with a 0.1 s beep
  at its start, so each join is found in the picture and in the sound. Three tests:
  - 30 fps edit with ten 23.976 fps clips, with and without `fps: 30`;
  - 23.976 fps edit with ten 30 fps clips;
  - [29.97, 25, 60, variable-rate, 23.976, 30] with `fps: 30`, and a 60 fps edit of [60, 30, 25, 29.97].
- What each test checks:
  - there is no frame interval over 1.5 frames, and `avg_frame_rate` equals `r_frame_rate` at the promised rate;
  - the timeline lasts the file's frames to 2 ms, and the sound lasts them to within a frame;
  - every reported join is on a frame, the picture changes colour on exactly that frame, and the next beep starts within a
    frame of it.
- On the code before this change the same tests fail with the defects above:
  - a hole at 19.967 s;
  - a timeline of 22.002 s against a 22.022 s file;
  - 12.004 s against 12.0 s.
- `tests/test_engine_contract.py::ConcatWholeFramesTests` (fake engine) covers:
  - the conform (fit) and the timeline with its hold segment;
  - a variable-rate input;
  - the cut of a length that is not whole frames;
  - whole-frame inputs joined with no extra call;
  - an xfade CONCAT left unchanged;
  - the rate validated without `fps`.
- `GeneratedClipTests` now expects the third clip on a frame of the 29.97 fps edit: its 3 s at 24 fps is joined as 90
  frames, not 3.0 s.


## ADR-034 — CONCAT with an xfade transition at whole frames: every input and the transition on the edit's frame grid

**Context.** ADR-033 left a CONCAT with an xfade transition as it was ("its inputs overlap"). That case was reproduced
with the same kind of tiny generated sources: 2 s each at 23.976, 25, 29.97, 30 and 60 fps, plus a variable-rate one (every
fifth frame dropped). Each clip is a flat colour with a white flash over its lower half and a beep, both at 1.0 s for 0.1 s.
So each clip is found in the picture (its first flash frame) and in the sound (its beep), and compared with the timeline.
They were joined with `fade`, `dissolve` and `wipeleft` of 0.25 s and 0.5 s. The runs used ffmpeg-skill main (3.6.0) and
FFmpeg 6.1.1.

join.py conforms each clip to the output rate and trims its picture and sound to the clip's length (to the millisecond).
It starts the transition after clip k at the lengths before it less the transitions, also written to the millisecond
(`xfade=...:offset=`), and crossfades the sound over exactly those lengths (`acrossfade`). xfade starts a transition on the
first frame at or after its offset and takes the next clip from its first frame there. An offset between frames therefore
starts the next clip's picture on the frame after it, while its sound starts at the offset itself. The picture is late by the
remainder, up to a frame, against its sound and against the timeline, which placed the clip at the offset. The clip's last
frame goes into the next transition. The file runs past the timeline when the last offset is late. An offset falls between
frames whenever an input's length is not whole frames of the output rate (any clip at another rate; 60 frames at 29.97 fps
last 2.002 s), or the transition is not (0.5 s is 14.985 frames at 29.97 fps, 11.988 at 23.976 and 12.5 at 25; 0.25 s is 7.5
frames at 30).

Measured before this change (the clips after the first: frames late against the timeline, and picture after sound):

| Edit | Late joins | Frames late | Picture after sound | Frames in file / timeline |
|---|---|---|---|---|
| six 29.97 fps clips, dissolve 0.5 | 5 of 5 | 0.96 to 1.02 | 32 to 34 ms | 286 / 285.075 |
| six 23.976 fps clips, fade 0.5 | 5 of 5 | 0.96 to 1.01 | 40 to 42 ms | 229 / 228.06 |
| 30 fps edit: 30, four 23.976, 30, fade 0.5 | 4 of 5 | 0.76 to 0.94 | 25 to 31 ms | 286 / 285.24 |
| 29.97, 25, 60, variable-rate, 23.976, 30 with `fps: 30`, wipeleft 0.5 | 5 of 5 | 0.88 to 0.94 | 29 to 31 ms | 286 / 285.12 |
| 25, 29.97, 23.976, variable-rate, 60, 30 with `fps: 25`, wipeleft 0.5 | 5 of 5 | 0.4 to 0.95 | 16 to 38 ms | 238 / 237.6 |
| six 30 fps clips, fade 0.25 | 3 of 5 | 0.5 | 17 ms | 323 / 322.5 |
| 23.976 fps edit: 23.976, six 30, dissolve 0.5 | 1 of 6, then a drift | 1.01, then 0.05 to 0.19 | 42 ms, then 2 to 8 ms | 264 / 263.784 |
| 60 fps edit: 60, 30, 25, 29.97, fade 0.5 | 0 of 3 | 0 | 0 | 390 / 390.12 |

The video stream outlasted the sound by up to a frame (9.543 s against 9.512 s for the six 29.97 fps clips), and the timeline
placed clips between frames (1.502 s at 29.97 fps). There were no holes and no duplicated timestamps: xfade writes every frame
on the output grid, and `avg_frame_rate` equalled `r_frame_rate` in every case. The defect was where the picture of each clip
lands, against its sound and the timeline.

**Decision.** A CONCAT with an xfade transition joins on the same whole-frame path as a plain or stinger CONCAT (ADR-030,
ADR-033): `Executor.joins_whole_frames` is now every CONCAT. The rules:
- **Inputs.** Unchanged from ADR-033. An input at the rate is rounded down to whole frames and cut only when its measured
  length differs. One at another rate, or variable-rate, is conformed (`fit --fps --duration --method trim`, nearest frame).
- **Transition.** **New:** its duration is rounded to the nearest frame of the rate (a half rounds up, at least one frame).
  It is joined at that length (`join --duration`), so every offset is whole frames. The compiled join step carries it, so the
  plan's `arguments`, the preview and the run agree. A transition already whole frames gives the same join argv as before.
- **Timeline.** It overlaps the clips by that duration (`build_timelines(..., joined_transition=...)`), so every clip starts
  on a frame, where the file shows it.
- **Range check.** "Too short for the transition" is checked on the joined lengths and the joined duration, the values
  join.py is given. A clip at most twice the transition is refused before anything runs instead of failing in the engine.
- **Report.** `whole_frames.transition: {type, duration, joined_duration, frames}` in the plan and the record, beside the
  inputs.
- **Validation and reuse.** The output rate is validated against the first input's without `fps` (ADR-033), and the
  idempotency key carries the rule, so an xfade result joined before this change is not reused. Every idempotency key
  now also carries the keys of the operations it takes. A rule that changes how an upstream result is made changes its key
  but not its `operation_id`, and the results made from it were reused before: a RESIZE of a CUT switched to
  `segment_cache` was (`SegmentCacheTests`).

**Cost.** The same as ADR-033: one extra encode per input at another rate, or with a length that is not whole frames.
Rounding the transition moves each overlap by under half a frame (at most 17 ms at 30 fps), so the edit is that much
longer or shorter per transition than the request's seconds imply. The joined duration is reported.

Measured after this change, on the same edits: 0 frames late at every join. Picture and sound are together (within 1 ms:
the 29.97 and 23.976 fps sources' flash frame lies at 1.001 s). The file has exactly the timeline's frames, and its picture,
sound and timeline last the same, for the first five edits in the table and the 60 fps one. The six 29.97 fps clips are
285 frames, 9.5095 s of picture, 9.509 s of sound and a timeline of 9.5095 s. The six 23.976 fps ones are 228 frames, the
mixed 30 fps and 25 fps edits 285 and 235 frames. The two other edits are the engine's remainder (below).

**Not fixed: the engine's millisecond offsets.** At whole frames an offset is a frame time, but join.py writes it to the
millisecond, and that can round up past its frame. 104 frames at 30 fps (3.466667 s) is written `offset=3.467`, and xfade
starts that transition on frame 105. At 30, 24 and 60 fps one whole-frame time in three rounds up; at 29.97, 23.976 and
59.94 fps about one in two; at 25 and 50 fps none. Such a transition starts exactly one frame late, and with it the next
clip's picture, against its sound and the timeline. The clip loses its last frame into the next transition, and the file is
one frame longer when it is the last join. It is never more than a frame and never adds up, because every offset is counted
from the start of the join. Measured after this change:
- six 30 fps clips with a 0.25 s fade (8 frames): the 3rd and 6th clips are one frame late (33 ms), at offsets 3.467 and
  8.667; the file has 321 frames for a timeline of 320;
- a 23.976 fps edit of six 30 fps clips with a 0.5 s dissolve (12 frames): the 4th and 6th clips are one frame late (43 ms),
  at offsets 4.505 and 7.508. Before this change that edit had one join a frame late and the rest drifting by 2 to 8 ms.

This Skill cannot place a transition more precisely than the offset join.py writes. Two workarounds were considered and not
taken. Nudging the transition's duration so that every offset rounds down ties this Skill to the engine's number formatting
and shifts the sound by that nudge at every join. Joining each transition alone (the outgoing clip's last frames and the
incoming clip's first frames, at offset 0) and butt-joining the rest adds two encodes per clip and many more joins, each with
ADR-033's own remainder. It is a proposal to ffmpeg-skill: place xfade offsets in frames of the output rate (or write them
to the microsecond, rounded down to the frame), as ADR-033 proposed for clip lengths. It is documented in
`contract.whole_frames.engine_note`. ffmpeg-skill 2.21.0 and 3.6.0 write the offset the same way.

**Versioning.** Additive (`contract --check`):
- the `whole_frames` block (`applies_to`, `rule`, `why`, `report`, `refused`, `not_applied` now empty, `engine_note`);
- the `media_compatibility` / `tools[].media` CONCAT texts;
- `plan.steps[].whole_frames.transition` and `execution.operations[].whole_frames.transition`.

`contract_version` stays "6.0". A bug fix, so the release is a patch.

**Verified.**
- `tests/test_integration.py::ConcatXfadeFrameRateTests` against ffmpeg-skill main (2.4.1 and later, as
  `ConcatFrameRateTests`). It uses the flash-and-beep clips above, in four tests:
  - every rate and the variable-rate clip with `fps: 25` and a 0.5 s wipeleft (13 frames), exact;
  - 30 fps clips around four 23.976 fps ones, fade 0.5, exact;
  - six 29.97 fps clips with a 0.5 s dissolve, which is not whole frames, exact;
  - six 30 fps clips with a 0.25 s fade, where join.py rounds two offsets up: at most one frame late there.
- What each test checks:
  - no frame interval over 1.5 frames or under 0.5, and `avg_frame_rate` equal to `r_frame_rate` at the promised rate;
  - every clip the timeline reports starts on a frame, and the timeline is whole frames;
  - the file has the timeline's frames (one more only where the engine's last offset rounds up), and its sound lasts as long
    within a frame;
  - each clip's flash is on the frame the timeline puts 1.0 s into it, its beep is there within 2 ms, and picture and sound
    are together within a quarter frame;
  - the report: the transition's frames and the timeline built from the joined lengths.
- On the code before this change, all four fail on the timeline: clips at 1.5 s (37.5 frames at 25 fps), 3.002 s at 30 fps,
  1.502 s at 29.97 fps and 1.75 s at 30 fps.
- `tests/test_engine_contract.py::ConcatWholeFramesTests` (fake engine), which before this change expected an xfade CONCAT to
  be left unchanged, now covers:
  - a 25 fps input conformed and a 0.25 s transition joined as 8 frames, in the plan, the join's argv and the timeline;
  - whole-frame inputs and transition joined as before, with no extra call and the same `--duration`;
  - an input too short for the transition at its whole-frame length, refused before anything runs.

## ADR-035 — The overlay boundary: `OVERLAY` stays plain still compositing as part of an edit

**Context.** `OVERLAY` composites one still image over a video. Two requests to widen it were open: a video layer
with chroma key (`overlay.py --video` / `--chromakey`, issue #11 item 2), and ADR-002's note that a picture-in-picture
`POSITION` type should rather "extend OVERLAY" with a video layer. Neither was decided. The
[Velisa ownership decisions 2026-11-12](https://github.com/kajisho5/Velisa/pull/156) (O10; Velisa
`docs/ecosystem/DECISION_LOG.md` D21) settled who owns each kind of overlay.

**Decision.**
- This Skill keeps `静止画オーバーレイ`: plain still compositing as part of an edit. `OVERLAY` places one png / jpg at
  a named position (or `{x, y}`) for a time range, alpha respected. Its parameters stay as they are.
- motion-graphics-skill owns `ロゴ・PiP・クロマキー合成` and `動くグラフィックの配置`. A video layer, picture-in-picture,
  chroma key, a logo or image treated as a graphic (fade, theme, safe zones) and any moving graphic go there
  (`image_overlay`, `logo`, `video_overlay`, `rendered_layer`).
- ffmpeg-skill is the engine under both.
- The PiP / chroma-key request for `OVERLAY` is answered by this record, not implemented here. ADR-002's "extend
  OVERLAY with a video layer" no longer applies: `POSITION` stays unsupported, and no operation is added.
- The wiring is unchanged. A caller choosing between this Skill's `OVERLAY` (the agent's `edit.overlay`) and
  motion-graphics-skill (`motion.image`) reads this rule.

**Contract part.** Roadmap step 19 also asks the contract to say this: `OVERLAY`'s description, and its refusal of a
video source as `INVALID_INPUT` with a message that names motion-graphics-skill `video_overlay`. That is ADR-036.

## ADR-036 — `OVERLAY` refuses a video with `INVALID_INPUT` naming motion-graphics-skill `video_overlay`; contract 7.0

**Context.** ADR-035 wrote the overlay boundary down in prose; roadmap step 19 also asks the contract to carry it. A video
given to `OVERLAY` was refused as `DEPENDENCY_ERROR kind_mismatch` ("must be a image source"), which says what is wrong
but not where the request belongs. `OVERLAY`'s documentation sits in the pinned `operations` block, and `POSITION`'s
reason in the pinned `unsupported` block still said a video layer "would extend OVERLAY (ADR-002)", which ADR-035 retired.

**Decision.**
- A video in `OVERLAY.params.image` (a video source, or an operation's output, which is always a video) is refused before
  anything runs as `INVALID_INPUT`, `details.reason: overlay_video_layer`, `details.use: "motion-graphics-skill
  video_overlay"`, with a message that names motion-graphics-skill's `video_overlay` element (and `rendered_layer` for an
  animated graphic with alpha). `validate`, `plan` and `run` refuse it alike. `INVALID_INPUT` already exists; no error code is
  added. An image in a video slot stays `DEPENDENCY_ERROR kind_mismatch`, and so does an operation in `IMAGE_INSERT`.
- The contract says the same in four places: `operations.OVERLAY.parameters.image` (pinned), the overlay tool's
  `description`, `media_compatibility.OVERLAY.refused_before_execution`, and `media_policy.image`. `POSITION`'s reason
  (pinned `unsupported`) now names motion-graphics-skill `video_overlay` instead of a future `OVERLAY` video layer, and
  `versioning.next` drops the `OVERLAY` video-layer candidate (not planned, ADR-035).
- The still path is unchanged: parameters, argv and outputs of every `OVERLAY` with an image are what they were.

**Versioning.** The `operations` and `unsupported` blocks are pinned (docs/contract.md), so this is a breaking contract
change by this repository's rule even though no request that used to succeed now fails: `contract_version` "6.0" → "7.0",
`version` 0.7.4 → 0.8.0, both by hand in the same change (ADR-029's lesson: the release workflow does not bump
`contract_version`). A minor version, not a major one: the package is still 0.x. The golden copy is regenerated.
video-production-agent widens its version gate to 0.8.x and re-pins from the v0.8.0 tag; its mapping of a failed `OVERLAY`
sees `INVALID_INPUT` (exit 3) where it saw `DEPENDENCY_ERROR` for this one case.

**Verified.** `tests/test_engine_contract.py` (`test_overlay_refuses_a_video_and_names_motion_graphics`): a video source and
a TRIM's output in `params.image` are refused by `validate`, `plan` and `run` with `INVALID_INPUT overlay_video_layer` naming
motion-graphics-skill `video_overlay`, and no engine tool runs; the contract test reads the four places. The still-image
`OVERLAY` tests are unchanged and pass.
