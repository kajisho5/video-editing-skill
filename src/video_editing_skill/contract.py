"""Machine-readable contract (`video-editing contract --json` / `skill --json`).

Derived from the operation table and the error table, never maintained beside them. Field names follow
video-production-agent's SkillPackage / ToolSpec where they overlap (skill_id, name, version, description,
capabilities, tools[] with tool_id, skill_id, version, required_capabilities, inputs, produces_output,
deterministic, result_keys).
"""
from typing import Any, Dict, List

from . import CONTRACT_SCHEMA, DOCTOR_SCHEMA, PACKAGE_NAME, PLAN_SCHEMA, REQUEST_SCHEMA, RESPONSE_SCHEMA, SKILL_ID, VERSION
from .errors import DEFAULT_RETRYABLE, ERROR_CODES, EXIT_CODES
from .ffmpeg_skill import REQUIRED_TOOLS, SUPPORTED_MAX_EXCLUSIVE, SUPPORTED_MIN
from .operations import (ENCODING, FORBIDDEN_KEYS, FRAME_SEMANTICS, MEDIA, MEDIA_POLICY, OPERATIONS, POSITIONS, TRANSITIONS, capability_list,
                         media_compatibility, unsupported_list)
from .paths import IMAGE_EXTENSIONS, OUTPUT_EXTENSIONS, VIDEO_EXTENSIONS

# Contract versioning (documented in docs/contract.md). Agents pin a snapshot of this document and compare these blocks
# verbatim: a change in any of them is a breaking contract change and needs a new skill version *and* a review on the
# agent side. Anything outside them may be added within the same version (additive).
PINNED_BLOCKS = ("schema", "skill_id", "version", "operations", "unsupported", "errors", "execution", "capabilities", "schemas")

TOOL_REQUIREMENTS = {
    "ffmpeg-skill/cut": ["ffmpeg", "ffprobe", "encoder:libx264", "encoder:aac"],
    "ffmpeg-skill/join": ["ffmpeg", "ffprobe", "encoder:libx264", "encoder:aac", "filter:xfade", "filter:acrossfade"],
    "ffmpeg-skill/fit": ["ffmpeg", "ffprobe", "encoder:libx264", "encoder:aac"],
    "ffmpeg-skill/overlay": ["ffmpeg", "ffprobe", "encoder:libx264", "encoder:aac"],
    "ffmpeg-skill/probe": ["ffprobe"],
}

PARAM_DOCS: Dict[str, Dict[str, str]] = {
    "TRIM": {"start": "time", "end": "time (> start)", "precision": "frame (default, re-encode) | keyframe (lossless when possible)"},
    "CUT": {"keep": "[{start, end}, ...] in output order", "precision": "frame | keyframe"},
    "CONCAT": {"transition": "{type: " + "|".join(TRANSITIONS) + ", duration: time 0.01..10} (optional)",
               "width": "even int (optional)", "height": "even int (optional)", "fps": "number or N/D (optional)",
               "mode": "pad | crop (how inputs of another aspect reach the frame)", "pad_color": "named colour or 0xRRGGBB"},
    "SPEED": {"factor": "number or N/D in [1/4, 4], not 1"},
    "FIT": {"aspect": "W:H", "width": "even int (optional)", "pad_color": "named colour or 0xRRGGBB", "fps": "optional"},
    "FILL": {"aspect": "W:H", "width": "even int (optional)", "fps": "optional"},
    "RESIZE": {"width": "even int", "fps": "optional"},
    "OVERLAY": {"image": "image source id", "position": "|".join(POSITIONS) + " or {x, y} px", "margin": "int px", "scale": "image width px (optional)",
                "opacity": "(0, 1]", "start": "time (optional)", "end": "time (optional)", "fade": "seconds 0..10 (optional)"},
}


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
        "skill_id": SKILL_ID, "name": "Video Editing Skill", "package": PACKAGE_NAME, "version": VERSION,
        "description": "Deterministic video editing: typed edit requests (trim, cut, concat with transitions, speed, fit/fill/resize, image overlay) "
                       "compiled to an operation graph with source/timeline mapping and executed through ffmpeg-skill. Not an agent: no editing "
                       "decisions, no LLM, no commands.",
        "role": "execution",
        "repository": "https://github.com/kajisho5/video-editing-skill",
        "not_provided": ["AI reasoning", "editing decisions", "production plans", "project IR", "speaker / scene detection", "transcription",
                         "captions", "colour grading", "audio mastering", "thumbnails", "QC beyond output validation"],
        "capabilities": capability_list(),
        "capability_names": sorted({c["capability"] for c in capability_list()}),
        "unsupported": unsupported_list(),
        "tools": tool_specs(),
        "operations": {t: {"capability": s["capability"], "tool": s["tool"], "arity": s["arity"], "parameters": PARAM_DOCS[t]} for t, s in sorted(OPERATIONS.items())},
        "engine": {"id": "ffmpeg-skill", "version_range": f">={'.'.join(map(str, SUPPORTED_MIN))},<{'.'.join(map(str, SUPPORTED_MAX_EXCLUSIVE))}",
                   "tools_used": [f"ffmpeg-skill/{t}" for t in REQUIRED_TOOLS],
                   "location": "VIDEO_EDITING_FFMPEG_SKILL_DIR | --ffmpeg-skill-dir | ~/.claude/skills/ffmpeg-skill | ./vendor/ffmpeg-skill | ../ffmpeg-skill",
                   "invocation": "[python, <ffmpeg-skill>/scripts/<tool>.py, typed argv, --json]; process group; scrubbed env; timeout"},
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
            "project": {"id": "optional label", "sources": [{"id": "label", "path": "file under an allowed root", "kind": "video | image"}],
                        "operations": [{"id": "label", "type": "one of operations", "input": "source or operation id", "inputs": "[ids] for CONCAT", "params": {}}],
                        "outputs": [{"id": "label", "operation": "operation id", "path": "relative path under the workspace (.mp4 | .mov | .mkv)"}]},
            "options": {"timeout_seconds": "1..86400 (default 3600)", "overwrite": "bool (default false)", "reuse": "bool (default true)"},
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
                                                "duration within tolerance of the timeline (frame 0.35 s, keyframe 1.5 s)",
                                                "frame size / aspect / width as requested, or equal to the input for TRIM / CUT / SPEED / OVERLAY",
                                                "frame rate as requested", "audio stream kept when the input(s) had one"],
                       "on_reuse": "the candidate is re-validated with the same checks (hash, size, probe); a stale record is discarded and the operation runs again",
                       "response": "every document is checked against the response shape before it is printed (self-check); a violation is INTERNAL_ERROR"},
        "doctor_shape": {"schema": DOCTOR_SCHEMA, "ok": "bool", "skill": {"id": SKILL_ID, "version": VERSION},
                         "contract": "schema ids", "engine": "ffmpeg-skill location, version, ffmpeg / ffprobe, missing capabilities",
                         "operations": [{"type": "…", "tool_id": "…", "status": "AVAILABLE | MISSING", "missing": ["what is missing"]}],
                         "supported_operations": "types whose status is AVAILABLE (never a guess)", "unsupported": "declared gaps", "checks": "…", "problems": "…"},
        "versioning": {"version": VERSION, "pinned_blocks": list(PINNED_BLOCKS),
                       "rule": "a change inside a pinned block is breaking: bump the version (0.x: minor) and expect agents to re-pin; new keys outside them "
                               "(top level or inside tools[]) are additive and allowed within the same version; the golden copy tests/contract/contract.json "
                               "is regenerated deliberately in the same change, and `contract --check` classifies every difference as breaking or additive",
                       "also_pinned_by_agents": ["request_shape", "response_shape", "engine", "formats", "capability_names", "tools[].parameters"],
                       "next": {"0.2.0": ["RESIZE: `height` as the alternative to `width` (exactly one of the two)",
                                         "request_shape: `outputs[].encoding` folded into the documented shape (accepted since 0.1.0 as an optional key)",
                                         "CROP (pixel rectangle) once ffmpeg-skill provides a typed crop tool", "IMAGE_INSERT (still -> timed clip) once ffmpeg-skill provides a typed tool",
                                         "FREEZE / REVERSE / POSITION: not planned (see docs/decisions.md ADR-002)"]}},
        # ---- 0.1.x additive blocks: encoding profile, frame semantics, media policy (docs/decisions.md ADR-003 .. ADR-005)
        "encoding": ENCODING,
        "frame_semantics": FRAME_SEMANTICS,
        "media_policy": MEDIA_POLICY,
    }
