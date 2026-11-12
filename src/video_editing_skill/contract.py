"""Machine-readable contract (`video-editing contract --json` / `skill --json`).

Derived from the operation table and the error table, never maintained beside them. Field names follow
video-production-agent's SkillPackage / ToolSpec where they overlap (skill_id, name, version, description,
capabilities, tools[] with tool_id, skill_id, version, required_capabilities, inputs, produces_output,
deterministic, result_keys).
"""
from typing import Any, Dict, List

from . import CONTRACT_SCHEMA, CONTRACT_VERSION, DOCTOR_SCHEMA, PACKAGE_NAME, PLAN_SCHEMA, REQUEST_SCHEMA, RESPONSE_SCHEMA, SKILL_ID, VERSION
from .errors import DEFAULT_RETRYABLE, ERROR_CODES, EXIT_CODES
from .ffmpeg_skill import ENGINE_TOOLS, MIN_VERSION, OPTIONAL_TOOLS
from .operations import (ENCODING, FORBIDDEN_KEYS, FRAME_SEMANTICS, MEDIA, MEDIA_POLICY, OPERATIONS, POSITIONS, TRANSITIONS, capability_list,
                         media_compatibility, unsupported_list)
from .paths import IMAGE_EXTENSIONS, OUTPUT_EXTENSIONS, VIDEO_EXTENSIONS

# Contract versioning (documented in docs/contract.md). Agents pin a snapshot of this document and compare these blocks
# verbatim: a change in any of them is a breaking contract change and needs a new skill version *and* a review on the
# agent side. Anything outside them may be added within the same version (additive).
PINNED_BLOCKS = ("schema", "skill_id", "contract_version", "operations", "unsupported", "errors", "execution", "capabilities", "schemas")

TOOL_REQUIREMENTS = {
    "ffmpeg-skill/cut": ["ffmpeg", "ffprobe", "encoder:libx264", "encoder:aac"],
    "ffmpeg-skill/join": ["ffmpeg", "ffprobe", "encoder:libx264", "encoder:aac", "filter:xfade", "filter:acrossfade"],
    "ffmpeg-skill/fit": ["ffmpeg", "ffprobe", "encoder:libx264", "encoder:aac"],
    "ffmpeg-skill/overlay": ["ffmpeg", "ffprobe", "encoder:libx264", "encoder:aac"],
    "ffmpeg-skill/freeze": ["ffmpeg", "ffprobe", "encoder:libx264", "encoder:aac"],
    "ffmpeg-skill/insert": ["ffmpeg", "ffprobe", "encoder:libx264"],   # a silent clip: no audio encoder
    "ffmpeg-skill/reverse": ["ffmpeg", "ffprobe", "encoder:libx264", "encoder:aac"],
    "ffmpeg-skill/crop": ["ffmpeg", "ffprobe", "encoder:libx264", "encoder:aac"],
    "ffmpeg-skill/probe": ["ffprobe"],
}

SEGMENT_CACHE: Dict[str, Any] = {
    "request_field": "options.segment_cache (bool, default false)",
    "applies_to": "CUT with precision frame; every other type is one engine call and is reused as a whole (contract.identity.reuse)",
    "render": ["each keep range is cut on its own: ffmpeg-skill/cut --start --end --accurate (plus the output's encoding profile)",
               "each part is validated (video, duration within the frame tolerance, audio kept) and stored under <work>/segments/ with its record",
               "the parts are joined by stream copy: ffmpeg-skill/join --transition none --copy (one part: copied as is)"],
    "segment_key": "sha256(input identity, range start / end as rationals, tool, tool versions, skill version, container, encoding, engine release flags); "
                   "the rest of the keep list is not part of it",
    "partial_re_render": "after a change only the parts whose key is new are encoded; the rest are taken from the cache (status reused)",
    "same_request_twice": "the operation itself is reused (idempotency_key); nothing is encoded or written in the work directory",
    "report": "execution.operations[].segments: {mode: segments, join: stream_copy | single | single_pass_fallback, parts: [{index, range, key, status: "
              "encoded | reused, sha256, seconds}], encoded, reused, fallback_reason?}; plan.steps[].segments: [{index, range, key, cached}]",
    "differs_from_single_pass": ["the joins are butted: cut.py's declick fades (--segments) are not applied to a stream-copy join",
                                 "the bytes differ from the single-pass render, so the idempotency key carries render: segments"],
    "fallback": "when ffmpeg-skill refuses the copy join (the parts' picture and sound do not line up within its limit), the operation is rendered in one "
                "pass as without the cache; the parts stay cached and the record says single_pass_fallback with the engine's reason",
    "needs": "ffmpeg-skill/join --copy (from ffmpeg-skill 2.24.0, detected); without it a request asking for the cache is refused before execution (TOOL_ERROR)",
    "reuse_false": "options.reuse false also bypasses the segment cache (every part is encoded again and the cache is refreshed)",
}

SMART_CUT: Dict[str, Any] = {
    "request_field": "TRIM / CUT params.precision: smart (opt-in; the default stays frame)",
    "engine_call": "ffmpeg-skill/cut --smart (TRIM: --start --end; CUT: --segments), plus --no-keep-hevc where offered",
    "what_it_does": "a cut point between keyframes is re-encoded from that point to the next keyframe, and from the last keyframe to the end point; "
                    "the whole GOPs between are the source's packets; the sound is cut on the picture's frame grid and encoded once (AAC). The engine "
                    "checks the result against the source (frame count and grid, copied packets, decoded frames of each re-encoded part, stream "
                    "parameters) before it is written; the result is frame-accurate like precision frame",
    "engine_fallback": "a source the engine cannot match (not H.264 8-bit 4:2:0 Baseline / Main / High, interlaced, variable-rate, HDR, an open GOP at a "
                       "copy boundary, ...) or a check that fails: the engine re-encodes the whole cut (mode accurate) and says why (smart.reason, "
                       "smart.reason_code, diagnostics.fallbacks); the operation still succeeds",
    "engine_without_smart": "an ffmpeg-skill whose cut.py has no --smart (detected; before 3.2.3): the cut runs as precision frame (--accurate), a warning is "
                            "added, and the record says smart.applied false, reason_code engine_without_smart",
    "refused": ["an outputs[].encoding profile on its output (INVALID_REQUEST encoding_needs_reencode): the copied GOPs keep the source's quality"],
    "not_applied": ["options.segment_cache (frame precision only)", "the cuts CONDENSE / PUNCH_IN / a stinger CONCAT make inside themselves (their "
                    "result is re-encoded by the next step anyway)"],
    "validation": "as precision frame: duration within 0.35 s, the input frame, audio kept, and the re-encode codec rule (H.264 for SDR)",
    "report": "execution.operations[].cuts: cut.py's mode (smart | accurate), reencoded, reencode_reason, duration_delta_seconds, ..., fallbacks (the "
              "engine's diagnostics.fallbacks) and smart: {applied, parts: [{segment, kind: reencode | copy, start, end, frames}], reencoded_frames, "
              "copied_frames, checks, source} or {applied: false, reason, reason_code, source}; plan.steps[].smart: {engine_flag: --smart | --accurate, "
              "available, reason_code, reason}",
}

STINGER: Dict[str, Any] = {
    "request_field": "CONCAT params.transition: {type: stinger, stinger, cut_point, cover: {start, end}}",
    "file": "a video source with an alpha plane (ProRes 4444, QuickTime Animation, PNG in MOV, ...), the edit's aspect and frame rate; its length is "
            "the file's; its sound, if any, is mixed in. hyperframes-skill / remotion-ffmpeg-skill / blender-skill write one with its stinger.json: "
            "cut_point and cover come from it (the caller reads it and passes them)",
    "placement": "the clips are butt-joined; at each join the cut is the first frame of the next clip, and the stinger starts cut_point before it, so its "
                 "frame at cut_point lies on the cut frame; every clip is joined at whole frames of the edit's rate, as in a plain CONCAT (a clip at "
                 "that rate rounded down, one at another rate or variable-rate conformed to it, rounded to the nearest frame), so every cut is on a "
                 "frame",
    "engine_calls": ["ffmpeg-skill/look <stinger> --at <a quarter frame before cut_point> --width <its width> --no-timecode -o <png>, once before "
                     "anything runs: the stinger's frame at cut_point, written with its alpha (cover_check)",
                     "ffmpeg-skill/fit --fps <edit rate> --duration <whole frames> --method trim, for a clip at another rate or variable-rate",
                     "ffmpeg-skill/cut --start 0 --end <whole frames> --accurate, only for a clip whose measured length is not that (a stream-copied "
                     "segment, as a rule)", "ffmpeg-skill/join (no transition, the CONCAT's size / rate / mode)", "ffmpeg-skill/overlay --video <stinger> --place 0-<length>@<start> per join "
                     "(alpha respected)", "with sound: ffmpeg-skill/broll --insert <stinger> --at <start> --duration <length> --audio mix per join, then "
                     "ffmpeg-skill/audio --replace (that mix onto the composite)"],
    "refused": ["no cut_point (INVALID_REQUEST stinger_no_cut_point) or no cover (INVALID_REQUEST stinger_no_cover)",
                "cut_point outside cover (INVALID_REQUEST stinger_not_covering)",
                "cut_point not on a frame of the stinger (INVALID_REQUEST cut_point_not_on_frame)",
                "a stinger whose frame at cut_point is not opaque in every pixel, measured (INVALID_INPUT stinger_not_covering, details.measured), or "
                "whose alpha there cannot be read (INVALID_INPUT stinger_cover_unmeasured); before anything runs",
                "a stinger without an alpha plane (INVALID_INPUT stinger_no_alpha), of another aspect (stinger_aspect) or frame rate (stinger_fps)",
                "an input shorter than the stinger's lead-in (before the join) or tail (after it) (INVALID_TIME_RANGE)",
                "a stinger that is an operation, not a source (DEPENDENCY_ERROR kind_mismatch)",
                "an ffmpeg-skill without look.py --at / --no-timecode or overlay.py --place, or (stinger with sound) broll.py / audio.py --replace (TOOL_ERROR)"],
    "report": "execution.operations[].stinger and plan.steps[].stinger: {source, cut_point, cover, duration, audio, joins: [{cut_at, stinger_start, "
              "stinger_end}], cover_check, cover_measured: {tool, frame, at, measured, covered, pixels, pixels_below, min_alpha, threshold, "
              "opaque_within, frame_size, provenance: OBSERVED} (in a plan: measured false, measured when the request runs)}; the output timeline's "
              "overlay track has a {kind: stinger, cut_at, timeline_range} segment per join",
    "cover_check": "declared and measured: the cover window (the stinger maker's statement) must contain cut_point, and the stinger's own frame at "
                   "cut_point is measured before anything runs: ffmpeg-skill/look writes it as a PNG with its alpha, this Skill reads every pixel's "
                   "alpha, and the frame covers when none is under 253 of 255 (ffmpeg-skill's alpha tolerance, 2 of 255); the same stinger and cut "
                   "point are measured once per request",
}

WHOLE_FRAMES: Dict[str, Any] = {
    "applies_to": "CONCAT without a transition, and CONCAT with a stinger (contract.stinger): the inputs are butt-joined",
    "rate": "params.fps, else the first input's frame rate (as join.py chooses it); the output is constant at that rate",
    "rule": ["every input is joined at whole frames of the rate, and the timeline places every clip at that length",
             "an input at the rate: its planned length rounded down to whole frames (one within 1 ms of the next frame keeps it); cut to it (ffmpeg-skill/cut --start 0 --end <length> "
             "--accurate) only when its measured length differs (a stream-copied segment, a cut that kept a part frame), else joined as it is",
             "an input at another rate, or variable-rate: its planned length rounded to the nearest frame of the rate, conformed to the rate and "
             "that length first (ffmpeg-skill/fit --fps <rate> --duration <length> --method trim); a clip rounded up holds its last instant for "
             "the rest of its last frame (a timeline segment of speed 0)"],
    "why": "join.py trims each clip to its own length and starts the next after it: a length that is not whole frames of the output rate moved "
           "every later clip off the frame grid (a one-frame hole in the picture once the remainders reached half a frame, and a timeline that "
           "drifted from the file by the remainder of every clip)",
    "report": "execution.operations[].whole_frames and plan.steps[].whole_frames: {rate: {fps, rational}, inputs: [{input, fps, length, "
              "joined_length, action: none | cut | conform (a plan: cut_if_needed | conform | none), measured_length}]}",
    "validation": "the output's frame rate is the rate within 0.02, also when params.fps is not given (VALIDATION_ERROR fps)",
    "refused": ["an input whose whole-frame length is no frame at all (INVALID_TIME_RANGE shorter_than_a_frame), before anything runs"],
    "not_applied": ["a CONCAT with an xfade transition (its inputs overlap; joined as before)"],
    "engine_note": "join.py writes each clip's length to the millisecond and starts the next clip after the longer of its picture and its "
                   "sound: a clip of whole frames whose length rounds up (at 30 fps a frame count 2 more than a multiple of 3, up by 1/3 ms; at "
                   "29.97 fps up to 0.5 ms) still starts the next one that much late, so some 35 to 50 such clips in one edit can still leave a "
                   "one-frame hole; that remainder is the engine's (a proposal to ffmpeg-skill), not worked around here. Before ffmpeg-skill 2.4.1 "
                   "join.py does not trim a clip's sound to its picture, so an AAC tail can still start the next clip up to a frame late",
}

AI_GENERATED: Dict[str, Any] = {
    "request_field": "project.sources[].ai_generated: {generator: text, licence: text} (each 1..200 printable characters)",
    "meaning": "declared by the caller, never inferred from the media; reported with provenance DECLARED",
    "mark": "AI_GENERATED: execution.operations[].marks / ai_generated and execution.outputs[].marks / ai_generated list every declared source the result "
            "contains ({source, generator, licence, provenance: DECLARED}); execution.sources[].ai_generated repeats the declaration",
    "identity": "not part of operation_id or idempotency_key: the bytes decide identity; the mark is recomputed on every run, so a reused result is marked too",
    "normalisation": ["CONCAT conforms a generated clip with the rest (ffmpeg-skill/join): frame size by params.width / height (mode pad | crop), frame rate by "
                      "params.fps or the first input's, square pixels (setsar=1), BT.709 tags on the H.264 output, and a silent track for an input "
                      "without sound when another has sound",
                      "set params.width / height / fps on the CONCAT so the edit's format decides, not whichever clip comes first"],
}

PARAM_DOCS: Dict[str, Dict[str, str]] = {
    "TRIM": {"start": "time", "end": "time (> start)", "precision": "frame (default, re-encode) | keyframe (lossless when possible) | auto (stream copy when the copy lands within 0.2 s of the range, e.g. an in point on a keyframe; else a frame-accurate re-encode; reported per cut) | smart (frame-accurate; only the partial GOPs at each cut point re-encoded, the whole GOPs between copied, verified against the source; else the whole cut re-encoded, with the reason; contract.smart_cut)"},
    "CUT": {"keep": "[{start, end}, ...] in output order", "precision": "frame | keyframe | auto | smart"},
    "CONCAT": {"transition": "{type: " + "|".join(TRANSITIONS) + ", duration: time 0.01..10} (optional), or {type: stinger, stinger: video source id "
                             "(a file with alpha), cut_point: time (the frame the clips switch at), cover: {start, end} (the window the graphic covers the "
                             "whole frame; must contain cut_point)} (contract.stinger)",
               "width": "even int (optional)", "height": "even int (optional)", "fps": "number or N/D (optional)",
               "mode": "pad | crop (how inputs of another aspect reach the frame)", "pad_color": "named colour or 0xRRGGBB"},
    "SPEED": {"factor": "number or N/D in [1/4, 4], not 1",
              "smooth": "blend | interpolate (optional; slow-motion quality when factor < 1 -- ffmpeg-skill fit.py ignores it when speeding up)"},
    "FIT": {"aspect": "W:H", "width": "even int (optional)", "pad_color": "named colour or 0xRRGGBB", "fps": "optional"},
    "FILL": {"aspect": "W:H", "width": "even int (optional)",
             "anchor": "{x, y} each 0..1: which edge the crop keeps (0=left/top, 0.5=centre default, 1=right/bottom) (optional)", "fps": "optional"},
    "RESIZE": {"width": "even int", "fps": "optional"},
    "OVERLAY": {"image": "image source id", "position": "|".join(POSITIONS) + " or {x, y} px", "margin": "int px", "scale": "image width px (optional)",
                "opacity": "(0, 1]", "start": "time (optional)", "end": "time (optional)", "fade": "seconds 0..10 (optional)"},
    "ROTATE": {"degrees": "90 | 180 | 270, clockwise (optional)", "flip": "h | v, mirror horizontal/vertical (optional; at least one of degrees/flip is required)"},
    "FREEZE": {"hold": "time 0.01..600 s: how long the frame is held",
               "at": "time, < input duration (optional): hold the frame shown at `at` and play the rest after the hold; omitted: hold the last frame at the end"},
    "REVERSE": {},
    "IMAGE_INSERT": {"duration": "time 0.01..600 s: the clip's length",
                     "width": "even int px (optional; alone: height follows the image aspect)",
                     "height": "even int px (optional; alone: width follows the image aspect; both: exact frame, the image covers it, centre-cropped)",
                     "fps": "1..240 (optional, default 30)"},
    "CONDENSE": {"segments": "[{start, end, label}] non-overlapping spans of the input (e.g. approved proposals: retake, silence, static); label "
                             "[a-z0-9][a-z0-9_+.-]{0,39}",
                 "rules": "{label: cut | keep | {speed: factor 1/4..4, not 1}}: one per label used",
                 "default": "cut | keep | {speed: factor} for the time no segment covers (default keep)",
                 "output": "result (default: the edit) | removed (a review file: only the spans the rules cut, joined in order at speed 1; the record lists each "
                           "with its place in the file)"},
    "PUNCH_IN": {"start": "time", "end": "time (> start): the zoomed range; a hard cut in at start and out at end",
                 "scale": "number or N/D in (1, 4]: the zoom (2 = the window is half the frame in each dimension)",
                 "subject": "{x, y, width, height} int px in the input frame as displayed: the box to keep in view (e.g. detection-skill's face box); "
                            "the window is centred on it and kept inside the frame"},
    "CROP": {"x": "int px >= 0: left edge in the input frame as displayed", "y": "int px >= 0: top edge",
             "width": "even int px: the rectangle's width (x + width <= input width)", "height": "even int px: the rectangle's height (y + height <= input height)"},
}


def proposals_block() -> Dict[str, Any]:
    from .proposals import CONFIDENCE, DEFAULT_MIN_CONFIDENCE, DEFAULT_PROFILE, PROFILES, PROPOSALS_SCHEMA, PROPOSE_SCHEMA, RESTART_MAX_SECONDS, RESTART_WINDOW
    return {
        "invocation": ["video-editing", "propose", "-", "--json", "--workspace", "<dir>", "--allowed-input", "<root>"],
        "request_schema": PROPOSE_SCHEMA, "response_schema": PROPOSALS_SCHEMA,
        "request_shape": {"schema": PROPOSE_SCHEMA,
                          "retakes": {"transcript": "JSON file under an allowed root: {words: [{word | text, start, end}]} in time order",
                                      "profile": f"{' | '.join(sorted(PROFILES))} (default {DEFAULT_PROFILE}; others are regional profiles)",
                                      "min_confidence": f"0..1 (default {DEFAULT_MIN_CONFIDENCE})"},
                          "source": "the video silence / static measure (under an allowed root)",
                          "silence": "{threshold_db: -90..-5 (default -35), min_seconds: 0.05.. (default 0.6)}: ffmpeg-skill/silence --list",
                          "static": "{}: spans with no motion, the shots ffmpeg-skill/scenes --shots labels static",
                          "combine": "{mode: and | or (default or), min_seconds: time (default 0)}"},
        "spans": {"silence": "ffmpeg-skill/silence --list --threshold --min-silence: its silences, snapped to the source's frame grid",
                  "static": "ffmpeg-skill/scenes --shots: shots labelled static (a measured optical-flow proxy); the span edges are the shot "
                            "boundaries the engine's scene detection finds, so a still span that starts or ends without a visible change of "
                            "picture is found only at the shot's edges",
                  "and": "only where every asked measurement holds (silent and static); label silence+static",
                  "or": "wherever any holds; labelled with every measurement that touches the span",
                  "needs": "ffmpeg-skill silence.py; scenes.py --shots (2.x+, detected; TOOL_ERROR without it)"},
        "applies": "nothing: the response says applied: false; approved proposal ids become CONDENSE segments, and only those are cut",
        "rules": {"repeat": "the same 1-4 words twice in a row; the first saying is proposed, up to where the second starts",
                  "restart": f"a phrase of 2+ words said again within {RESTART_WINDOW} words and {RESTART_MAX_SECONDS:g} s, with only hesitations "
                             "between (or a 3+ word opening after at most 3 words)",
                  "fragment": "a broken-off word (ends with -, —, ~) or a short false start the next word begins with",
                  "filler": {k: sorted(v["fillers"]) for k, v in sorted(PROFILES.items())}},
        "confidence": {"per_rule": dict(CONFIDENCE), "filler": "per word, in the profile",
                       "meaning": "a fixed weight of the rule that fired, not a probability; emphatic doubles of the profile (very very, はい はい) score below the default"},
        "proposal_shape": {"id": "p1..", "kind": "repeat | restart | fragment | filler | span", "labels": "span: the measurements that hold", "label": "retake", "action": "cut", "start": "time", "end": "time",
                           "word_range": "[first, last] word index", "text": "the words", "reason": "why, in words", "confidence": "0..1", "also": "merged overlaps"},
        "not_provided": ["any model, score or language detection", "automatic application"],
    }


def ffmpeg_skill_version_range() -> str:
    """The version range this Skill's own `version_supported()` check enforces at runtime (`ffmpeg_skill.py`'s
    `MIN_VERSION`), as the range-string shape `dependencies[].version_range` uses (never an exact pin:
    `kajisho5/AI-video-production-OS` `docs/VERSIONING.md` section 2). A floor only, no ceiling: what differs between
    ffmpeg-skill releases is detected from the installed checkout (ADR-013). Shared by `engine.version_range` and
    `dependencies` so the two can never say something different."""
    return f">={'.'.join(map(str, MIN_VERSION))}"


def capability_provides() -> List[Dict[str, str]]:
    """Cross-repository Capability ids this Skill can be asked to perform (AI Video Production OS
    `CapabilityContract.provides`, kajisho5/AI-video-production-OS docs/SPEC.md section 1).

    Derived from OPERATIONS, the same source tool_specs() uses, so it can never drift from what the
    Skill actually does. Deliberately excludes capability_list()'s two synthetic entries
    (video.transition, video.reorder): those describe a property of CONCAT/CUT, not something an
    Operation can independently target, and a Capability id here is meant to be one a ProductionPlan
    step can request on its own.

    `lifecycle` is EXPERIMENTAL for all of them, not because the operations themselves are unproven
    (they are the same tested, already-in-production TRIM/CUT/... operations tool_specs() describes),
    but because the cross-Skill Capability id concept this field publishes is new: no Agent or registry
    is known to consume it yet. See docs/decisions.md ADR-006.
    """
    return [{"id": spec["capability"], "lifecycle": "EXPERIMENTAL", "tool_id": f"{SKILL_ID}/{t.lower()}"}
            for t, spec in sorted(OPERATIONS.items())]


def tool_specs() -> List[Dict[str, Any]]:
    specs = []
    for t in sorted(OPERATIONS):
        spec = OPERATIONS[t]
        specs.append({
            "tool_id": f"{SKILL_ID}/{t.lower()}", "skill_id": SKILL_ID, "version": VERSION, "operation_type": t,
            "capability": spec["capability"], "description": spec["summary"],
            "required_capabilities": list(TOOL_REQUIREMENTS[spec["tool"]]),
            "inputs": ["input"] if spec["arity"] == "one" else ["inputs"], "input_arity": spec["arity"],
            "parameters": PARAM_DOCS[t],
            "media": {"inputs": MEDIA[t]["inputs"], "requires": dict(MEDIA[t]["requires"]), "output": dict(MEDIA[t]["output"])},
            "produces_output": True, "writes_media": True, "deterministic": True, "idempotency_hint": "content_equivalent",
            "result_keys": ["operation_id", "output", "probe", "commands", "provenance"],
            "executed_by": spec["tool"], "kind": "transform",
        })
    return specs


def skill_contract() -> Dict[str, Any]:
    return {
        "schema": CONTRACT_SCHEMA,
        "skill_id": SKILL_ID, "name": "Video Editing Skill", "package": PACKAGE_NAME, "version": VERSION, "contract_version": CONTRACT_VERSION,
        "description": "Deterministic video editing: typed edit requests (trim, cut, concat with transitions, speed, fit/fill/resize, rotate/flip, "
                       "image overlay, freeze frame, reverse, still image to clip) compiled to an operation graph with source/timeline mapping and executed through ffmpeg-skill. Not an agent: no "
                       "editing decisions, no LLM, no commands.",
        "role": "execution",
        "repository": "https://github.com/kajisho5/video-editing-skill",
        "not_provided": ["AI reasoning", "editing decisions", "production plans", "project IR", "speaker / scene detection", "transcription",
                         "captions", "colour grading", "audio mastering", "thumbnails", "QC beyond output validation"],
        "capabilities": capability_list(),
        "capability_names": sorted({c["capability"] for c in capability_list()}),
        "unsupported": unsupported_list(),
        "tools": tool_specs(),
        "operations": {t: {"capability": s["capability"], "tool": s["tool"], "arity": s["arity"], "parameters": PARAM_DOCS[t]} for t, s in sorted(OPERATIONS.items())},
        "engine": {"id": "ffmpeg-skill", "version_range": ffmpeg_skill_version_range(),
                   "tools_used": [f"ffmpeg-skill/{t}" for t in ENGINE_TOOLS],
                   "tools_optional": [f"ffmpeg-skill/{t}" for t in OPTIONAL_TOOLS],
                   "location": "VIDEO_EDITING_FFMPEG_SKILL_DIR | --ffmpeg-skill-dir | ~/.claude/skills/ffmpeg-skill | ./vendor/ffmpeg-skill | ../ffmpeg-skill",
                   "invocation": "[python, <ffmpeg-skill>/scripts/<tool>.py, typed argv, --json]; process group; scrubbed env; timeout",
                   "compatibility": {"upper_bound": None,
                                     "detected": ["tools present (scripts/<tool>.py)",
                                                  "CLI flags per tool (scripts/_contract.py --json input_schema, else <tool>.py --help)",
                                                  "optional tools (tools_optional): a checkout without one leaves only the operation it executes MISSING",
                                                  "the ffmpeg-skill tool of each unsupported type (doctor: unsupported[].engine_tool_status)",
                                                  "encoding quality flag: --quality where offered, else --crf (same CRF scale)",
                                                  "probe colour key: video.bt2020_or_hdr where present, else video.hdr (same meaning)",
                                                  "cut.py's SDR HEVC re-encode codec: --no-keep-hevc passed where offered (3.0+), so a frame-precision TRIM / CUT writes H.264 for SDR like every other operation",
                                                  "cut.py --smart (3.2.3+): a precision smart TRIM / CUT uses it where offered, else --accurate (contract.smart_cut)"],
                                     "floor_reason": "overlay.py bounds a looped-image composite with -t only from 0.10.0 (behaviour, not a flag)"}},
        "execution": {
            "mode": "local_subprocess",
            "canonical_invocation": ["video-editing", "run", "-", "--json", "--workspace", "<dir>", "--allowed-input", "<root>"],
            "stdin": "EditRequest JSON when the request argument is '-'",
            "stdout": "exactly one response document when --json is given",
            "stderr": "diagnostics only; never part of the contract",
            "shell": False, "arbitrary_executables": False, "raw_ffmpeg_arguments": False, "filter_strings": False, "network": False, "ai": False,
            "input_mutation": False,
            "executable_resolution": "ffmpeg-skill directory from environment / CLI only; ffmpeg and ffprobe from PATH by ffmpeg-skill; nothing from the request",
            "output_location": "request output paths are relative to --workspace and may not leave it",
            "dry_run": "plan - --json compiles, checks ranges against probed durations and asks ffmpeg-skill --dry-run for commands; no media is written",
        },
        "schemas": {"request": REQUEST_SCHEMA, "response": RESPONSE_SCHEMA, "plan": PLAN_SCHEMA, "contract": CONTRACT_SCHEMA, "doctor": DOCTOR_SCHEMA},
        "request_shape": {
            "schema": REQUEST_SCHEMA,
            "project": {"id": "optional label", "sources": [{"id": "label", "path": "file under an allowed root", "kind": "video | image",
                                                             "ai_generated": "optional {generator, licence}: the caller declares the source AI-generated (contract.ai_generated)"}],
                        "operations": [{"id": "label", "type": "one of operations", "input": "source or operation id", "inputs": "[ids] for CONCAT", "params": {}}],
                        "outputs": [{"id": "label", "operation": "operation id", "path": "relative path under the workspace (.mp4 | .mov | .mkv)",
                                     "encoding": "optional {crf, preset} (contract.encoding); refused where noted there",
                                     "max_size": "optional {bytes | kb | mb | gb | kib | mib | gib: number} (MB = 10^6 bytes): fit the file under it (contract.size_limit)"}]},
            "options": {"timeout_seconds": "1..86400 (default 3600)", "overwrite": "bool (default false)", "reuse": "bool (default true)",
                        "segment_cache": "bool (default false): a frame-precision CUT renders each keep range alone, cached by content, joined by stream copy (contract.segment_cache)"},
        },
        "time": {"forms": ["number (seconds)", "'ss.fff' | 'mm:ss' | 'hh:mm:ss.fff'", "{seconds}", "{rational: 'N/D'}", "{frames, timebase}", "{frames, fps}"],
                 "representation": "exact rationals; serialised as {seconds: 'fixed 6 places', rational: 'N/D'}",
                 "mapping": "every output carries a timeline: tracks[].segments[] with source_range and timeline_range per source"},
        "formats": {"video_inputs": list(VIDEO_EXTENSIONS), "image_inputs": list(IMAGE_EXTENSIONS), "outputs": list(OUTPUT_EXTENSIONS)},
        "identity": {"operation_id": "op_ + sha256(type, canonical params, input identities)[:16]; input identity = sha256 of source bytes or upstream operation_id",
                     "idempotency_key": "sha256(operation_id, tool, tool versions, skill version, container)",
                     "reuse": "a work-dir intermediate whose record matches the idempotency key and hash is reused and reported status: reused"},
        "provenance": {"per_operation": ["skill", "skill_version", "tool", "tool_versions", "operation_id", "type", "inputs[].sha256", "output.sha256",
                                         "parameters", "commands", "started_at", "finished_at", "status"],
                       "observation": "probe documents are OBSERVED with source ffmpeg-skill/probe@<version>; request values are never reported as observations"},
        "errors": {"codes": list(ERROR_CODES), "exit_codes": dict(EXIT_CODES), "retryable_default": dict(DEFAULT_RETRYABLE),
                   "shape": {"ok": False, "error": {"code": "...", "message": "...", "retryable": False, "details": {}}}},
        "response_shape": {"ok": True, "schema": RESPONSE_SCHEMA, "skill": {"id": SKILL_ID, "version": VERSION}, "status": "completed | reused",
                           "project": "...", "execution": {"operations": ["provenance records"], "outputs": ["path, sha256, timeline, observation"]}},
        # ---- additive blocks (outside PINNED_BLOCKS; see docs/contract.md)
        # ---- provides: cross-repository Capability ids (docs/decisions.md ADR-006)
        "provides": capability_provides(),
        # ---- dependencies: cross-repository Skill dependencies (docs/decisions.md ADR-008)
        "dependencies": [{"skill_id": "ffmpeg-skill", "version_range": ffmpeg_skill_version_range()}],
        "media_compatibility": media_compatibility(),
        "graph": {"model": "sources -> operations (DAG) -> outputs; every operation feeds an output; order is topological, ties by id",
                  "refused": ["cycle (DEPENDENCY_ERROR)", "unknown input / operation reference (DEPENDENCY_ERROR)", "duplicate source / operation / output id (INVALID_REQUEST)",
                              "operation that leads to no output (DEPENDENCY_ERROR)", "two outputs on one path (DEPENDENCY_ERROR)",
                              "video slot fed by an image or image slot fed by a video / operation (DEPENDENCY_ERROR)",
                              "unknown operation type (UNSUPPORTED_OPERATION)", "unknown or forbidden key anywhere (INVALID_REQUEST)",
                              "media an operation cannot take (INVALID_INPUT, before execution)", "engine tool / encoder / filter missing (TOOL_ERROR, before execution)"],
                  "limits": {"sources": 200, "operations": 500, "outputs": 50, "concat_inputs": 100, "cut_ranges": 500},
                  "forbidden_keys": list(FORBIDDEN_KEYS)},
        "validation": {"before_execution": ["schema and typed parameters", "path policy", "graph", "every source probed by ffmpeg-skill (video: stream + duration; image: decodes)",
                                            "ranges against probed durations", "media compatibility (operations.MEDIA)", "engine tools and capabilities (ffmpeg-skill doctor)"],
                       "after_each_operation": ["file exists, non-empty, readable", "probe: video stream, duration, frame size",
                                                "duration within tolerance of the timeline (frame / auto / smart 0.35 s, keyframe 1.5 s)",
                                                "frame size / aspect / width as requested, or equal to the input for TRIM / CUT / SPEED / OVERLAY / FREEZE / REVERSE",
                                                "frame rate as requested (a CONCAT without a transition or with a stinger: the first input's when params.fps "
                                                "is not given)", "audio stream kept when the input(s) had one"],
                       "on_reuse": "the candidate is re-validated with the same checks (hash, size, probe); a stale record is discarded and the operation runs again",
                       "response": "every document is checked against the response shape before it is printed (self-check); a violation is INTERNAL_ERROR"},
        "doctor_shape": {"schema": DOCTOR_SCHEMA, "ok": "bool", "skill": {"id": SKILL_ID, "version": VERSION},
                         "contract": "schema ids", "engine": "ffmpeg-skill location, version, ffmpeg / ffprobe, missing capabilities",
                         "operations": [{"type": "…", "tool_id": "…", "status": "AVAILABLE | MISSING", "missing": ["what is missing"]}],
                         "supported_operations": "types whose status is AVAILABLE (never a guess)", "unsupported": "declared gaps", "checks": "…", "problems": "…"},
        "versioning": {"version": VERSION, "contract_version": CONTRACT_VERSION, "pinned_blocks": list(PINNED_BLOCKS),
                       "rule": "two independent axes (docs/decisions.md ADR-007): `version` is this package's own release version and may move on any "
                               "release, including one that changes nothing a dependent needs to react to; `contract_version` is the version of the "
                               "shape a pinned block publishes and changes only when a pinned block changes in a breaking way. A change inside a "
                               "pinned block bumps `contract_version` (and `version`, since this package does not yet release independently of its "
                               "contract) and agents must re-pin; new keys outside the pinned blocks (top level or inside tools[]) are additive and "
                               "allowed within the same contract_version; the golden copy tests/contract/contract.json is regenerated deliberately in "
                               "the same change, and `contract --check` classifies every difference as breaking or additive",
                       "also_pinned_by_agents": ["request_shape", "response_shape", "engine", "formats", "capability_names", "tools[].parameters"],
                       "next": {"0.7.0": ["RESIZE: `height` as the alternative to `width` -- blocked on ffmpeg-skill: `fit.py` has no `--height` flag "
                                         "(docs/decisions.md ADR-003 correction, found by live verification against ffmpeg-skill 0.10.0)",
                                         "OVERLAY video-layer + chroma-key params (ffmpeg-skill overlay.py --video/--chromakey), per ADR-002's own \"extend OVERLAY rather than add a "
                                         "type\" prescription -- flagged, not decided or scheduled here (see kajisho5/video-editing-skill#11 item 2)",
                                         "stabilize / sequence / background: capability ownership (here, elsewhere, or nowhere) not yet decided (see kajisho5/video-editing-skill#11 item 3)",
                                         "POSITION: not planned as a type (see docs/decisions.md ADR-002)"]}},
        # ---- 0.1.x additive blocks: encoding profile, frame semantics, media policy (docs/decisions.md ADR-003 .. ADR-005)
        "encoding": ENCODING,
        # ---- target file size (docs/decisions.md ADR-028)
        "size_limit": {"request_field": "project.outputs[].max_size: {mb: 25} (one of bytes, kb, mb, gb = 10^3 steps; kib, mib, gib = 2^10 steps); 100 kB .. 50 GB",
                       "engine": "ffmpeg-skill/export --preset youtube --no-scale --allow-long --max-mb <target MiB>: a two-pass x264 encode at the video bitrate "
                                 "the running time leaves after the audio (AAC 192k), at the produced frame size and rate",
                       "retry": "measured after each encode: over the limit -> target x limit / size x 0.97; under 90 % -> target x 0.95 x limit / size; up to 5 "
                                "encodes; the delivered file lies in [90 %, 100 %] of the limit",
                       "refused": ["with outputs[].encoding (a quality and a size: give one; INVALID_REQUEST size_and_encoding)",
                                   "a path that is not .mp4 (INVALID_REQUEST size_needs_mp4)",
                                   "an ffmpeg-skill without export --max-mb (2.x+; TOOL_ERROR before execution)",
                                   "a limit the engine cannot meet (under its lowest watchable video bitrate, 100 kb/s, for the duration; or none of 5 encodes "
                                   "in range): OUTPUT_ERROR size_limit_unreachable with the attempts and the engine's reason; nothing is delivered"],
                       "not_done": "no automatic downscale or audio-bitrate reduction to make a small limit fit: the failure says so and the caller decides",
                       "report": "execution.outputs[].size_limit: {limit_bytes, bytes, ratio, attempts: [{target_mib, bytes, ratio}], two_pass}"},
        # ---- cut proposals (docs/decisions.md ADR-024)
        "proposals": proposals_block(),
        # ---- stinger transition (docs/decisions.md ADR-023)
        "stinger": STINGER,
        # ---- butt joins at whole frames (docs/decisions.md ADR-030, ADR-033)
        "whole_frames": WHOLE_FRAMES,
        # ---- AI-generated sources (docs/decisions.md ADR-022)
        "ai_generated": AI_GENERATED,
        # ---- segment cache and partial re-render (docs/decisions.md ADR-018)
        "segment_cache": SEGMENT_CACHE,
        # ---- keyframe-aware smart cut (docs/decisions.md ADR-031)
        "smart_cut": SMART_CUT,
        "frame_semantics": FRAME_SEMANTICS,
        "media_policy": MEDIA_POLICY,
    }
