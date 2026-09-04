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
from .ffmpeg_skill import FfmpegSkill, cancelled, probe, run_tool, tool_error
from .project import EditProject
from .timebase import Time
from .timeline import Clip, build_timelines

DURATION_TOLERANCE = {"frame": 0.35, "keyframe": 1.5, "default": 0.35}


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
    def __init__(self, project: EditProject, skill: FfmpegSkill, tool_versions: Dict[str, Optional[str]], log=None):
        self.project = project
        self.skill = skill
        self.tool_versions = {"ffmpeg-skill": skill.version, **tool_versions}
        self.log = log or (lambda msg: None)
        self.steps: Dict[str, Step] = compile_project(project)
        self.work = project.policy.work_dir(create=False)
        self.durations: Dict[str, Optional[Time]] = {}
        self.source_probes: Dict[str, Dict[str, Any]] = {}
        self.clips: Dict[str, Clip] = {}
        self.results: List[Dict[str, Any]] = []
        self.produced: Dict[str, str] = {}          # op ref -> validated intermediate path
        self.keys: Dict[str, str] = {}              # op ref -> idempotency key

    # ------------------------------------------------------------ preparation
    def probe_sources(self) -> None:
        for ref in sorted(self.project.sources):
            src = self.project.sources[ref]
            if src.kind != "video":
                continue
            doc = probe(self.skill, src.path)
            if not doc.get("video"):
                raise EditError("INVALID_INPUT", f"source {ref!r} has no video stream", {"source": ref})
            d = doc.get("duration")
            if not d or float(d) <= 0:
                raise EditError("INVALID_INPUT", f"source {ref!r} has no duration (a still image or a broken container is not a video source)",
                                {"source": ref, "reason": "no_duration"})
            self.source_probes[ref] = doc
            self.durations[ref] = Time.from_float(float(d))
        self.clips = build_timelines(self.project, self.durations)
        self._check_ranges()
        self._compute_keys()

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
            self.keys[ref] = stable_hash({"operation_id": op.operation_id, "tool": op.tool, "tool_versions": self.tool_versions,
                                          "skill_version": VERSION, "container": output_extension(self.project, ref)})

    # ------------------------------------------------------------ plan
    def plan(self, preview: bool = True) -> Dict[str, Any]:
        steps: List[Dict[str, Any]] = []
        for ref in self.project.order:
            st = self.steps[ref]
            d: Dict[str, Any] = st.to_dict()
            d["idempotency_key"] = self.keys.get(ref)
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
        try:
            self.work = self.project.policy.work_dir(create=True)
            for ref in self.project.order:
                if cancelled():
                    raise EditError("CANCELLED", "interrupted before operation " + ref)
                self._run_step(ref)
            self._deliver()
        except EditError as exc:
            status = "cancelled" if exc.code == "CANCELLED" else "failed"
            error = exc
        doc: Dict[str, Any] = {"status": status, "started_at": started, "finished_at": now_iso(), "work_dir": self.work,
                               "operations": self.results, "outputs": self._outputs_doc(status == "completed")}
        if error is not None:
            doc["error"] = error.to_dict()
            raise _Failed(error, doc)
        return doc

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
            self.produced[ref] = final
            self.results.append(self._record(op, st, "reused", t0, final, rec["sha256"], rec.get("probe"), [], 0.0))
            self.log(f"reused {ref} ({op.operation_id})")
            return
        _remove(partial)
        _remove(final)
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
        exp = self._expected_size(op)
        if exp and (v["width"], v["height"]) != exp:
            raise EditError("VALIDATION_ERROR", f"{what}: frame {v['width']}x{v['height']} is not the requested {exp[0]}x{exp[1]}")
        return doc

    def _expected_size(self, op) -> Optional[tuple]:
        p = op.params
        if op.type in ("FIT", "FILL") and "width" in p:
            w = p["width"]
            aw, ah = (int(x) for x in p["aspect"].split(":"))
            h = int(round(w * ah / aw))
            return (w, h + (h % 2))
        if op.type == "CONCAT" and "width" in p and "height" in p:
            return (p["width"], p["height"])
        return None

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
                raise EditError("OUTPUT_ERROR", f"output {ref!r}: cannot write {os.path.basename(out.path)}: {exc.strerror}")

    def _outputs_doc(self, delivered: bool) -> List[Dict[str, Any]]:
        docs = []
        for ref in sorted(self.project.outputs):
            out = self.project.outputs[ref]
            d: Dict[str, Any] = {"id": ref, "operation": out.operation, "path": out.path, "delivered": delivered and os.path.isfile(out.path)}
            if d["delivered"]:
                rec = next((r for r in self.results if r["operation"] == out.operation), None)
                d["sha256"] = sha256_file(out.path)
                d["size"] = os.path.getsize(out.path)
                d["timeline"] = self.clips[out.operation].to_dict() if out.operation in self.clips else None
                d["observation"] = {"kind": "media.probe", "provenance": "OBSERVED", "source": f"ffmpeg-skill/probe@{self.skill.version}",
                                    "data": rec["probe"] if rec else None}
            docs.append(d)
        return docs

    def _record(self, op, st: Step, status: str, started: str, path: Optional[str], digest: Optional[str], doc: Optional[Dict[str, Any]],
                commands: List[str], seconds: float, err: Optional[EditError] = None) -> Dict[str, Any]:
        inputs = []
        for r in op.inputs:
            if r in self.project.sources:
                inputs.append({"ref": r, "kind": "source", "sha256": self.project.sources[r].sha256})
            else:
                rec = next((x for x in self.results if x["operation"] == r), None)
                inputs.append({"ref": r, "kind": "operation", "operation_id": self.project.operations[r].operation_id,
                               "sha256": rec.get("output", {}).get("sha256") if rec else None})
        rec = {"operation": op.ref, "operation_id": op.operation_id, "type": op.type, "capability": op.capability, "status": status,
               "skill": SKILL_ID, "skill_version": VERSION, "tool": st.tool, "tool_versions": self.tool_versions,
               "idempotency_key": self.keys.get(op.ref), "parameters": st.to_dict()["arguments"], "inputs": inputs,
               "output": {"path": path, "sha256": digest} if path else None,
               "probe": doc, "commands": commands, "started_at": started, "finished_at": now_iso(), "seconds": seconds,
               "provenance": "OBSERVED" if doc else None}
        if err is not None:
            rec["error"] = err.to_dict()
        return rec


class _Failed(Exception):
    def __init__(self, error: EditError, doc: Dict[str, Any]):
        super().__init__(error.message)
        self.error = error
        self.doc = doc
