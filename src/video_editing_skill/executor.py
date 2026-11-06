"""Execution: run the compiled steps in graph order inside the workspace, validate every output,
record provenance, reuse identical earlier results, and never leave a failed file behind as a result.

Layout under <workspace>/.video-editing/work/   (flat: operation_id is content-derived, so it is the key)
  <operation_id>.partial.<pid>.<ext>   the tool writes here
  <operation_id>.<ext>           moved here only after validation
  <operation_id>.json            record {idempotency_key, sha256, size, probe} used for reuse
Final outputs are copied from the validated intermediate to their path via a .partial file + rename.
"""
import os
import shutil
import time
from typing import Any, Dict, List, Optional

from . import SKILL_ID, VERSION
from .canonical import sha256_file, stable_hash
from fractions import Fraction

from .compiler import ALLOWED_FLAGS, Step, _fps_text, compile_project, output_extension
from .errors import EditError
from .ffmpeg_skill import FfmpegSkill, cancelled, missing_capabilities, probe, run_tool, tool_error
from .operations import ENCODING_DEFAULTS, MEDIA, condense_pieces, condense_played, even, params_to_json, punch_window
from .paths import is_within
from .project import EditProject
from .timebase import Time
from .timeline import Clip, build_timelines

DURATION_TOLERANCE = {"frame": 0.35, "keyframe": 1.5, "auto": 0.35, "default": 0.35}
FPS_TOLERANCE = 0.02
ASPECT_TOLERANCE_PX = 2      # |height - width * ah / aw| for FIT / FILL without an explicit width


def _size_of(doc: Optional[Dict[str, Any]]) -> Optional[tuple]:
    v = (doc or {}).get("video") or {}
    w, h = v.get("width"), v.get("height")
    return (int(w), int(h)) if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0 else None


def _has_audio(doc: Optional[Dict[str, Any]]) -> Optional[bool]:
    if not doc:
        return None
    return bool(doc.get("audio"))


def _fps_of(doc: Optional[Dict[str, Any]]) -> Optional[float]:
    v = (doc or {}).get("video") or {}
    try:
        f = float(v.get("fps") or 0)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _hdr_of(video: Dict[str, Any]) -> bool:
    """The colour system ffmpeg-skill's encoders route on: BT.2020 primaries or a PQ/HLG transfer. ffmpeg-skill 2.0
    narrowed probe's `hdr` to PQ/HLG/Dolby Vision only and moved the wider meaning to `bt2020_or_hdr`; before 2.0, `hdr`
    carried it and `bt2020_or_hdr` did not exist. Whichever key the probe has is read with that one meaning."""
    if "bt2020_or_hdr" in video:
        return bool(video.get("bt2020_or_hdr"))
    return bool(video.get("hdr"))


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


class Executor:
    def __init__(self, project: EditProject, skill: FfmpegSkill, tool_versions: Dict[str, Optional[str]], log=None,
                 engine_doctor: Optional[Dict[str, Any]] = None):
        self.project = project
        self.skill = skill
        self.tool_versions = {"ffmpeg-skill": skill.version, **tool_versions}
        self.engine_doctor = engine_doctor or {}
        self.log = log or (lambda msg: None)
        self.steps: Dict[str, Step] = compile_project(project)
        self.work = project.policy.work_dir(create=False)
        self.durations: Dict[str, Optional[Time]] = {}
        self.source_probes: Dict[str, Dict[str, Any]] = {}
        self.clips: Dict[str, Clip] = {}
        self.results: List[Dict[str, Any]] = []
        self.produced: Dict[str, str] = {}          # op ref -> validated intermediate path
        self.keys: Dict[str, str] = {}              # op ref -> idempotency key
        self.request_sha256: Optional[str] = None   # sha256 of the canonical request document (set by the CLI)

    # ------------------------------------------------------------ preparation
    def probe_sources(self) -> None:
        """Every source is probed by ffmpeg-skill before anything runs: a video needs a video stream and a duration, an image
        must decode to a frame. What the probe says is the source's OBSERVED profile (frame size, fps, audio) used for
        the media-compatibility checks and for validating outputs."""
        for ref in sorted(self.project.sources):
            src = self.project.sources[ref]
            doc = probe(self.skill, src.path)
            if src.kind == "image":
                if _size_of(doc) is None:
                    raise EditError("INVALID_INPUT", f"source {ref!r} does not decode as an image", {"source": ref, "reason": "image_undecodable"})
                self.source_probes[ref] = doc
                continue
            if not doc.get("video") or _size_of(doc) is None:
                raise EditError("INVALID_INPUT", f"source {ref!r} has no video stream", {"source": ref, "reason": "no_video_stream"})
            d = doc.get("duration")
            if not d or float(d) <= 0:
                raise EditError("INVALID_INPUT", f"source {ref!r} has no duration (a still image or a broken container is not a video source)",
                                {"source": ref, "reason": "no_duration"})
            self.source_probes[ref] = doc
            self.durations[ref] = Time.from_float(float(d))
        self.clips = build_timelines(self.project, self.durations)
        self._check_engine()
        self._check_ranges()
        self._check_media()
        self._compute_keys()

    def _check_engine(self) -> None:
        """Every operation's engine tool and its encoders / filters must be present (ffmpeg-skill doctor) before anything runs;
        a gap is TOOL_ERROR (not retryable), never a late ffmpeg failure."""
        from .contract import TOOL_REQUIREMENTS
        for ref in self.project.order:
            op = self.project.operations[ref]
            script = op.tool.split("/", 1)[1]
            if script not in self.skill.tools:
                raise EditError("TOOL_ERROR", f"operation {ref!r} ({op.type}) needs {op.tool}, which this ffmpeg-skill checkout does not provide",
                                {"operation": ref, "tool": op.tool, "missing": [f"tool:{op.tool}"]}, retryable=False)
            gaps = missing_capabilities(self.engine_doctor, TOOL_REQUIREMENTS[op.tool]) if self.engine_doctor else []
            gaps += [f"flag:{f}" for f in self.skill.missing_flags(script, self.steps[ref].flags())]   # detected, not version-guessed
            if op.stinger is not None:   # butt join, then the stinger over each join, then (with sound) its audio mixed in (ADR-023)
                needs = [("overlay", ["--video", "--place", "--position", "--margin", "--scale"])]
                if self.profile(op.stinger).get("audio"):
                    needs += [("broll", ["--insert", "--at", "--duration", "--audio"]), ("audio", ["--replace"])]
                for tool, flags in needs:
                    if tool not in self.skill.tools:
                        gaps.append(f"tool:ffmpeg-skill/{tool}")
                    else:
                        gaps += [f"flag:ffmpeg-skill/{tool} {f}" for f in self.skill.missing_flags(tool, flags)]
            if op.type == "CONDENSE":   # cut each piece, fit --duration a sped one, join them (ADR-026)
                needs = [("cut", ["--start", "--end", "--accurate"]), ("join", ["--transition", "--width", "--height", "--fps"])]
                if any("factor" in r for r in list(op.params["rules"].values()) + [op.params["default"]]):
                    needs.append(("fit", ["--duration", "--method", "--max-speed"]))
                for tool, flags in needs:
                    if tool not in self.skill.tools:
                        gaps.append(f"tool:ffmpeg-skill/{tool}")
                    else:
                        gaps += [f"flag:ffmpeg-skill/{tool} {f}" for f in self.skill.missing_flags(tool, flags)]
            if op.type == "PUNCH_IN":   # cut the range out, crop and scale it back, join the parts (ADR-020)
                for tool, flags in (("cut", ["--start", "--end", "--accurate"]), ("fit", ["--aspect", "--fit", "--width"]),
                                    ("join", ["--transition", "--width", "--height", "--fps"]), ("crop", ["--x", "--y", "--width", "--height"])):
                    if tool not in self.skill.tools:
                        gaps.append(f"tool:ffmpeg-skill/{tool}")
                    else:
                        gaps += [f"flag:ffmpeg-skill/{tool} {f}" for f in self.skill.missing_flags(tool, flags)]
            if self.segmented(ref):   # each keep range is cut alone and the parts joined by stream copy (ADR-018)
                if "join" not in self.skill.tools:
                    gaps.append("tool:ffmpeg-skill/join")
                else:
                    gaps += [f"flag:ffmpeg-skill/join {f}" for f in self.skill.missing_flags("join", ["--transition", "--copy"])]
            if gaps:
                raise EditError("TOOL_ERROR", f"operation {ref!r} ({op.type}) needs {', '.join(gaps)}, which ffmpeg-skill's doctor did not find",
                                {"operation": ref, "tool": op.tool, "missing": gaps}, retryable=False)

    # ------------------------------------------------------------ media compatibility
    def profile(self, ref: str) -> Dict[str, Any]:
        """Media profile of a source or of an operation's result: {audio, width, height, fps}. Sources and already
        produced intermediates come from probes (OBSERVED); a not-yet-produced operation is derived from its inputs and
        parameters (EXPECTED). Unknown facts are None, never guessed."""
        if ref in self.project.sources:
            return self._profile_of_doc(self.source_probes.get(ref), "OBSERVED")
        rec = next((r for r in self.results if r["operation"] == ref and r.get("probe")), None)
        if rec is not None:
            return self._profile_of_doc(rec["probe"], "OBSERVED")
        op = self.project.operations[ref]
        first = self.profile(op.inputs[0])
        prof: Dict[str, Any] = {"audio": first["audio"], "width": first["width"], "height": first["height"], "fps": first["fps"],
                                "hdr": first["hdr"], "vfr": False, "provenance": "EXPECTED"}
        if op.type == "CONCAT":
            ins = [self.profile(r) for r in op.video_inputs]
            audios = [i["audio"] for i in ins]
            prof["audio"] = True if any(a is True for a in audios) else (False if all(a is False for a in audios) else None)
        if op.type == "IMAGE_INSERT":   # insert.py writes a silent SDR clip whatever the still is
            prof["audio"], prof["hdr"] = False, False
        frame = self.target_frame(ref)
        if frame is not None:
            prof["width"], prof["height"] = frame
        fps = self.target_fps(ref)
        if fps is not None:
            prof["fps"] = fps
        return prof

    # ------------------------------------------------------------ normalization (frame_semantics)
    def target_frame(self, ref: str) -> Optional[tuple]:
        """The frame an operation must deliver, computed before execution from its parameters and the (measured or expected)
        input frame (the image's, for IMAGE_INSERT) with ffmpeg-skill's own rule; None when it is the input's frame (TRIM / CUT / SPEED /
        OVERLAY / FREEZE / REVERSE) and that is not known yet."""
        op = self.project.operations[ref]
        p = op.params
        first = self.profile(op.inputs[0])
        sw, sh = first["width"], first["height"]
        if op.type == "IMAGE_INSERT":   # insert.py: both given -> as given; one given -> the other from the image aspect; none -> the image; each even()
            if "width" in p and "height" in p:
                return (even(p["width"]), even(p["height"]))
            if "width" in p:
                return (even(p["width"]), even(p["width"] * sh / sw) if sw and sh else None)
            if "height" in p:
                return (even(p["height"] * sw / sh) if sw and sh else None, even(p["height"]))
            return (even(sw), even(sh)) if sw and sh else None
        if op.type == "CROP":   # crop.py: exactly the rectangle (ADR-016)
            return (p["width"], p["height"])
        if op.type == "PUNCH_IN":   # zoomed and scaled back: the input frame
            return (sw, sh) if sw and sh else None
        if op.type == "ROTATE":
            if p.get("degrees") in (90, 270) and sw and sh:
                return (sh, sw)
            return (sw, sh) if sw and sh else None
        if op.type == "CONCAT":   # join.py: both given -> as given; one given -> the other from the first input's aspect (round); none -> the first input; floored to even
            if "width" in p and "height" in p:
                w, h = p["width"], p["height"]
            elif "width" in p and sw and sh:
                w, h = p["width"], int(round(p["width"] * sh / sw))
            elif "height" in p and sw and sh:
                w, h = int(round(p["height"] * sw / sh)), p["height"]
            elif sw and sh:
                w, h = sw, sh
            else:
                return None
            return (w - (w % 2), h - (h % 2))
        if op.type == "RESIZE":
            if sw and sh:
                return (even(p["width"]), even(p["width"] * sh / sw))
            return (even(p["width"]), None)
        if op.type in ("FIT", "FILL"):
            aw, ah = (int(x) for x in p["aspect"].split(":"))
            ratio = aw / ah
            if "width" in p:
                w = even(p["width"])
            elif sw and sh and self.skill.fit_bounds_by_source():
                # fit.py from 0.16.10: the canvas takes the aspect without exceeding the source in either dimension
                if ratio <= sw / sh:
                    h = even(sh)
                    return (even(h * ratio), h)
                w = even(sw)
            elif sw and sh:
                # fit.py before 0.16.10: the canvas keeps the source width (narrower aspect) or height (wider aspect)
                w = even(sw if ratio <= sw / sh else sh * ratio)
            else:
                return None
            return (w, even(w / ratio))
        return (sw, sh) if sw and sh else None

    def target_fps(self, ref: str) -> Optional[float]:
        op = self.project.operations[ref]
        if "fps" in op.params:
            return float(op.params["fps"])
        return None

    def normalized(self, ref: str) -> Dict[str, Any]:
        """Normalized parameters: what the request means once the sources are measured (frame_semantics, SPEED duration, audio
        expectation, encoding). Reported in plan steps and execution records; verified on the output."""
        op = self.project.operations[ref]
        first = self.profile(op.inputs[0])
        frame = self.target_frame(ref)
        clip = self.clips.get(ref)
        d: Dict[str, Any] = {"params": params_to_json(op.params),
                             "source_frame": [first["width"], first["height"]] if first["width"] and first["height"] else None,
                             "target_frame": [frame[0], frame[1]] if frame and frame[0] and frame[1] else None,
                             "target_fps": self.target_fps(ref),
                             "target_duration": clip.duration.to_dict() if clip is not None and clip.duration is not None else None,
                             "audio": self.profile_expected_audio(op),
                             "hdr": first["hdr"],
                             "video_codec": "hevc" if first["hdr"] else ("h264" if first["hdr"] is False else None),
                             "encoding": {**ENCODING_DEFAULTS, **(op.encoding or {})} if self._reencodes(op) else None}
        return d

    @staticmethod
    def _reencodes(op) -> bool:
        return not (op.type in ("TRIM", "CUT") and op.params.get("precision") in ("keyframe", "auto"))

    @staticmethod
    def _profile_of_doc(doc: Optional[Dict[str, Any]], provenance: str) -> Dict[str, Any]:
        """A probe document as a profile. A source whose rotation metadata is ±90 / 270 is measured with width and height
        swapped, exactly as ffmpeg-skill's fit.py / join.py do before they compute a target frame."""
        size = _size_of(doc)
        v = (doc or {}).get("video") or {}
        if size and v.get("rotation") in (90, -90, 270, -270):
            size = (size[1], size[0])
        return {"audio": _has_audio(doc), "width": size[0] if size else None, "height": size[1] if size else None, "fps": _fps_of(doc),
                "hdr": _hdr_of(v) if doc else None, "vfr": bool(v.get("variable_frame_rate_suspected")) if doc else None,
                "provenance": provenance if doc else None}

    def _check_media(self) -> None:
        """Refuse, before any tool runs, every input the operation cannot take (operations.MEDIA, contract.media_policy); warn
        about what the engine will convert on its own (VFR conform, HDR -> HEVC)."""
        for ref in sorted(self.project.sources):
            prof = self.profile(ref)
            if prof.get("vfr"):
                self.project.warnings.append(f"source {ref!r} looks variable-frame-rate; ffmpeg-skill conforms it to constant fps")
            if prof.get("hdr"):
                self.project.warnings.append(f"source {ref!r} is HDR; ffmpeg-skill encodes it as HEVC 10-bit (output codec hevc)")
        for ref in self.project.order:
            op = self.project.operations[ref]
            req = MEDIA[op.type]["requires"]
            video_inputs = [] if op.type == "IMAGE_INSERT" else op.video_inputs
            if op.stinger is not None:
                self._check_stinger(ref)
            if op.type == "CONCAT":
                hdrs = {r: self.profile(r).get("hdr") for r in video_inputs}
                if any(h is True for h in hdrs.values()) and any(h is False for h in hdrs.values()):
                    raise EditError("INVALID_INPUT", f"operation {ref!r} (CONCAT): HDR and SDR inputs cannot be joined (the engine encodes from the first input's colour system)",
                                    {"operation": ref, "inputs": hdrs, "reason": "hdr_mismatch"})
            if op.type in ("TRIM", "CUT", "SPEED", "OVERLAY", "ROTATE", "FREEZE", "REVERSE", "PUNCH_IN", "CONDENSE"):   # these keep the input frame; the engine's encoders need even sizes
                prof = self.profile(video_inputs[0])
                if prof["width"] and prof["height"] and (prof["width"] % 2 or prof["height"] % 2):
                    raise EditError("INVALID_INPUT", f"operation {ref!r} ({op.type}): input {video_inputs[0]!r} frame {prof['width']}x{prof['height']} has an odd dimension; "
                                    f"{op.type} keeps the input frame and the engine's encoder needs even sizes; run RESIZE, FIT, FILL or CONCAT on it first",
                                    {"operation": ref, "input": video_inputs[0], "frame": [prof["width"], prof["height"]], "reason": "odd_frame"})
            for r in video_inputs:
                prof = self.profile(r)
                if req["audio"] and prof["audio"] is False:
                    raise EditError("INVALID_INPUT", f"operation {ref!r} ({op.type}): input {r!r} has no audio stream and {op.type} needs one; "
                                    "add audio upstream or use a source with audio",
                                    {"operation": ref, "input": r, "reason": "audio_required"})
            if op.type == "CROP":   # crop.py refuses a rectangle outside the frame; refuse it here before anything runs
                prof = self.profile(op.inputs[0])
                p = op.params
                if prof["width"] and prof["height"] and (p["x"] + p["width"] > prof["width"] or p["y"] + p["height"] > prof["height"]):
                    raise EditError("INVALID_REQUEST", f"operation {ref!r} (CROP): rectangle ({p['x']},{p['y']},{p['width']}x{p['height']}) does not lie inside the "
                                    f"input frame {prof['width']}x{prof['height']} of {op.inputs[0]!r}",
                                    {"operation": ref, "input": op.inputs[0], "frame": [prof["width"], prof["height"]],
                                     "rectangle": {k: p[k] for k in ("x", "y", "width", "height")}, "reason": "crop_outside_frame"})
            if op.type == "PUNCH_IN":   # the window must hold the subject box: refused before anything runs
                prof = self.profile(op.inputs[0])
                if prof["width"] and prof["height"]:
                    try:
                        punch_window(prof["width"], prof["height"], op.params["scale"], op.params["subject"])
                    except EditError as exc:
                        exc.details = dict(exc.details or {}, operation=ref, input=op.inputs[0])
                        raise
            if op.type in ("OVERLAY", "IMAGE_INSERT"):
                img = self.profile(op.inputs[-1])
                if img["width"] is None:
                    raise EditError("INVALID_INPUT", f"operation {ref!r}: image {op.inputs[-1]!r} has no frame size", {"reason": "image_undecodable"})

    def _check_ranges(self) -> None:
        for ref in self.project.order:
            op = self.project.operations[ref]
            inp = op.inputs[0]
            limit = self.clips[inp].duration if inp in self.clips else self.durations.get(inp)
            if limit is None:
                continue
            ranges = []
            if op.type in ("TRIM", "PUNCH_IN"):
                ranges = [(op.params["start"], op.params["end"])]
            elif op.type == "CUT":
                ranges = [(r["start"], r["end"]) for r in op.params["keep"]]
            elif op.type == "CONDENSE":
                ranges = [(r["start"], r["end"]) for r in op.params["segments"]]
                if not condense_played(op.params, condense_pieces(op.params, limit)):
                    if op.params.get("output") == "removed":
                        raise EditError("INVALID_REQUEST", f"operation {ref!r} (CONDENSE output removed): no span is cut, so there is nothing to review",
                                        {"reason": "nothing_removed"})
                    raise EditError("INVALID_REQUEST", f"operation {ref!r} (CONDENSE): every span is cut; nothing would be kept", {"reason": "nothing_kept"})
            elif op.type == "FREEZE" and "at" in op.params:
                at = op.params["at"]
                if not at.value < limit.value:   # the last frame is held by omitting `at`
                    raise EditError("INVALID_TIME_RANGE", f"operation {ref!r}: at {at.text(3)}s is at or beyond the input duration {limit.text(3)}s; "
                                    "omit at to hold the last frame")
            elif op.type == "OVERLAY":
                for k in ("start", "end"):
                    t = op.params.get(k)
                    if t is not None and not t.value <= limit.value:
                        raise EditError("INVALID_TIME_RANGE", f"operation {ref!r}: {k} {t.text(3)}s is beyond the input duration {limit.text(3)}s")
            for a, b in ranges:
                if not a.value < limit.value:
                    raise EditError("INVALID_TIME_RANGE", f"operation {ref!r}: start {a.text(3)}s is at or beyond the input duration {limit.text(3)}s")
                if b.value > limit.value + 0.1:  # 100 ms: container duration vs last frame fuzz
                    raise EditError("INVALID_TIME_RANGE", f"operation {ref!r}: end {b.text(3)}s is beyond the input duration {limit.text(3)}s")
                if b.value > limit.value:
                    self.project.warnings.append(f"operation {ref!r}: end {b.text(3)}s clamped to the input duration {limit.text(3)}s")
            if op.type == "CONCAT" and op.stinger is not None:
                self._check_stinger_ranges(ref)
            elif op.type == "CONCAT" and op.params.get("transition"):
                d = op.params["transition"]["duration"].value
                for i in op.inputs:
                    c = self.clips.get(i)
                    dur = c.duration if c else self.durations.get(i)
                    if dur is not None and dur.value <= d * 2:
                        raise EditError("INVALID_TIME_RANGE", f"operation {ref!r}: input {i!r} ({dur.text(3)}s) is too short for a {float(d):g}s transition")

    def _compute_keys(self) -> None:
        for ref in self.project.order:
            op = self.project.operations[ref]
            key: Dict[str, Any] = {"operation_id": op.operation_id, "tool": op.tool, "tool_versions": self.tool_versions,
                                   "skill_version": VERSION, "container": output_extension(self.project, ref)}
            if op.encoding:
                key["encoding"] = dict(op.encoding)
            if self.segmented(ref):   # a segment render is other bytes than the single-pass one: never reused for it
                key["render"] = "segments"
            self.keys[ref] = stable_hash(key)

    # ------------------------------------------------------------ segment cache (ADR-018)
    def segmented(self, ref: str) -> bool:
        """options.segment_cache: a frame-precision CUT renders every keep range on its own (cut.py --start / --end --accurate),
        keeps each part in a content-keyed cache and joins the parts by stream copy (join.py --copy). A keyframe-precision CUT
        already stream-copies, and every other type is one engine call, so only this case changes."""
        op = self.project.operations[ref]
        return bool(self.project.options.segment_cache) and op.type == "CUT" and op.params.get("precision") == "frame"

    def _segments_dir(self) -> str:
        return os.path.join(self.work, "segments")

    def segment_key(self, ref: str, rng: Dict[str, Time]) -> str:
        """What a cached part depends on, and nothing else: the input's identity (the source's bytes or the upstream
        operation's id), the range, the encoding asked for, the container, the tool and its versions, and the flags the
        installed engine release adds. The rest of the keep list is not part of it, so changing one range leaves the others'
        keys as they were."""
        op = self.project.operations[ref]
        inp = op.inputs[0]
        ident = self.project.sources[inp].identity if inp in self.project.sources else self.project.operations[inp].operation_id
        return stable_hash({"kind": "segment", "input": ident, "start": rng["start"].to_dict()["rational"], "end": rng["end"].to_dict()["rational"],
                            "tool": "ffmpeg-skill/cut", "tool_versions": self.tool_versions, "skill_version": VERSION,
                            "container": output_extension(self.project, ref), "encoding": dict(op.encoding) if op.encoding else None,
                            "engine_args": sorted(self._engine_args(op))})

    def _segment_paths(self, ref: str, key: str) -> tuple:
        base = os.path.join(self._segments_dir(), "seg_" + key[:24])
        return base + output_extension(self.project, ref), base + ".json"

    def _cached_segment(self, ref: str, key: str) -> Optional[Dict[str, Any]]:
        if not self.project.options.reuse:
            return None
        media, rec_path = self._segment_paths(ref, key)
        if not (os.path.isfile(media) and os.path.isfile(rec_path)):
            return None
        try:
            import json
            with open(rec_path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            return None
        if rec.get("key") != key or rec.get("size") != os.path.getsize(media) or rec.get("sha256") != sha256_file(media):
            return None
        return rec

    def segments_plan(self, ref: str) -> List[Dict[str, Any]]:
        op = self.project.operations[ref]
        out = []
        for i, rng in enumerate(op.params["keep"]):
            key = self.segment_key(ref, rng)
            out.append({"index": i, "range": {"start": rng["start"].to_dict(), "end": rng["end"].to_dict()}, "key": key,
                        "cached": self._cached_segment(ref, key) is not None})
        return out

    # ------------------------------------------------------------ plan
    def plan(self, preview: bool = True) -> Dict[str, Any]:
        # ffmpeg-skill from 1.x refuses a dry run whose output directory does not exist ("this tool never creates
        # directories"); the work directory is created for the previews and removed again, so a plan still writes nothing
        created = self._make_missing_dirs(self.work) if preview else []
        try:
            return self._plan(preview)
        finally:
            for d in reversed(created):
                try:
                    os.rmdir(d)
                except OSError:
                    pass

    def _make_missing_dirs(self, path: str) -> List[str]:
        missing: List[str] = []
        d = path
        while not os.path.isdir(d) and is_within(self.project.policy.workspace, d) and d != os.path.dirname(d):
            missing.append(d)
            d = os.path.dirname(d)
        created: List[str] = []
        for d in reversed(missing):
            try:
                os.mkdir(d)
            except OSError:
                break
            created.append(d)
        return created

    def _plan(self, preview: bool) -> Dict[str, Any]:
        steps: List[Dict[str, Any]] = []
        for ref in self.project.order:
            st = self.steps[ref]
            d: Dict[str, Any] = st.to_dict()
            d["idempotency_key"] = self.keys.get(ref)
            d["normalized"] = self.normalized(ref) if self.source_probes else None
            enc = self.project.operations[ref].encoding
            d["encoding"] = dict(enc) if enc else None
            d["timeline"] = self.clips[ref].to_dict() if ref in self.clips else None
            d["intermediate"] = os.path.join(self.work, self.project.operations[ref].operation_id + output_extension(self.project, ref))
            d["reusable"] = self._reusable(ref) is not None
            if self.segmented(ref):
                d["segments"] = self.segments_plan(ref)
            if self.project.operations[ref].type == "PUNCH_IN" and self.source_probes:
                d["punch_in"] = self.punch_plan(ref)
            if self.project.operations[ref].type == "CONDENSE" and self.source_probes:
                d["condense"] = self.condense_plan(ref)
            if self.project.operations[ref].stinger is not None and self.source_probes:
                d["stinger"] = self.stinger_plan(ref)
            if preview:
                d["preview"] = self._preview(ref)
            steps.append(d)
        return {"work_dir": self.work, "steps": steps}

    def _preview(self, ref: str) -> Dict[str, Any]:
        """ffmpeg-skill --dry-run for one step: the commands it would run. Nothing is written."""
        if self.project.operations[ref].type == "CONDENSE":
            return {"ok": True, "commands": [], "note": "CONDENSE runs ffmpeg-skill cut per piece, fit --duration per sped piece and join; each call's commands are recorded when it runs"}
        if self.project.operations[ref].type == "PUNCH_IN":
            return {"ok": True, "commands": [], "note": "PUNCH_IN runs ffmpeg-skill cut, crop, fit and join in turn; each call's commands are recorded when it runs"}
        if self.project.operations[ref].stinger is not None:
            return {"ok": True, "commands": [], "note": "a stinger CONCAT runs ffmpeg-skill join, overlay (and broll + audio for its sound) in turn; "
                                                         "each call's commands are recorded when it runs"}
        try:
            argv = self._argv(ref, dry_run=True)
            run = run_tool(self.skill, self.steps[ref].script, argv, timeout=120, dry_run=True)
            doc = run.document if isinstance(run.document, dict) else {}
            return {"ok": run.ok, "commands": list(doc.get("commands", [])), "note": None if run.ok else (run.stderr_tail or "dry-run failed")}
        except EditError as exc:
            return {"ok": False, "commands": [], "note": exc.message}

    # ------------------------------------------------------------ run
    def run(self) -> Dict[str, Any]:
        started = now_iso()
        status = "completed"
        error: Optional[EditError] = None
        done: List[str] = []
        try:
            self.work = self.project.policy.work_dir(create=True)
            for ref in self.project.order:
                if cancelled():
                    raise EditError("CANCELLED", "interrupted before operation " + ref)
                self._run_step(ref)
                done.append(ref)
            self._deliver()
        except EditError as exc:
            status = "cancelled" if exc.code == "CANCELLED" else "failed"
            error = exc
            for ref in self.project.order:   # every operation is accounted for: the ones after a failure are recorded as skipped
                if ref not in done and not any(r["operation"] == ref for r in self.results):
                    self.results.append(self._record(self.project.operations[ref], self.steps[ref], "skipped", now_iso(), None, None, None, [], 0.0,
                                                     note="not run: an earlier operation failed" if status == "failed" else "not run: cancelled"))
        if status == "completed" and self.results and all(r["status"] == "reused" for r in self.results):
            status = "reused"
        doc: Dict[str, Any] = {"status": status, "started_at": started, "finished_at": now_iso(), "work_dir": self.work,
                               "request_sha256": self.request_sha256, "engine": self.skill.to_dict(),
                               "reused": status == "reused",
                               "sources": self._sources_doc(),
                               "operations": self.results, "outputs": self._outputs_doc(status in ("completed", "reused"))}
        if error is not None:
            doc["error"] = error.to_dict()
            raise _Failed(error, doc)
        return doc

    def _sources_doc(self) -> List[Dict[str, Any]]:
        docs = []
        for ref in sorted(self.project.sources):
            src = self.project.sources[ref]
            d = src.to_dict()
            d["observation"] = {"kind": "media.probe", "provenance": "OBSERVED", "source": f"ffmpeg-skill/probe@{self.skill.version}",
                                "data": self.source_probes.get(ref)} if ref in self.source_probes else None
            docs.append(d)
        return docs

    def _argv(self, ref: str, dry_run: bool = False) -> List[str]:
        st = self.steps[ref]
        op = self.project.operations[ref]
        inputs = [self._path_of(r, dry_run) for r in st.positional_inputs]
        image = self._path_of(st.image_input, dry_run) if st.image_input else None
        out = self._partial(ref)
        dur = None
        if st.needs_input_duration:
            c = self.clips.get(op.inputs[0])
            dur = c.duration if c else self.durations.get(op.inputs[0])
            if dur is None and not dry_run:
                dur = self._probe_duration(inputs[0])
            if dur is None:
                dur = Time.from_float(10.0)  # dry-run placeholder only; the real run probes
        return st.argv_for(inputs, out, image, dur, resolve=lambda flag: self.skill.resolve_flag(st.script, flag), engine_args=self._engine_args(op))

    def _engine_args(self, op) -> Dict[str, Any]:
        """Flags that depend on the installed release, not on the request. ffmpeg-skill 3.0 made cut.py keep an SDR HEVC
        source as HEVC when it re-encodes; every other re-encoding tool (and cut.py before 3.0) writes H.264 for SDR, which
        is what output validation and the contract's one-codec-per-colour rule expect. A re-encoding TRIM / CUT on such a
        checkout passes --no-keep-hevc (ADR-017); a keyframe-precision one stream-copies and keeps the source codec."""
        if op.tool == "ffmpeg-skill/cut" and self._reencodes(op) and self.skill.cut_keeps_hevc_by_default():
            return {"no_keep_hevc": True}
        return {}

    def _partial(self, ref: str) -> str:
        op = self.project.operations[ref]
        return os.path.join(self.work, f"{op.operation_id}.partial.{os.getpid()}{output_extension(self.project, ref)}")

    def _path_of(self, ref: str, dry_run: bool) -> str:
        if ref in self.project.sources:
            return self.project.sources[ref].path
        if ref in self.produced:
            return self.produced[ref]
        if dry_run:
            return os.path.join(self.work, self.project.operations[ref].operation_id + output_extension(self.project, ref))
        raise EditError("INTERNAL_ERROR", f"input {ref!r} has not been produced")

    def _probe_duration(self, path: str) -> Time:
        doc = probe(self.skill, path)
        if not doc.get("duration"):
            raise EditError("VALIDATION_ERROR", f"{os.path.basename(path)}: no duration")
        return Time.from_float(float(doc["duration"]))

    def _reusable(self, ref: str) -> Optional[Dict[str, Any]]:
        if not self.project.options.reuse:
            return None
        op = self.project.operations[ref]
        final = os.path.join(self.work, op.operation_id + output_extension(self.project, ref))
        rec_path = os.path.join(self.work, op.operation_id + ".json")
        if not (os.path.isfile(final) and os.path.isfile(rec_path)):
            return None
        try:
            import json
            with open(rec_path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            return None
        if rec.get("idempotency_key") != self.keys.get(ref) or rec.get("size") != os.path.getsize(final):
            return None
        return rec

    def _run_step(self, ref: str) -> None:
        op = self.project.operations[ref]
        st = self.steps[ref]
        ext = output_extension(self.project, ref)
        final = os.path.join(self.work, op.operation_id + ext)
        partial = self._partial(ref)
        t0 = now_iso()
        rec = self._reusable(ref)
        if rec is not None and sha256_file(final) == rec.get("sha256"):
            # a candidate for reuse is re-validated like a fresh output (exists, non-empty, probe, expected duration / frame /
            # audio); a record that no longer validates is discarded and the operation runs again
            try:
                doc = self._validate(ref, final)
            except EditError as exc:
                self.log(f"reuse candidate for {ref} no longer validates ({exc.message}); running again")
                rec = None
            else:
                self.produced[ref] = final
                self.results.append(self._record(op, st, "reused", t0, final, rec["sha256"], doc, [], 0.0))
                self.log(f"reused {ref} ({op.operation_id})")
                return
        _remove(partial)
        _remove(final)
        _remove(os.path.join(self.work, op.operation_id + ".json"))
        self._sweep_partials(op.operation_id)
        segments: Optional[Dict[str, Any]] = None
        engine_doc: Optional[Dict[str, Any]] = None
        if self.segmented(ref):
            commands, seconds, segments = self._run_segments(ref, partial, t0)
        elif op.type == "PUNCH_IN":
            commands, seconds = self._run_punch_in(ref, partial, t0)
        elif op.type == "CONDENSE":
            commands, seconds = self._run_condense(ref, partial, t0)
        elif op.stinger is not None:
            commands, seconds = self._run_stinger(ref, partial, t0)
        else:
            argv = self._argv(ref)
            self.log(f"run {ref}: {st.tool}")
            run = run_tool(self.skill, st.script, argv, timeout=self.project.options.timeout_seconds)
            commands, seconds = run.commands, run.seconds
            engine_doc = run.document if isinstance(run.document, dict) else None
            if not run.ok:
                _remove(partial)
                err = tool_error(run, f"operation {ref!r} ({st.tool})")
                self.results.append(self._record(op, st, "failed", t0, None, None, None, run.commands, run.seconds, err))
                raise err
        try:
            doc = self._validate(ref, partial)
        except EditError as exc:
            _remove(partial)
            self.results.append(self._record(op, st, "failed", t0, None, None, None, commands, seconds, exc, segments=segments))
            raise
        os.replace(partial, final)
        digest = sha256_file(final)
        record = {"idempotency_key": self.keys[ref], "operation_id": op.operation_id, "sha256": digest, "size": os.path.getsize(final), "probe": doc}
        import json
        tmp = os.path.join(self.work, op.operation_id + ".json.partial")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh, sort_keys=True)
        os.replace(tmp, os.path.join(self.work, op.operation_id + ".json"))
        self.produced[ref] = final
        self.results.append(self._record(op, st, "completed", t0, final, digest, doc, commands, seconds, segments=segments))
        if op.type == "PUNCH_IN":
            self.results[-1]["punch_in"] = self.punch_plan(ref)
        if op.type == "CONDENSE":
            self.results[-1]["condense"] = self.condense_plan(ref)
        if op.stinger is not None:
            self.results[-1]["stinger"] = self.stinger_plan(ref)
        if engine_doc is not None and op.type in ("TRIM", "CUT") and op.params.get("precision") in ("keyframe", "auto"):
            self.results[-1]["cuts"] = self._cut_report(op, engine_doc)

    def _cut_report(self, op, doc: Dict[str, Any]) -> Dict[str, Any]:
        """What cut.py measured and chose for a lossless-first TRIM / CUT (ADR-019): copy, accurate (re-encoded) or hybrid
        (a copy that missed the tolerance and was re-encoded), why, and how far the result is from the asked range. The
        engine's own keys, passed through as OBSERVED; a key an older release does not report is null, never guessed."""
        keys = ("mode", "reencoded", "reencode_reason", "precision", "segment_precision", "segment_end_snap_seconds", "duration_delta_seconds",
                "nearest_keyframes", "edit_list", "keyframe_snapped")
        rep: Dict[str, Any] = {"requested_precision": op.params.get("precision"), "provenance": "OBSERVED", "source": f"ffmpeg-skill/cut@{self.skill.version}"}
        rep.update({k: doc.get(k) for k in keys})
        rep["ranges"] = len(op.params["keep"]) if op.type == "CUT" else 1
        return rep

    def _run_segments(self, ref: str, partial: str, t0: str) -> tuple:
        """The segment render of a CUT (ADR-018): every keep range is taken from the cache when its key is there, else cut on
        its own and validated before it is cached; then the parts are joined by stream copy into `partial`. Returns
        (commands, seconds, report). When the engine refuses the copy join (its parts' streams do not line up), the
        operation is rendered in one pass as without the cache, and the report says so."""
        op = self.project.operations[ref]
        st = self.steps[ref]
        os.makedirs(self._segments_dir(), exist_ok=True)
        commands: List[str] = []
        seconds = 0.0
        parts: List[Dict[str, Any]] = []
        files: List[str] = []
        src_path = self._path_of(op.inputs[0], False)
        want_audio = self.profile(op.inputs[0]).get("audio")
        for i, rng in enumerate(op.params["keep"]):
            if cancelled():
                raise EditError("CANCELLED", f"interrupted in operation {ref} (segment {i})")
            key = self.segment_key(ref, rng)
            media, rec_path = self._segment_paths(ref, key)
            part = {"index": i, "range": {"start": rng["start"].to_dict(), "end": rng["end"].to_dict()}, "key": key}
            cached = self._cached_segment(ref, key)
            if cached is not None:
                part.update({"status": "reused", "sha256": cached["sha256"], "seconds": 0.0})
                parts.append(part)
                files.append(media)
                continue
            seg_partial = os.path.join(self._segments_dir(), f"seg_{key[:24]}.partial.{os.getpid()}{output_extension(self.project, ref)}")
            for stale in (seg_partial, media, rec_path):
                _remove(stale)
            args: Dict[str, Any] = {"start": rng["start"].tool_arg(), "end": rng["end"].tool_arg(), "accurate": True}
            for k in ("crf", "preset"):
                if op.encoding and k in op.encoding:
                    args[k] = op.encoding[k]
            seg_step = Step(op, "ffmpeg-skill/cut", args, [op.inputs[0]])
            argv = seg_step.argv_for([src_path], seg_partial, resolve=lambda flag: self.skill.resolve_flag("cut", flag), engine_args=self._engine_args(op))
            self.log(f"run {ref} segment {i}: ffmpeg-skill/cut")
            run = run_tool(self.skill, "cut", argv, timeout=self.project.options.timeout_seconds)
            commands += run.commands
            seconds += run.seconds
            if not run.ok:
                _remove(seg_partial)
                err = tool_error(run, f"operation {ref!r} segment {i} (ffmpeg-skill/cut)")
                self.results.append(self._record(op, st, "failed", t0, None, None, None, commands, seconds, err, segments=self._segments_report(parts, None)))
                raise err
            try:
                doc = self._validate_segment(ref, i, seg_partial, (rng["end"] - rng["start"]).seconds, want_audio)
            except EditError as exc:
                _remove(seg_partial)
                self.results.append(self._record(op, st, "failed", t0, None, None, None, commands, seconds, exc, segments=self._segments_report(parts, None)))
                raise
            os.replace(seg_partial, media)
            digest = sha256_file(media)
            import json
            tmp = rec_path + ".partial"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"key": key, "sha256": digest, "size": os.path.getsize(media), "probe": doc}, fh, sort_keys=True)
            os.replace(tmp, rec_path)
            part.update({"status": "encoded", "sha256": digest, "seconds": run.seconds})
            parts.append(part)
            files.append(media)
        if len(files) == 1:
            shutil.copyfile(files[0], partial)
            return commands, seconds, self._segments_report(parts, "single")
        join = Step(op, "ffmpeg-skill/join", {"transition": "none", "copy": True}, [])
        argv = join.argv_for(files, partial, resolve=lambda flag: self.skill.resolve_flag("join", flag))
        self.log(f"run {ref}: ffmpeg-skill/join --copy ({len(files)} segments)")
        run = run_tool(self.skill, "join", argv, timeout=self.project.options.timeout_seconds)
        commands += run.commands
        seconds += run.seconds
        if run.ok:
            return commands, seconds, self._segments_report(parts, "stream_copy")
        _remove(partial)
        reason = tool_error(run, "join --copy").message
        self.log(f"{ref}: the engine refused the stream-copy join ({reason}); rendering in one pass")
        argv = self._argv(ref)
        run = run_tool(self.skill, st.script, argv, timeout=self.project.options.timeout_seconds)
        commands += run.commands
        seconds += run.seconds
        report = self._segments_report(parts, "single_pass_fallback")
        report["fallback_reason"] = reason
        if not run.ok:
            _remove(partial)
            err = tool_error(run, f"operation {ref!r} ({st.tool})")
            self.results.append(self._record(op, st, "failed", t0, None, None, None, commands, seconds, err, segments=report))
            raise err
        return commands, seconds, report

    # ------------------------------------------------------------ stinger transition (ADR-023)
    _ALPHA_FORMATS = ("yuva", "rgba", "bgra", "argb", "abgr", "gbrap", "ya8", "ya16", "pal8")

    def stinger_plan(self, ref: str) -> Dict[str, Any]:
        """Where each stinger goes: for every join, the cut (the first frame of the next clip), the stinger's start (cut - cut_point)
        and end, all on the output timeline; plus what the stinger is."""
        op = self.project.operations[ref]
        tr = op.params["transition"]
        sd = self.durations.get(op.stinger) if op.stinger else None
        joins = []
        t = Time(Fraction(0))
        known = True
        for r in op.video_inputs[:-1]:
            c = self.clips.get(r)
            d = c.duration if c is not None and c.duration is not None else self.durations.get(r)
            if d is None:
                known = False
                break
            t = t + d
            joins.append({"cut_at": t.to_dict(), "stinger_start": (t - tr["cut_point"]).to_dict(),
                          "stinger_end": (t - tr["cut_point"] + sd).to_dict() if sd is not None else None})
        prof = self.profile(op.stinger) if op.stinger else {}
        return {"source": op.stinger, "cut_point": tr["cut_point"].to_dict(), "cover": {k: tr["cover"][k].to_dict() for k in ("start", "end")},
                "duration": sd.to_dict() if sd is not None else None, "audio": prof.get("audio"), "joins": joins if known else None,
                "cover_check": "declared: the caller's covered window (stinger.json); not measured on the pixels here"}

    def _check_stinger(self, ref: str) -> None:
        op = self.project.operations[ref]
        st = op.stinger
        assert st is not None
        doc = self.source_probes.get(st) or {}
        v = doc.get("video") or {}
        what = f"operation {ref!r} (CONCAT stinger {st!r})"
        pix = str(v.get("pix_fmt") or "")
        if not pix.startswith(self._ALPHA_FORMATS):
            raise EditError("INVALID_INPUT", f"{what}: the stinger has no alpha plane (pixel format {pix or 'unknown'}); a stinger is laid over the join and must be "
                            "transparent outside its graphic (ProRes 4444, QuickTime Animation, PNG / QTRLE in MOV)",
                            {"operation": ref, "input": st, "pix_fmt": pix or None, "reason": "stinger_no_alpha"})
        frame = self.target_frame(ref)
        sp = self.profile(st)
        if frame and frame[0] and frame[1] and sp["width"] and sp["height"] and sp["width"] * frame[1] != sp["height"] * frame[0]:
            raise EditError("INVALID_INPUT", f"{what}: the stinger frame {sp['width']}x{sp['height']} is not the edit's aspect {frame[0]}x{frame[1]}, so it could "
                            "not cover the frame", {"operation": ref, "input": st, "reason": "stinger_aspect"})
        fps = self.target_fps(ref) or self.profile(op.video_inputs[0]).get("fps")
        sfps = sp.get("fps")
        if fps and sfps and abs(fps - sfps) > FPS_TOLERANCE:
            raise EditError("INVALID_INPUT", f"{what}: the stinger runs at {sfps:g} fps and the edit at {fps:g} fps; its cut point would fall between frames",
                            {"operation": ref, "input": st, "reason": "stinger_fps"})
        if sfps:
            cp = op.params["transition"]["cut_point"].value
            frames = cp * Fraction(sfps).limit_denominator(1001)
            if abs(frames - round(frames)) > Fraction(1, 1000):
                raise EditError("INVALID_REQUEST", f"{what}: cut_point {float(cp):.4f}s is not on a frame of the stinger ({sfps:g} fps)",
                                {"operation": ref, "reason": "cut_point_not_on_frame"})

    def _check_stinger_ranges(self, ref: str) -> None:
        op = self.project.operations[ref]
        tr = op.params["transition"]
        sd = self.durations.get(op.stinger) if op.stinger else None
        cp = tr["cut_point"]
        if sd is not None and not (cp.value < sd.value and tr["cover"]["end"].value <= sd.value + Fraction(1, 1000)):
            raise EditError("INVALID_TIME_RANGE", f"operation {ref!r}: the stinger is {sd.text(3)}s; its cut point and covered window must lie inside it")
        ins = op.video_inputs
        for i, r in enumerate(ins):
            c = self.clips.get(r)
            d = c.duration if c is not None and c.duration is not None else self.durations.get(r)
            if d is None:
                continue
            if i < len(ins) - 1 and d.value < cp.value:
                raise EditError("INVALID_TIME_RANGE", f"operation {ref!r}: input {r!r} ({d.text(3)}s) is shorter than the stinger's lead-in ({cp.text(3)}s)")
            if i > 0 and sd is not None and d.value < (sd - cp).value:
                raise EditError("INVALID_TIME_RANGE", f"operation {ref!r}: input {r!r} ({d.text(3)}s) is shorter than the stinger's tail ({(sd - cp).text(3)}s)")

    def _run_stinger(self, ref: str, partial: str, t0: str) -> tuple:
        """The clips are butt-joined (join.py, no transition: the cut at each join is the first frame of the next clip); the stinger
        is composited over each join from cut - cut_point (overlay.py --video --place, alpha respected), so the frame at the
        cut is the stinger's covered frame; with sound, the stinger's audio is mixed under each join (broll.py --audio mix) and
        that track replaces the composite's (audio.py --replace). Every call is an ffmpeg-skill tool."""
        op = self.project.operations[ref]
        st = self.steps[ref]
        plan = self.stinger_plan(ref)
        if not plan["joins"] or plan["duration"] is None:
            raise EditError("INVALID_INPUT", f"operation {ref!r}: the join times or the stinger's length are not known", {"operation": ref})
        assert op.stinger is not None
        ext = output_extension(self.project, ref)
        stinger_path = self._path_of(op.stinger, False)
        sd = self.durations.get(op.stinger)
        assert sd is not None
        frame = self.target_frame(ref)
        temps: List[str] = []
        commands: List[str] = []
        seconds = 0.0

        def call(tool: str, argv_tail: List[str], inputs: List[str]) -> str:
            nonlocal seconds
            out = os.path.join(self.work, f"{op.operation_id}.stg{len(temps)}.partial.{os.getpid()}{ext}")
            _remove(out)
            temps.append(out)
            argv = list(inputs) + ["-o", out]
            i = 0
            while i < len(argv_tail):   # held to ALLOWED_FLAGS, spelled as the installed engine spells it
                flag = argv_tail[i]
                if flag[2:].replace("-", "_") not in ALLOWED_FLAGS[tool]:
                    raise EditError("INTERNAL_ERROR", f"flag {flag} is not allowed for ffmpeg-skill/{tool}")
                spelled = self.skill.resolve_flag(tool, flag)
                if spelled is None:
                    raise EditError("TOOL_ERROR", f"ffmpeg-skill/{tool} has no {flag} flag", {"tool": f"ffmpeg-skill/{tool}", "missing": [f"flag:{flag}"]}, retryable=False)
                val = argv_tail[i + 1] if i + 1 < len(argv_tail) and not argv_tail[i + 1].startswith("--") else None
                if val is None:
                    argv.append(spelled)
                    i += 1
                else:
                    argv += [f"{spelled}={val}"] if val.startswith("-") else [spelled, val]
                    i += 2
            self.log(f"run {ref}: ffmpeg-skill/{tool}")
            run = run_tool(self.skill, tool, argv, timeout=self.project.options.timeout_seconds)
            commands.extend(run.commands)
            seconds += run.seconds
            if not run.ok:
                err = tool_error(run, f"operation {ref!r} (CONCAT stinger, ffmpeg-skill/{tool})")
                for t in temps:
                    _remove(t)
                self.results.append(self._record(op, st, "failed", t0, None, None, None, commands, seconds, err))
                raise err
            return out

        enc: List[str] = []
        for k in ("crf", "preset"):
            if op.encoding and k in op.encoding:
                enc += ["--" + k, str(op.encoding[k])]
        try:
            joined = os.path.join(self.work, f"{op.operation_id}.stg-join.partial.{os.getpid()}{ext}")
            temps.append(joined)
            join_argv = st.argv_for([self._path_of(r, False) for r in op.video_inputs], joined, resolve=lambda f: self.skill.resolve_flag("join", f))
            self.log(f"run {ref}: ffmpeg-skill/join")
            run = run_tool(self.skill, "join", join_argv, timeout=self.project.options.timeout_seconds)
            commands.extend(run.commands)
            seconds += run.seconds
            if not run.ok:
                err = tool_error(run, f"operation {ref!r} (ffmpeg-skill/join)")
                self.results.append(self._record(op, st, "failed", t0, None, None, None, commands, seconds, err))
                raise err
            places: List[str] = []
            for j in plan["joins"]:
                start = Time.parse(j["stinger_start"]["seconds"])
                places += ["--place", f"0-{sd.tool_arg()}@{start.tool_arg()}"]
            width = frame[0] if frame and frame[0] else self.profile(op.video_inputs[0])["width"]
            picture = call("overlay", ["--video", stinger_path] + places + ["--position", "0,0", "--margin", "0", "--scale", str(width)] + enc, [joined])
            if plan["audio"]:
                inserts: List[str] = []
                for j in plan["joins"]:
                    inserts += ["--insert", stinger_path, "--at", Time.parse(j["stinger_start"]["seconds"]).tool_arg(), "--duration", sd.tool_arg(),
                                "--audio", "mix"]
                mixed = call("broll", inserts, [joined])
                final = call("audio", ["--replace", mixed], [picture])
            else:
                final = picture
            os.replace(final, partial)
        finally:
            for t in temps:
                if t != partial:
                    _remove(t)
        return commands, seconds

    # ------------------------------------------------------------ CONDENSE (ADR-026)
    def _input_duration(self, op) -> Optional[Time]:
        c = self.clips.get(op.inputs[0])
        return c.duration if c is not None and c.duration is not None else self.durations.get(op.inputs[0])

    def condense_plan(self, ref: str) -> Optional[Dict[str, Any]]:
        op = self.project.operations[ref]
        d = self._input_duration(op)
        if d is None:
            return None
        pieces = condense_pieces(op.params, d)
        rows = []
        for pc in pieces:
            length = pc["end"] - pc["start"]
            f = pc.get("factor")
            rows.append({"start": pc["start"].to_dict(), "end": pc["end"].to_dict(), "label": pc["label"], "action": pc["action"],
                         "factor": params_to_json({"f": f})["f"] if f is not None else None,
                         "output_seconds": "0.000000" if pc["action"] == "cut" else length.scale(1 / (f or Fraction(1))).to_dict()["seconds"]})
        removed = sum(((pc["end"] - pc["start"]).value for pc in pieces if pc["action"] == "cut"), Fraction(0))
        doc: Dict[str, Any] = {"output": op.params.get("output", "result"), "pieces": rows, "removed": Time(removed).to_dict(), "input_duration": d.to_dict()}
        if op.params.get("output") == "removed":   # the review list: each removed span and where it plays in the review file
            t = Time(Fraction(0))
            review = []
            for pc in condense_played(op.params, pieces):
                length = pc["end"] - pc["start"]
                review.append({"label": pc["label"], "source_range": {"start": pc["start"].to_dict(), "end": pc["end"].to_dict()},
                               "review_range": {"start": t.to_dict(), "end": (t + length).to_dict()}})
                t = t + length
            doc["review"] = review
        return doc

    def _engine_call(self, op, st: Step, t0: str, tool: str, args: Dict[str, Any], inputs: List[str], temps: List[str], acc: Dict[str, Any],
                     engine_args: Optional[Dict[str, Any]] = None) -> str:
        """One typed ffmpeg-skill call writing a temporary file of this operation (removed by the caller)."""
        out = os.path.join(self.work, f"{op.operation_id}.t{len(temps)}.partial.{os.getpid()}{output_extension(self.project, op.ref)}")
        _remove(out)
        temps.append(out)
        enc = {k: op.encoding[k] for k in ("crf", "preset") if op.encoding and k in op.encoding}
        argv = Step(op, "ffmpeg-skill/" + tool, dict(args, **enc), []).argv_for(inputs, out, resolve=lambda f: self.skill.resolve_flag(tool, f),
                                                                               engine_args=engine_args)
        self.log(f"run {op.ref}: ffmpeg-skill/{tool}")
        run = run_tool(self.skill, tool, argv, timeout=self.project.options.timeout_seconds)
        acc["commands"].extend(run.commands)
        acc["seconds"] += run.seconds
        if not run.ok:
            err = tool_error(run, f"operation {op.ref!r} ({op.type}, ffmpeg-skill/{tool})")
            for t in temps:
                _remove(t)
            self.results.append(self._record(op, st, "failed", t0, None, None, None, acc["commands"], acc["seconds"], err))
            raise err
        return out

    def _run_condense(self, ref: str, partial: str, t0: str) -> tuple:
        """Each piece that is kept is cut out frame-accurately; a sped one is then retimed by fit.py --duration (method speed:
        picture and pitch-preserved sound, as SPEED); the pieces are joined in order with no transition."""
        op = self.project.operations[ref]
        st = self.steps[ref]
        d = self._input_duration(op)
        src = self._path_of(op.inputs[0], False)
        if d is None:
            d = self._probe_duration(src)
        prof = self.profile(op.inputs[0])
        cut_args = {"no_keep_hevc": True} if self.skill.cut_keeps_hevc_by_default() else {}
        temps: List[str] = []
        acc: Dict[str, Any] = {"commands": [], "seconds": 0.0}
        try:
            parts: List[str] = []
            for pc in condense_played(op.params, condense_pieces(op.params, d)):
                part = self._engine_call(op, st, t0, "cut", {"start": pc["start"].tool_arg(), "end": pc["end"].tool_arg(), "accurate": True}, [src], temps, acc,
                                         cut_args)
                if pc["action"] == "speed":
                    target = (pc["end"] - pc["start"]).scale(1 / pc["factor"])
                    part = self._engine_call(op, st, t0, "fit", {"duration": target.tool_arg(), "method": "speed", "max_speed": "4"}, [part], temps, acc)
                parts.append(part)
            if len(parts) == 1:
                os.replace(parts[0], partial)
            else:
                fps = prof.get("fps")
                args: Dict[str, Any] = {"transition": "none", "width": prof["width"], "height": prof["height"]}
                if fps:
                    args["fps"] = _fps_text(Fraction(fps).limit_denominator(1001))
                os.replace(self._engine_call(op, st, t0, "join", args, parts, temps, acc), partial)
        finally:
            for t in temps:
                if t != partial:
                    _remove(t)
        return acc["commands"], acc["seconds"]

    # ------------------------------------------------------------ PUNCH_IN (ADR-020)
    def punch_plan(self, ref: str) -> Optional[Dict[str, Any]]:
        op = self.project.operations[ref]
        prof = self.profile(op.inputs[0])
        if not (prof["width"] and prof["height"]):
            return None
        win = punch_window(prof["width"], prof["height"], op.params["scale"], op.params["subject"])
        return {"window": win, "frame": [prof["width"], prof["height"]], "subject": dict(op.params["subject"]),
                "scale": params_to_json({"s": op.params["scale"]})["s"], "transition": "cut",
                "range": {"start": op.params["start"].to_dict(), "end": op.params["end"].to_dict()}}

    def _run_punch_in(self, ref: str, partial: str, t0: str) -> tuple:
        """A zoom on a range: the range is cut out frame-accurately, cropped to the window and scaled back to the input frame
        (fit.py --fit crop: the window's aspect is the frame's to within one even step, so nothing is stretched), and joined
        with the untouched parts before and after it (join.py, no transition). Each call is an ffmpeg-skill tool."""
        op = self.project.operations[ref]
        st = self.steps[ref]
        prof = self.profile(op.inputs[0])
        plan = self.punch_plan(ref)
        if plan is None or not prof.get("fps"):
            raise EditError("INVALID_INPUT", f"operation {ref!r} (PUNCH_IN): the input frame or frame rate is not known", {"operation": ref})
        win, (fw, fh) = plan["window"], plan["frame"]
        ext = output_extension(self.project, ref)
        src = self._path_of(op.inputs[0], False)
        c = self.clips.get(op.inputs[0])
        total = c.duration if c is not None and c.duration is not None else self.durations.get(op.inputs[0])
        if total is None:
            total = self._probe_duration(src)
        start, end = op.params["start"], op.params["end"]
        enc = {k: op.encoding[k] for k in ("crf", "preset") if op.encoding and k in op.encoding}
        temps: List[str] = []
        commands: List[str] = []
        seconds = 0.0

        def call(tool: str, args: Dict[str, Any], inputs: List[str], engine_args: Optional[Dict[str, Any]] = None) -> str:
            nonlocal seconds
            out = os.path.join(self.work, f"{op.operation_id}.pin{len(temps)}.partial.{os.getpid()}{ext}")
            _remove(out)
            temps.append(out)
            argv = Step(op, "ffmpeg-skill/" + tool, dict(args, **enc), []).argv_for(inputs, out, resolve=lambda f: self.skill.resolve_flag(tool, f),
                                                                                       engine_args=engine_args)
            self.log(f"run {ref}: ffmpeg-skill/{tool}")
            run = run_tool(self.skill, tool, argv, timeout=self.project.options.timeout_seconds)
            commands.extend(run.commands)
            seconds += run.seconds
            if not run.ok:
                err = tool_error(run, f"operation {ref!r} (PUNCH_IN, ffmpeg-skill/{tool})")
                for t in temps:
                    _remove(t)
                self.results.append(self._record(op, st, "failed", t0, None, None, None, commands, seconds, err))
                raise err
            return out

        cut_args = {"no_keep_hevc": True} if self.skill.cut_keeps_hevc_by_default() else {}
        try:
            parts: List[str] = []
            if start.value > 0:
                parts.append(call("cut", {"start": "0", "end": start.tool_arg(), "accurate": True}, [src], cut_args))
            mid = call("cut", {"start": start.tool_arg(), "end": end.tool_arg(), "accurate": True}, [src], cut_args)
            cropped = call("crop", {"x": win["x"], "y": win["y"], "width": win["width"], "height": win["height"]}, [mid])
            parts.append(call("fit", {"aspect": f"{fw}:{fh}", "fit": "crop", "width": fw}, [cropped]))
            if end.value < total.value - Time.from_float(0.5 / prof["fps"]).value:
                parts.append(call("cut", {"start": end.tool_arg(), "end": total.tool_arg(), "accurate": True}, [src], cut_args))
            if len(parts) == 1:
                os.replace(parts[0], partial)
            else:
                from fractions import Fraction
                joined = call("join", {"transition": "none", "width": fw, "height": fh, "fps": _fps_text(Fraction(prof["fps"]).limit_denominator(1001))}, parts)
                os.replace(joined, partial)
        finally:
            for t in temps:
                if t != partial:
                    _remove(t)
        return commands, seconds

    @staticmethod
    def _segments_report(parts: List[Dict[str, Any]], join: Optional[str]) -> Dict[str, Any]:
        return {"mode": "segments", "join": join, "parts": parts,
                "encoded": sum(1 for p in parts if p.get("status") == "encoded"), "reused": sum(1 for p in parts if p.get("status") == "reused")}

    def _validate_segment(self, ref: str, index: int, path: str, expected: float, want_audio: Optional[bool]) -> Dict[str, Any]:
        what = f"operation {ref!r} segment {index}"
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise EditError("OUTPUT_ERROR", f"{what}: the tool reported success but wrote nothing")
        doc = probe(self.skill, path)
        if not doc.get("video") or not doc.get("duration"):
            raise EditError("VALIDATION_ERROR", f"{what}: no video stream or duration")
        tol = DURATION_TOLERANCE["frame"]
        if abs(float(doc["duration"]) - expected) > tol:
            raise EditError("VALIDATION_ERROR", f"{what}: duration {float(doc['duration']):.3f}s differs from the expected {expected:.3f}s by more than {tol}s",
                            {"observed": float(doc["duration"]), "expected": expected, "tolerance": tol})
        if want_audio is True and not doc.get("audio"):
            raise EditError("VALIDATION_ERROR", f"{what}: the audio stream was lost", {"reason": "audio_lost"})
        return doc

    def _sweep_partials(self, operation_id: str) -> None:
        """Partial files of this operation left by a crashed earlier process (any pid). One run per workspace at a time is
        the rule; a partial of the same operation is never a live one of another run."""
        try:
            names = os.listdir(self.work)
        except OSError:
            return
        for name in names:
            if name.startswith(operation_id + ".partial.") or name == operation_id + ".json.partial":
                _remove(os.path.join(self.work, name))

    def _validate(self, ref: str, path: str) -> Dict[str, Any]:
        """Output validation: exists, non-empty, readable, probe says video + duration, duration as expected."""
        what = f"operation {ref!r} output"
        if not os.path.isfile(path):
            raise EditError("OUTPUT_ERROR", f"{what}: the tool reported success but wrote nothing")
        if os.path.getsize(path) == 0:
            raise EditError("OUTPUT_ERROR", f"{what}: empty file")
        if not os.access(path, os.R_OK):
            raise EditError("OUTPUT_ERROR", f"{what}: not readable")
        doc = probe(self.skill, path)
        if not doc.get("video"):
            raise EditError("VALIDATION_ERROR", f"{what}: no video stream")
        dur = doc.get("duration")
        if not dur or float(dur) <= 0:
            raise EditError("VALIDATION_ERROR", f"{what}: no valid duration")
        v = doc["video"]
        if not v.get("width") or not v.get("height"):
            raise EditError("VALIDATION_ERROR", f"{what}: no frame size")
        clip = self.clips.get(ref)
        op = self.project.operations[ref]
        if clip is not None and clip.duration is not None:
            tol = DURATION_TOLERANCE.get(op.params.get("precision", "default"), DURATION_TOLERANCE["default"])
            if op.type == "SPEED":
                tol = max(tol, 0.35)
            if abs(float(dur) - clip.duration.seconds) > tol:
                raise EditError("VALIDATION_ERROR", f"{what}: duration {float(dur):.3f}s differs from the expected {clip.duration.seconds:.3f}s by more than {tol}s",
                                {"observed": float(dur), "expected": clip.duration.seconds, "tolerance": tol})
        self._validate_media(op, doc, what)
        return doc

    def _validate_media(self, op, doc: Dict[str, Any], what: str) -> None:
        """Frame size, aspect, frame rate and audio presence against what the request and the inputs imply (operations.MEDIA)."""
        p = op.params
        v = doc["video"]
        got = (int(v["width"]), int(v["height"]))
        first = self.profile(op.inputs[0])
        target = self.target_frame(op.ref)
        if target and target[0] and target[1]:
            if got != tuple(target):
                raise EditError("VALIDATION_ERROR", f"{what}: frame {got[0]}x{got[1]} is not the normalized target {target[0]}x{target[1]}",
                                {"observed": list(got), "expected": list(target), "reason": "frame_size"})
        elif op.type in ("FIT", "FILL"):
            aw, ah = (int(x) for x in p["aspect"].split(":"))
            if abs(got[1] - got[0] * ah / aw) > ASPECT_TOLERANCE_PX:
                raise EditError("VALIDATION_ERROR", f"{what}: frame {got[0]}x{got[1]} is not at the requested aspect {p['aspect']}",
                                {"observed": list(got), "expected": p["aspect"], "reason": "aspect"})
        elif op.type == "RESIZE" and got[0] != even(p["width"]):
            raise EditError("VALIDATION_ERROR", f"{what}: width {got[0]} is not the requested {p['width']}", {"observed": list(got), "reason": "frame_size"})
        codec = str(v.get("codec") or "")
        if first.get("hdr") is not None and self._reencodes(op):
            want = "hevc" if first["hdr"] else "h264"
            if codec and codec != want:
                raise EditError("VALIDATION_ERROR", f"{what}: video codec {codec} is not the engine's {want} for this source",
                                {"observed": codec, "expected": want, "reason": "codec"})
        fps = _fps_of(doc)
        if "fps" in p and fps is not None and abs(fps - float(p["fps"])) > FPS_TOLERANCE:
            raise EditError("VALIDATION_ERROR", f"{what}: frame rate {fps:g} is not the requested {float(p['fps']):g}",
                            {"observed": fps, "expected": float(p["fps"]), "reason": "fps"})
        expected_audio = self.profile_expected_audio(op)
        if expected_audio is True and not doc.get("audio"):
            raise EditError("VALIDATION_ERROR", f"{what}: the audio stream was lost", {"reason": "audio_lost"})

    def profile_expected_audio(self, op) -> Optional[bool]:
        if op.type == "IMAGE_INSERT":
            return False
        if op.type == "CONCAT":
            audios = [self.profile(r).get("audio") for r in op.video_inputs + ([op.stinger] if op.stinger else [])]
            return True if any(a is True for a in audios) else (False if all(a is False for a in audios) else None)
        return self.profile(op.inputs[0]).get("audio")

    def _deliver(self) -> None:
        for ref in sorted(self.project.outputs):
            out = self.project.outputs[ref]
            src = self.produced[out.operation]
            os.makedirs(os.path.dirname(out.path), exist_ok=True)
            partial = out.path + ".partial"
            try:
                shutil.copyfile(src, partial)
                os.replace(partial, out.path)
            except OSError as exc:
                _remove(partial)
                raise EditError("OUTPUT_ERROR", f"output {ref!r}: cannot write {os.path.basename(out.path)}: {exc.strerror}") from exc

    def _outputs_doc(self, delivered: bool) -> List[Dict[str, Any]]:
        docs = []
        for ref in sorted(self.project.outputs):
            out = self.project.outputs[ref]
            d: Dict[str, Any] = {"id": ref, "operation": out.operation, "path": out.path, "delivered": delivered and os.path.isfile(out.path)}
            if d["delivered"]:
                rec = next((r for r in self.results if r["operation"] == out.operation), None)
                d["sha256"] = sha256_file(out.path)
                d["size"] = os.path.getsize(out.path)
                d["container"] = os.path.splitext(out.path)[1].lower()
                d["reused"] = bool(rec and rec["status"] == "reused")
                d["operation_id"] = self.project.operations[out.operation].operation_id
                d["timeline"] = self.clips[out.operation].to_dict() if out.operation in self.clips else None
                mark = self.ai_mark(out.operation)
                if mark:
                    d["marks"], d["ai_generated"] = ["AI_GENERATED"], mark
                d["observation"] = {"kind": "media.probe", "provenance": "OBSERVED", "source": f"ffmpeg-skill/probe@{self.skill.version}",
                                    "data": rec["probe"] if rec else None}
            docs.append(d)
        return docs

    def _record(self, op, st: Step, status: str, started: str, path: Optional[str], digest: Optional[str], doc: Optional[Dict[str, Any]],
                commands: List[str], seconds: float, err: Optional[EditError] = None, note: Optional[str] = None,
                segments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        inputs = []
        for r in op.inputs:
            if r in self.project.sources:
                inputs.append({"ref": r, "kind": "source", "sha256": self.project.sources[r].sha256})
            else:
                rec = next((x for x in self.results if x["operation"] == r), None)
                inputs.append({"ref": r, "kind": "operation", "operation_id": self.project.operations[r].operation_id,
                               "sha256": (rec.get("output") or {}).get("sha256") if rec else None})
        rec = {"operation": op.ref, "operation_id": op.operation_id, "type": op.type, "capability": op.capability, "status": status,
               "skill": SKILL_ID, "skill_version": VERSION, "tool": st.tool, "tool_versions": self.tool_versions,
               "idempotency_key": self.keys.get(op.ref), "parameters": st.to_dict()["arguments"],
               "params": params_to_json(op.params), "encoding": dict(op.encoding) if op.encoding else None,
               "normalized": self.normalized(op.ref) if self.source_probes else None,
               "depends_on": list(op.depends_on), "inputs": inputs,
               "output": {"path": path, "sha256": digest} if path else None,
               "probe": doc, "commands": commands, "started_at": started, "finished_at": now_iso(), "seconds": seconds,
               "provenance": "OBSERVED" if doc else None}
        if err is not None:
            rec["error"] = err.to_dict()
        if note is not None:
            rec["note"] = note
        if segments is not None:
            rec["segments"] = segments
        mark = self.ai_mark(op.ref)
        if mark:
            rec["marks"], rec["ai_generated"] = ["AI_GENERATED"], mark
        return rec

    def ai_mark(self, ref: str) -> List[Dict[str, str]]:
        """Every caller-declared AI-generated source an operation's result contains (ADR-022), in source order."""
        op = self.project.operations[ref]
        return [{"source": s, **self.project.sources[s].ai_generated, "provenance": "DECLARED"}   # type: ignore[dict-item]
                for s in op.source_refs if self.project.sources[s].ai_generated is not None]


class _Failed(Exception):
    def __init__(self, error: EditError, doc: Dict[str, Any]):
        super().__init__(error.message)
        self.error = error
        self.doc = doc
