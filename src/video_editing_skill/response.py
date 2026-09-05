"""Response self-check: the shape every document on stdout must have, verified *before* it is printed.

A document that does not pass is never printed as a success: the CLI reports INTERNAL_ERROR instead. The same
function serves the tests (every fake-engine and real-media response is checked) and any caller that wants to
verify a document it received. Rules follow the contract's response_shape / plan / validate documents:

  success   {"ok": true, "schema", "skill": {"id", "version"}, "status", "command", "project", "warnings", ...}
  run       + "engine", "execution": {"status", "started_at", "finished_at", "work_dir", "request_sha256", "engine",
              "reused", "sources[]", "operations[]" (provenance records), "outputs[]" (delivered files)}
  plan      + "engine", "dry_run": true, "plan": {"work_dir", "steps[]"}
  failure   {"ok": false, "error": {"code", "message", "retryable", "details"}} (+ status / execution / project on run)
"""
import re
from typing import Any, List, Optional

from . import PLAN_SCHEMA, RESPONSE_SCHEMA, SKILL_ID, VERSION
from .errors import ERROR_CODES

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
STATUS_BY_COMMAND = {"validate": ("valid",), "plan": ("planned",), "run": ("completed", "reused")}
EXECUTION_STATUSES = ("completed", "reused", "failed", "cancelled")
RECORD_STATUSES = ("completed", "reused", "failed", "skipped")
RECORD_KEYS = ("operation", "operation_id", "type", "capability", "status", "skill", "skill_version", "tool", "tool_versions", "idempotency_key",
               "parameters", "inputs", "output", "probe", "commands", "started_at", "finished_at", "seconds", "provenance")
OUTPUT_KEYS = ("id", "operation", "path", "delivered")
DELIVERED_KEYS = ("sha256", "size", "container", "reused", "operation_id", "timeline", "observation")
OBSERVATION_SOURCE = "ffmpeg-skill/probe@"


def _is_time(v: Any) -> bool:
    return isinstance(v, dict) and set(v) == {"seconds", "rational"} and isinstance(v["seconds"], str) and re.match(r"^-?\d+/\d+$", str(v["rational"])) is not None


def check_timeline(tl: Any, where: str) -> List[str]:
    p: List[str] = []
    if not isinstance(tl, dict):
        return [f"{where}: timeline is not an object"]
    if not isinstance(tl.get("duration_known"), bool):
        p.append(f"{where}: timeline.duration_known must be a boolean")
    if tl.get("duration_known"):
        if not _is_time(tl.get("duration")):
            p.append(f"{where}: timeline.duration is not a time")
    elif tl.get("duration") is not None:
        p.append(f"{where}: timeline.duration must be null when unknown")
    tracks = tl.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        return p + [f"{where}: timeline.tracks must be a non-empty list"]
    for i, t in enumerate(tracks):
        if not isinstance(t, dict) or t.get("kind") not in ("video", "overlay") or not isinstance(t.get("id"), str) or not isinstance(t.get("segments"), list):
            p.append(f"{where}: timeline.tracks[{i}] malformed")
            continue
        for j, seg in enumerate(t["segments"]):
            if not isinstance(seg, dict) or not isinstance(seg.get("source"), str):
                p.append(f"{where}: timeline.tracks[{i}].segments[{j}] malformed")
                continue
            if t["kind"] == "video":
                sr = seg.get("source_range")
                if not (isinstance(sr, dict) and _is_time(sr.get("start")) and _is_time(sr.get("end"))):
                    p.append(f"{where}: timeline.tracks[{i}].segments[{j}].source_range malformed")
                tr = seg.get("timeline_range")
                if tr is not None and not (isinstance(tr, dict) and _is_time(tr.get("start")) and _is_time(tr.get("end"))):
                    p.append(f"{where}: timeline.tracks[{i}].segments[{j}].timeline_range malformed")
                if not re.match(r"^\d+/\d+$", str(seg.get("speed"))):
                    p.append(f"{where}: timeline.tracks[{i}].segments[{j}].speed malformed")
    return p


def check_observation(obs: Any, where: str) -> List[str]:
    if not isinstance(obs, dict):
        return [f"{where}: observation is not an object"]
    p: List[str] = []
    if obs.get("kind") != "media.probe":
        p.append(f"{where}: observation.kind must be media.probe")
    if obs.get("provenance") != "OBSERVED":
        p.append(f"{where}: observation.provenance must be OBSERVED")
    if not str(obs.get("source", "")).startswith(OBSERVATION_SOURCE):
        p.append(f"{where}: observation.source must be {OBSERVATION_SOURCE}<version>")
    data = obs.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("video"), dict):
        p.append(f"{where}: observation.data must be a probe document with a video stream")
    return p


def check_record(rec: Any, where: str) -> List[str]:
    if not isinstance(rec, dict):
        return [f"{where}: record is not an object"]
    p = [f"{where}: missing {k}" for k in RECORD_KEYS if k not in rec]
    if rec.get("status") not in RECORD_STATUSES:
        p.append(f"{where}: status {rec.get('status')!r} not in {RECORD_STATUSES}")
    if rec.get("skill") != SKILL_ID or rec.get("skill_version") != VERSION:
        p.append(f"{where}: skill / skill_version do not name this skill")
    if not str(rec.get("tool", "")).startswith("ffmpeg-skill/"):
        p.append(f"{where}: tool is not an ffmpeg-skill tool")
    if not isinstance(rec.get("commands"), list) or not all(isinstance(c, str) for c in rec.get("commands", [])):
        p.append(f"{where}: commands must be a list of strings")
    if not re.match(r"^op_[0-9a-f]{16}$", str(rec.get("operation_id"))):
        p.append(f"{where}: operation_id malformed")
    st = rec.get("status")
    out = rec.get("output")
    if st in ("completed", "reused"):
        if not (isinstance(out, dict) and _SHA256.match(str(out.get("sha256"))) and isinstance(out.get("path"), str)):
            p.append(f"{where}: a {st} record needs output.path and output.sha256")
        if rec.get("provenance") != "OBSERVED" or not isinstance(rec.get("probe"), dict):
            p.append(f"{where}: a {st} record needs an OBSERVED probe")
    else:
        if out is not None or rec.get("probe") is not None:
            p.append(f"{where}: a {st} record must not carry an output or a probe")
        if st == "failed" and not isinstance(rec.get("error"), dict):
            p.append(f"{where}: a failed record needs its error")
    for k in ("started_at", "finished_at"):
        if not _ISO.match(str(rec.get(k))):
            p.append(f"{where}: {k} is not an ISO-8601 UTC timestamp")
    return p


def check_output(out: Any, where: str, expect_delivered: bool) -> List[str]:
    if not isinstance(out, dict):
        return [f"{where}: output is not an object"]
    p = [f"{where}: missing {k}" for k in OUTPUT_KEYS if k not in out]
    if expect_delivered and out.get("delivered") is not True:
        p.append(f"{where}: not delivered in a successful run")
    if out.get("delivered"):
        p += [f"{where}: missing {k}" for k in DELIVERED_KEYS if k not in out]
        if not _SHA256.match(str(out.get("sha256"))):
            p.append(f"{where}: sha256 malformed")
        if not isinstance(out.get("size"), int) or out.get("size", 0) <= 0:
            p.append(f"{where}: size must be a positive integer")
        if not isinstance(out.get("reused"), bool):
            p.append(f"{where}: reused must be a boolean")
        p += check_timeline(out.get("timeline"), where)
        p += check_observation(out.get("observation"), where)
    return p


def check_error(err: Any, where: str = "error") -> List[str]:
    if not isinstance(err, dict):
        return [f"{where}: not an object"]
    p: List[str] = []
    if err.get("code") not in ERROR_CODES:
        p.append(f"{where}: unknown code {err.get('code')!r}")
    if not isinstance(err.get("message"), str) or not err.get("message"):
        p.append(f"{where}: message must be a non-empty string")
    if not isinstance(err.get("retryable"), bool):
        p.append(f"{where}: retryable must be a boolean")
    if not isinstance(err.get("details"), dict):
        p.append(f"{where}: details must be an object")
    return p


def check_response(doc: Any, command: Optional[str] = None) -> List[str]:
    """Problems with a document this skill is about to print (or a caller received). Empty means it conforms."""
    if not isinstance(doc, dict):
        return ["document is not an object"]
    if "ok" not in doc or not isinstance(doc["ok"], bool):
        return ["ok must be a boolean"]
    if doc["ok"] is False:
        failure = check_error(doc.get("error"))
        if "execution" in doc:
            failure += _check_execution(doc["execution"], success=False)
        return failure
    p: List[str] = []
    cmd = command or doc.get("command")
    if doc.get("schema") != (PLAN_SCHEMA if cmd == "plan" else RESPONSE_SCHEMA):
        p.append(f"schema {doc.get('schema')!r} is not the {cmd} schema")
    if doc.get("skill") != {"id": SKILL_ID, "version": VERSION}:
        p.append("skill block does not name this skill and version")
    if cmd not in STATUS_BY_COMMAND:
        return p + [f"unknown command {cmd!r}"]
    if doc.get("command") != cmd:
        p.append(f"command {doc.get('command')!r} != {cmd!r}")
    if doc.get("status") not in STATUS_BY_COMMAND[cmd]:
        p.append(f"status {doc.get('status')!r} is not one of {STATUS_BY_COMMAND[cmd]} for {cmd}")
    if not isinstance(doc.get("project"), dict) or not isinstance(doc.get("warnings"), list):
        p.append("project must be an object and warnings a list")
    if cmd in ("plan", "run"):
        eng = doc.get("engine")
        if not isinstance(eng, dict) or not eng.get("ffmpeg-skill"):
            p.append("engine block must name the ffmpeg-skill version")
    if cmd == "plan":
        if doc.get("dry_run") is not True:
            p.append("plan must say dry_run: true")
        plan = doc.get("plan")
        if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list) or not plan.get("steps"):
            p.append("plan.steps must be a non-empty list")
        else:
            for i, st in enumerate(plan["steps"]):
                for k in ("operation", "operation_id", "type", "tool", "inputs", "arguments", "idempotency_key", "timeline", "intermediate", "reusable"):
                    if k not in st:
                        p.append(f"plan.steps[{i}]: missing {k}")
                if isinstance(st, dict) and isinstance(st.get("timeline"), dict):
                    p += check_timeline(st["timeline"], f"plan.steps[{i}]")
    if cmd == "run":
        p += _check_execution(doc.get("execution"), success=True)
        ex = doc.get("execution") or {}
        if isinstance(ex, dict) and ex.get("status") != doc.get("status"):
            p.append("status and execution.status disagree")
    return p


def _check_execution(ex: Any, success: bool) -> List[str]:
    if not isinstance(ex, dict):
        return ["execution is not an object"]
    p: List[str] = []
    for k in ("status", "started_at", "finished_at", "work_dir", "engine", "reused", "sources", "operations", "outputs"):
        if k not in ex:
            p.append(f"execution: missing {k}")
    if ex.get("status") not in EXECUTION_STATUSES:
        p.append(f"execution.status {ex.get('status')!r} not in {EXECUTION_STATUSES}")
    if success and ex.get("status") not in ("completed", "reused"):
        p.append("a successful run must be completed or reused")
    if not success and ex.get("status") in ("completed", "reused"):
        p.append("a failed run must not be completed or reused")
    if not isinstance(ex.get("reused"), bool) or (success and ex.get("reused") != (ex.get("status") == "reused")):
        p.append("execution.reused must be a boolean equal to (status == reused)")
    recs = ex.get("operations")
    if not isinstance(recs, list) or (success and not recs):
        p.append("execution.operations must be a list (non-empty on success)")
        recs = []
    for i, rec in enumerate(recs):
        p += check_record(rec, f"execution.operations[{i}]")
    if success:
        if any(r.get("status") not in ("completed", "reused") for r in recs if isinstance(r, dict)):
            p.append("a successful run has only completed / reused records")
        if ex.get("status") == "reused" and any(r.get("status") != "reused" for r in recs if isinstance(r, dict)):
            p.append("status reused requires every record to be reused")
    else:
        if not any(r.get("status") == "failed" for r in recs if isinstance(r, dict)) and ex.get("status") == "failed" and recs:
            pass  # a failure before the first step (probe, media check) has no failed record: allowed
    outs = ex.get("outputs")
    if not isinstance(outs, list) or (success and not outs):
        p.append("execution.outputs must be a list (non-empty on success)")
        outs = []
    for i, out in enumerate(outs):
        p += check_output(out, f"execution.outputs[{i}]", expect_delivered=success)
    srcs = ex.get("sources")
    if not isinstance(srcs, list):
        p.append("execution.sources must be a list")
    else:
        for i, sd in enumerate(srcs):
            if not isinstance(sd, dict) or not isinstance(sd.get("id"), str) or sd.get("kind") not in ("video", "image"):
                p.append(f"execution.sources[{i}] malformed")
            elif sd.get("observation") is not None:
                p += check_observation(sd["observation"], f"execution.sources[{i}]")
    if ex.get("request_sha256") is not None and not _SHA256.match(str(ex.get("request_sha256"))):
        p.append("execution.request_sha256 malformed")
    return p


__all__ = ["check_response", "check_record", "check_output", "check_timeline", "check_observation", "check_error",
           "STATUS_BY_COMMAND", "RECORD_STATUSES", "EXECUTION_STATUSES", "RECORD_KEYS", "OUTPUT_KEYS", "DELIVERED_KEYS"]
