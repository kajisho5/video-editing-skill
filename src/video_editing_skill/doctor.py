"""Environment report: python, ffmpeg-skill (location, version, tools), ffmpeg / ffprobe, path policy.

Prints no environment variables and no secrets. Exit 1 when the skill cannot run here.
"""
import platform
from typing import Any, Dict, List, Optional

from . import DOCTOR_SCHEMA, SKILL_ID, VERSION
from .ffmpeg_skill import ENV_DIR, REQUIRED_TOOLS, locate, tool_versions
from .paths import PathPolicy
from .errors import EditError


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

    versions = tool_versions()
    for name in ("ffmpeg", "ffprobe"):
        v = versions.get(name)
        checks.append({"check": name, "status": "AVAILABLE" if v else "MISSING", "version": v})
        if not v:
            problems.append(f"{name} not on PATH")

    if workspace:
        try:
            policy = PathPolicy(workspace, allowed_inputs)
            checks.append({"check": "path_policy", "status": "AVAILABLE", **policy.describe()})
        except EditError as exc:
            checks.append({"check": "path_policy", "status": "MISSING", "detail": exc.message})
            problems.append("path policy: " + exc.message)
    else:
        checks.append({"check": "path_policy", "status": "UNKNOWN", "detail": "pass --workspace to check a workspace"})

    return {"schema": DOCTOR_SCHEMA, "ok": not problems, "skill": {"id": SKILL_ID, "version": VERSION}, "checks": checks, "problems": problems,
            "summary": "ready to edit" if not problems else "not ready: " + "; ".join(problems), "secrets_shown": False}


def format_doctor(rep: Dict[str, Any]) -> str:
    lines = [f"video-editing-skill {rep['skill']['version']}: {rep['summary']}"]
    for c in rep["checks"]:
        extra = " ".join(f"{k}={v}" for k, v in c.items() if k not in ("check", "status", "detail") and v not in (None, [], {}))
        line = f"  {c['check']:<28} {c['status']:<10} {extra}"
        if c.get("detail"):
            line += f"  ({c['detail']})"
        lines.append(line.rstrip())
    return "\n".join(lines)
