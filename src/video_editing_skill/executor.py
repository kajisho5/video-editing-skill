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
from .compiler import Step, compile_project, output_extension
from .errors import EditError
from .ffmpeg_skill import FfmpegSkill, cancelled, missing_capabilities, probe, run_tool, tool_error
from .operations import ENCODING_DEFAULTS, MEDIA, even, params_to_json
from .project import EditProject
from .timebase import Time
from .timeline import Clip, build_timelines

DURATION_TOLERANCE = {"frame": 0.35, "keyframe": 1.5, "default": 0.35}
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
            ins = [self.profile(r) for r in op.inputs]
            audios = [i["audio"] for i in ins]
            prof["audio"] = True if any(a is True for a in audios) else (False if all(a is False for a in audios) else None)
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
        input frame with ffmpeg-skill's own rule; None when it is the input's frame (TRIM / CUT / SPEED / OVERLAY) and that is
        not known yet."""
        op = self.project.operations[ref]
        p = op.params
        first = self.profile(op.inputs[0])
        sw, sh = first["width"], first["height"]
        if op.type == "CONCAT":
            if "width" in p and "height" in p:
                return (p["width"], p["height"])
            if sw and sh:
                return (sw - (sw % 2), sh - (sh % 2))
            return None
        if op.type == "RESIZE":
            if sw and sh:
                return (even(p["width"]), even(p["width"] * sh / sw))
            return (even(p["width"]), None)
        if op.type in ("FIT", "FILL"):
            aw, ah = (int(x) for x in p["aspect"].split(":"))
            ratio = aw / ah
            if "width" in p:
                w = even(p["width"])
            elif sw and sh:
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
        return not (op.type in ("TRIM", "CUT") and op.params.get("precision") == "keyframe")

    @staticmethod
    def _profile_of_doc(doc: Optional[Dict[str, Any]], provenance: str) -> Dict[str, Any]:
        """A probe document as a profile. A source whose rotation metadata is ±90 / 270 is measured with width and height
        swapped, exactly as ffmpeg-skill's fit.py / join.py do before they compute a target frame."""
        size = _size_of(doc)
        v = (doc or {}).get("video") or {}
        if size and v.get("rotation") in (90, -90, 270, -270):
            size = (size[1], size[0])
        return {"audio": _has_audio(doc), "width": size[0] if size else None, "height": size[1] if size else None, "fps": _fps_of(doc),
                "hdr": bool(v.get("hdr")) if doc else None, "vfr": bool(v.get("variable_frame_rate_suspected")) if doc else None,
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
            video_inputs = op.inputs[:-1] if op.type == "OVERLAY" else op.inputs
            if op.type == "CONCAT":
                hdrs = {r: self.profile(r).get("hdr") for r in video_inputs}
                if any(h is True for h in hdrs.values()) and any(h is False for h in hdrs.values()):
                    raise EditError("INVALID_INPUT", f"operation {ref!r} (CONCAT): HDR and SDR inputs cannot be joined (the engine encodes from the first input's colour system)",
                                    {"operation": ref, "inputs": hdrs, "reason": "hdr_mismatch"})
            for r in video_inputs:
                prof = self.profile(r)
                if req["audio"] and prof["audio"] is False:
                    raise EditError("INVALID_INPUT", f"operation {ref!r} ({op.type}): input {r!r} has no audio stream and {op.type} needs one "
                                    "(ffmpeg-skill 0.9.x overlay never terminates on a video without audio); add audio upstream or use a source with audio",
                                    {"operation": ref, "input": r, "reason": "audio_required"})
            if op.type == "OVERLAY":
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
            if op.type == "TRIM":
                ranges = [(op.params["start"], op.params["end"])]
            elif op.type == "CUT":
                ranges = [(r["start"], r["end"]) for r in op.params["keep"]]
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
            if op.type == "CONCAT" and op.params.get("transition"):
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
            self.keys[ref] = stable_hash(key)

    # ------------------------------------------------------------ plan
    def plan(self, preview: bool = True) -> Dict[str, Any]:
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
            if preview:
                d["preview"] = self._preview(ref)
            steps.append(d)
        return {"work_dir": self.work, "steps": steps}

    def _preview(self, ref: str) -> Dict[str, Any]:
        """ffmpeg-skill --dry-run for one step: the commands it would run. Nothing is written."""
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
        return st.argv_for(inputs, out, image, dur)

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
        argv = self._argv(ref)
        self.log(f"run {ref}: {st.tool}")
        run = run_tool(self.skill, st.script, argv, timeout=self.project.options.timeout_seconds)
        if not run.ok:
            _remove(partial)
            err = tool_error(run, f"operation {ref!r} ({st.tool})")
            self.results.append(self._record(op, st, "failed", t0, None, None, None, run.commands, run.seconds, err))
            raise err
        try:
            doc = self._validate(ref, partial)
        except EditError as exc:
            _remove(partial)
            self.results.append(self._record(op, st, "failed", t0, None, None, None, run.commands, run.seconds, exc))
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
        self.results.append(self._record(op, st, "completed", t0, final, digest, doc, run.commands, run.seconds))

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
        if op.type == "CONCAT":
            audios = [self.profile(r).get("audio") for r in op.inputs]
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
                d["observation"] = {"kind": "media.probe", "provenance": "OBSERVED", "source": f"ffmpeg-skill/probe@{self.skill.version}",
                                    "data": rec["probe"] if rec else None}
            docs.append(d)
        return docs

    def _record(self, op, st: Step, status: str, started: str, path: Optional[str], digest: Optional[str], doc: Optional[Dict[str, Any]],
                commands: List[str], seconds: float, err: Optional[EditError] = None, note: Optional[str] = None) -> Dict[str, Any]:
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
        return rec


class _Failed(Exception):
    def __init__(self, error: EditError, doc: Dict[str, Any]):
        super().__init__(error.message)
        self.error = error
        self.doc = doc
