"""Environment report: python, ffmpeg-skill (location, version, tools), ffmpeg / ffprobe, path policy.

Prints no environment variables and no secrets. Exit 1 when the skill cannot run here.
"""
import platform
from typing import Any, Dict, List, Optional

from . import CONTRACT_SCHEMA, CONTRACT_VERSION, DOCTOR_SCHEMA, PLAN_SCHEMA, REQUEST_SCHEMA, RESPONSE_SCHEMA, SKILL_ID, VERSION
from .contract import TOOL_REQUIREMENTS
from .errors import EditError
from .ffmpeg_skill import ENV_DIR, REQUIRED_TOOLS, SUPPORTED_MAX_EXCLUSIVE, SUPPORTED_MIN, engine_doctor, locate, missing_capabilities
from .operations import OPERATIONS, unsupported_list
from .paths import PathPolicy


def operation_availability(skill, eng: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per operation type: AVAILABLE only when its ffmpeg-skill tool exists and every encoder / filter it needs is reported
    present by ffmpeg-skill's doctor; otherwise MISSING with the gaps named. Nothing is reported available on a guess."""
    rows = []
    for t in sorted(OPERATIONS):
        spec = OPERATIONS[t]
        script = spec["tool"].split("/", 1)[1]
        missing: List[str] = []
        if skill is None:
            missing.append("ffmpeg-skill")
        else:
            if not skill.version_supported():
                missing.append(f"ffmpeg-skill version {skill.version} (supported >={'.'.join(map(str, SUPPORTED_MIN))},<{'.'.join(map(str, SUPPORTED_MAX_EXCLUSIVE))})")
            if script not in skill.tools:
                missing.append(f"tool:{spec['tool']}")
            missing += missing_capabilities(eng or {}, TOOL_REQUIREMENTS[spec["tool"]]) if eng is not None else ["ffmpeg-skill doctor did not run"]
        rows.append({"type": t, "tool_id": f"{SKILL_ID}/{t.lower()}", "capability": spec["capability"], "executed_by": spec["tool"],
                     "required_capabilities": list(TOOL_REQUIREMENTS[spec["tool"]]),
                     "status": "AVAILABLE" if not missing else "MISSING", "missing": missing})
    return rows


def doctor_report(ffmpeg_skill_dir: Optional[str] = None, workspace: Optional[str] = None, allowed_inputs: Optional[List[str]] = None) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    problems: List[str] = []
    checks.append({"check": "python", "status": "AVAILABLE", "version": platform.python_version(), "platform": platform.system()})
    checks.append({"check": "skill", "status": "AVAILABLE", "id": SKILL_ID, "version": VERSION})

    skill = locate(ffmpeg_skill_dir)
    if skill is None:
        checks.append({"check": "ffmpeg-skill", "status": "MISSING",
                       "detail": f"not found; set {ENV_DIR} or --ffmpeg-skill-dir to a checkout that has scripts/probe.py"})
        problems.append("ffmpeg-skill not found")
    else:
        missing = skill.missing_tools()
        ok = skill.version_supported() and not missing
        checks.append({"check": "ffmpeg-skill", "status": "AVAILABLE" if ok else "DEGRADED", "version": skill.version, "root": skill.root,
                       "version_supported": skill.version_supported(), "missing_tools": missing,
                       "detail": None if ok else ("unsupported version" if not skill.version_supported() else f"missing tools {missing}")})
        if not ok:
            problems.append("ffmpeg-skill version or tools unsupported")
        for t in REQUIRED_TOOLS:
            checks.append({"check": f"tool:ffmpeg-skill/{t}", "status": "AVAILABLE" if t in skill.tools else "MISSING"})

    eng: Optional[Dict[str, Any]] = None
    if skill is not None:
        eng = engine_doctor(skill)
        for name in ("ffmpeg", "ffprobe"):
            v = eng.get(name)
            checks.append({"check": name, "status": "AVAILABLE" if v else "MISSING", "version": v, "source": "ffmpeg-skill doctor"})
            if not v:
                problems.append(f"{name} not available to ffmpeg-skill")
        if eng.get("missing"):
            checks.append({"check": "ffmpeg-skill:capabilities", "status": "DEGRADED", "missing": eng["missing"], "detail": eng.get("detail")})
            problems.append("ffmpeg-skill reports missing capabilities")
    else:
        for name in ("ffmpeg", "ffprobe"):
            checks.append({"check": name, "status": "UNKNOWN", "detail": "reported by ffmpeg-skill doctor once ffmpeg-skill is found"})

    if workspace:
        try:
            policy = PathPolicy(workspace, allowed_inputs)
            checks.append({"check": "path_policy", "status": "AVAILABLE", **policy.describe()})
        except EditError as exc:
            checks.append({"check": "path_policy", "status": "MISSING", "detail": exc.message})
            problems.append("path policy: " + exc.message)
    else:
        checks.append({"check": "path_policy", "status": "UNKNOWN", "detail": "pass --workspace to check a workspace"})

    operations = operation_availability(skill, eng)
    for row in operations:
        if row["status"] != "AVAILABLE":
            problems.append(f"operation {row['type']} unavailable: " + ", ".join(row["missing"]))
    supported = [r["type"] for r in operations if r["status"] == "AVAILABLE"]
    return {"schema": DOCTOR_SCHEMA, "ok": not problems, "skill": {"id": SKILL_ID, "version": VERSION},
            "contract": {"schema": CONTRACT_SCHEMA, "version": VERSION, "contract_version": CONTRACT_VERSION,
                         "request": REQUEST_SCHEMA, "response": RESPONSE_SCHEMA, "plan": PLAN_SCHEMA, "doctor": DOCTOR_SCHEMA},
            "engine": {"id": "ffmpeg-skill", "found": skill is not None, "version": skill.version if skill else None, "root": skill.root if skill else None,
                       "version_supported": skill.version_supported() if skill else None, "tools_required": list(REQUIRED_TOOLS),
                       "tools_missing": skill.missing_tools() if skill else list(REQUIRED_TOOLS),
                       "ffmpeg": (eng or {}).get("ffmpeg"), "ffprobe": (eng or {}).get("ffprobe"), "ready": bool((eng or {}).get("ok")),
                       "capabilities_missing": list((eng or {}).get("missing") or []), "capabilities_reported": isinstance((eng or {}).get("available"), list)},
            "operations": operations, "supported_operations": supported,
            "unsupported": unsupported_list(),
            "checks": checks, "problems": problems,
            "summary": "ready to edit" if not problems else "not ready: " + "; ".join(problems), "secrets_shown": False}


def format_doctor(rep: Dict[str, Any]) -> str:
    lines = [f"video-editing-skill {rep['skill']['version']}: {rep['summary']}",
             "  operations: " + ", ".join(f"{r['type']}={r['status']}" for r in rep.get("operations", []))]
    for c in rep["checks"]:
        extra = " ".join(f"{k}={v}" for k, v in c.items() if k not in ("check", "status", "detail") and v not in (None, [], {}))
        line = f"  {c['check']:<28} {c['status']:<10} {extra}"
        if c.get("detail"):
            line += f"  ({c['detail']})"
        lines.append(line.rstrip())
    return "\n".join(lines)
