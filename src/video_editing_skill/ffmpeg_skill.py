"""The media engine boundary: locating ffmpeg-skill and running one of its tools.

video-editing-skill never runs ffmpeg itself. Every media operation is a subprocess
    [python, <ffmpeg-skill>/scripts/<tool>.py, <typed argv>, --json]
built from validated values, in its own process group, with a scrubbed environment and a timeout.
The reply is ffmpeg-skill's --json document ({"status": "completed" | "failed", ...}).

Location (first hit wins): VIDEO_EDITING_FFMPEG_SKILL_DIR, --ffmpeg-skill-dir (CLI), ~/.claude/skills/ffmpeg-skill,
./vendor/ffmpeg-skill, ../ffmpeg-skill, ../kajisho5/ffmpeg-skill. A directory counts when it has scripts/probe.py.
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .errors import EditError

ENV_DIR = "VIDEO_EDITING_FFMPEG_SKILL_DIR"
SUPPORTED_MIN = (0, 9, 0)
SUPPORTED_MAX_EXCLUSIVE = (1, 0, 0)
REQUIRED_TOOLS = ("probe", "cut", "join", "fit", "overlay")
_ENV_KEEP = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "TERM", "SYSTEMROOT", "SYSTEMDRIVE", "PATHEXT",
             "COMSPEC", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA")


@dataclass
class FfmpegSkill:
    root: str
    version: str
    tools: List[str]

    def script(self, tool: str) -> str:
        return os.path.join(self.root, "scripts", f"{tool}.py")

    def version_supported(self) -> bool:
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)", self.version)
        if not m:
            return False
        v = tuple(int(x) for x in m.groups())
        return SUPPORTED_MIN <= v < SUPPORTED_MAX_EXCLUSIVE

    def missing_tools(self) -> List[str]:
        return [t for t in REQUIRED_TOOLS if t not in self.tools]

    def to_dict(self) -> Dict[str, Any]:
        return {"id": "ffmpeg-skill", "version": self.version, "root": self.root, "tools": list(self.tools),
                "version_supported": self.version_supported(), "missing_tools": self.missing_tools()}


def _candidate(path: str) -> Optional[FfmpegSkill]:
    if not os.path.isfile(os.path.join(path, "scripts", "probe.py")):
        return None
    version = "unknown"
    pj = os.path.join(path, "package.json")
    if os.path.isfile(pj):
        try:
            with open(pj, encoding="utf-8") as fh:
                version = str(json.load(fh).get("version", "unknown"))
        except (OSError, ValueError):
            pass
    tools = sorted(f[:-3] for f in os.listdir(os.path.join(path, "scripts")) if f.endswith(".py") and not f.startswith("_"))
    return FfmpegSkill(os.path.realpath(path), version, tools)


def locate(explicit: Optional[str] = None) -> Optional[FfmpegSkill]:
    candidates: List[str] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get(ENV_DIR):
        candidates.append(os.environ[ENV_DIR])
    candidates += [os.path.expanduser("~/.claude/skills/ffmpeg-skill"), os.path.join(os.getcwd(), "vendor", "ffmpeg-skill"),
                   os.path.join(os.getcwd(), "..", "ffmpeg-skill"), os.path.join(os.getcwd(), "..", "kajisho5", "ffmpeg-skill")]
    for c in candidates:
        found = _candidate(c)
        if found:
            return found
    return None


def engine_doctor(skill: FfmpegSkill, timeout: float = 60.0) -> Dict[str, Any]:
    """ffmpeg-skill's own doctor (`scripts/_contract.py doctor --json`): ffmpeg / ffprobe versions and whether
    its required capabilities are present. This skill never runs ffmpeg or ffprobe itself, not even for a
    version string; ffmpeg-skill is the only FFmpeg boundary."""
    script = os.path.join(skill.root, "scripts", "_contract.py")
    result: Dict[str, Any] = {"ffmpeg": None, "ffprobe": None, "ok": False, "missing": [], "detail": None}
    if not os.path.isfile(script):
        result["detail"] = "ffmpeg-skill has no scripts/_contract.py doctor"
        return result
    cmd = [sys.executable, script, "doctor", "--json"]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, env=clean_env(), timeout=timeout, **_group_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        result["detail"] = f"doctor could not run: {exc}"
        return result
    doc = _parse_json(proc.stdout.decode("utf-8", errors="replace"))
    if not isinstance(doc, dict):
        result["detail"] = "doctor printed no JSON: " + proc.stderr.decode("utf-8", errors="replace").strip()[-200:]
        return result
    result["ffmpeg"] = doc.get("ffmpeg") or None
    result["ffprobe"] = doc.get("ffprobe") or None
    result["missing"] = [m for m in doc.get("missing", []) if isinstance(m, str)]
    result["ok"] = bool(doc.get("ok")) and bool(result["ffmpeg"]) and bool(result["ffprobe"])
    if not result["ok"]:
        result["detail"] = "ffmpeg-skill doctor: missing " + ", ".join(result["missing"] or ["ffmpeg / ffprobe"])
    return result


def tool_versions(doctor: Dict[str, Any]) -> Dict[str, Optional[str]]:
    return {"ffmpeg": doctor.get("ffmpeg"), "ffprobe": doctor.get("ffprobe")}


def clean_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_KEEP}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# ---------------------------------------------------------------- running a tool
@dataclass
class ToolRun:
    tool: str
    argv: List[str]
    exit_code: int
    document: Optional[Dict[str, Any]]
    stderr_tail: str
    seconds: float
    timed_out: bool = False
    interrupted: bool = False
    commands: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and isinstance(self.document, dict) and self.document.get("status") != "failed"


class Cancelled(Exception):
    pass


_CANCEL = {"flag": False}


def install_signal_handlers() -> None:
    def handler(signum, frame):  # noqa: ANN001
        _CANCEL["flag"] = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # not the main thread
            pass


def cancelled() -> bool:
    return _CANCEL["flag"]


def reset_cancel() -> None:
    _CANCEL["flag"] = False


def _group_kwargs() -> Dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    try:
        proc.communicate(timeout=5)
    except Exception:
        pass


def run_tool(skill: FfmpegSkill, tool: str, argv: List[str], timeout: float, dry_run: bool = False) -> ToolRun:
    """Run one ffmpeg-skill tool. `argv` is the typed argument list (no --json, no --dry-run)."""
    if tool not in skill.tools:
        raise EditError("TOOL_ERROR", f"ffmpeg-skill has no tool {tool!r}", retryable=False)
    for a in argv:
        if not isinstance(a, str) or "\x00" in a:
            raise EditError("INTERNAL_ERROR", "argument is not a clean string")
    cmd = [sys.executable, skill.script(tool)] + list(argv) + ["--json"] + (["--dry-run"] if dry_run else [])
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=clean_env(), **_group_kwargs())
    except OSError as exc:
        raise EditError("TOOL_ERROR", f"cannot start ffmpeg-skill/{tool}: {exc}", retryable=False) from exc
    out_b = err_b = b""
    timed_out = interrupted = False
    deadline = t0 + timeout
    try:
        while True:
            try:
                out_b, err_b = proc.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if cancelled():
                    interrupted = True
                    kill_tree(proc)
                    break
                if time.monotonic() > deadline:
                    timed_out = True
                    kill_tree(proc)
                    break
    except KeyboardInterrupt:
        kill_tree(proc)
        raise
    out = out_b.decode("utf-8", errors="replace")
    err = err_b.decode("utf-8", errors="replace")
    doc = _parse_json(out)
    return ToolRun(tool, cmd, proc.returncode if proc.returncode is not None else -1, doc,
                   "\n".join(err.strip().splitlines()[-12:]), round(time.monotonic() - t0, 3), timed_out, interrupted,
                   list((doc or {}).get("commands", [])) if isinstance(doc, dict) else [])


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return None
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except ValueError:
        pass
    start = text.find("{")
    if start >= 0:
        try:
            v = json.loads(text[start:])
            return v if isinstance(v, dict) else None
        except ValueError:
            return None
    return None


def probe(skill: FfmpegSkill, path: str, timeout: float = 120.0) -> Dict[str, Any]:
    """ffmpeg-skill/probe measurement document for one file."""
    run = run_tool(skill, "probe", [path], timeout)
    if run.exit_code != 0 or not isinstance(run.document, dict) or run.document.get("status") == "failed":
        raise tool_error(run, f"probe failed on {os.path.basename(path)}")
    return run.document


def tool_error(run: ToolRun, what: str) -> EditError:
    if run.interrupted:
        return EditError("CANCELLED", f"{what}: interrupted", {"tool": run.tool})
    if run.timed_out:
        return EditError("CANCELLED", f"{what}: timed out", {"tool": run.tool, "reason": "timeout"}, retryable=True)
    kind = None
    msg = ""
    if isinstance(run.document, dict) and isinstance(run.document.get("error"), dict):
        kind = run.document["error"].get("kind")
        msg = str(run.document["error"].get("message", ""))
    if kind == "input":
        return EditError("INVALID_INPUT", f"{what}: {msg or run.stderr_tail}", {"tool": run.tool, "ffmpeg_skill_error": "input"}, retryable=False)
    if kind == "missing_tool" or run.exit_code == 127:
        return EditError("TOOL_ERROR", f"{what}: ffmpeg/ffprobe missing", {"tool": run.tool, "ffmpeg_skill_error": "missing_tool"}, retryable=False)
    return EditError("TOOL_ERROR", f"{what}: {msg or run.stderr_tail or f'exit {run.exit_code}'}",
                     {"tool": run.tool, "exit_code": run.exit_code, "ffmpeg_skill_error": kind}, retryable=True)


