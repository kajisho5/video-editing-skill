"""Command line: the process boundary.

  video-editing skill [--json]                      manifest / contract
  video-editing contract [--json]                   same document
  video-editing doctor [--json] [--workspace D] [--allowed-input R]...
  video-editing validate <request.json | -> --json --workspace D [--allowed-input R]...
  video-editing plan     <request.json | -> --json --workspace D [--allowed-input R]...   (dry run)
  video-editing run      <request.json | -> --json --workspace D [--allowed-input R]...

stdout carries exactly one JSON document under --json; everything else goes to stderr. Exit codes are
the error table's (0 success). The workspace and the allowed input roots are CLI arguments, never
request fields: a caller that produces the request (an agent, an AI) cannot widen them.
"""
import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from . import PLAN_SCHEMA, RESPONSE_SCHEMA, SKILL_ID, VERSION
from .contract import skill_contract
from .contract_check import run_check
from .doctor import doctor_report, format_doctor
from .errors import EditError
from .executor import Executor, _Failed
from .ffmpeg_skill import install_signal_handlers, locate, tool_versions
from .paths import PathPolicy
from .project import parse_request

MAX_REQUEST_BYTES = 4 * 1024 * 1024


def _utf8_streams() -> None:
    """stdout carries JSON with ensure_ascii=False; never let a legacy console code page (cp932, cp1252) break it."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _print_json(doc: Any) -> None:
    sys.stdout.write(json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=False) + "\n")
    sys.stdout.flush()


def _log(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _read_request(arg: str) -> Dict[str, Any]:
    if arg == "-":
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    else:
        try:
            with open(arg, "rb") as fh:
                raw = fh.read(MAX_REQUEST_BYTES + 1)
        except OSError as exc:
            raise EditError("INVALID_REQUEST", f"cannot read request file: {exc.strerror}")
    if len(raw) > MAX_REQUEST_BYTES:
        raise EditError("INVALID_REQUEST", f"request larger than {MAX_REQUEST_BYTES} bytes", {"reason": "oversized"})
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise EditError("INVALID_REQUEST", f"request is not valid UTF-8 JSON: {exc}")
    return doc


def _engine(args: argparse.Namespace):
    skill = locate(args.ffmpeg_skill_dir)
    if skill is None:
        raise EditError("TOOL_ERROR", "ffmpeg-skill not found (set VIDEO_EDITING_FFMPEG_SKILL_DIR or --ffmpeg-skill-dir)", retryable=False)
    if not skill.version_supported() or skill.missing_tools():
        raise EditError("TOOL_ERROR", f"ffmpeg-skill {skill.version} at {skill.root} is not supported (missing tools {skill.missing_tools()})", retryable=False)
    versions = tool_versions()
    if not versions.get("ffmpeg") or not versions.get("ffprobe"):
        raise EditError("TOOL_ERROR", "ffmpeg / ffprobe not on PATH", retryable=False)
    return skill, versions


def _policy(args: argparse.Namespace) -> PathPolicy:
    if not args.workspace:
        raise EditError("INVALID_REQUEST", "--workspace is required")
    return PathPolicy(args.workspace, args.allowed_input or None)


def _envelope(project, extra: Dict[str, Any]) -> Dict[str, Any]:
    doc: Dict[str, Any] = {"ok": True, "schema": RESPONSE_SCHEMA, "skill": {"id": SKILL_ID, "version": VERSION}}
    doc.update(extra)
    doc["project"] = project.to_dict()
    doc["warnings"] = list(project.warnings)
    return doc


# ---------------------------------------------------------------- commands
def cmd_skill(args: argparse.Namespace) -> int:
    if getattr(args, "check", None) is not None:
        saved = _read_request(args.check) if args.check else None
        rep = run_check(saved)
        if args.json:
            _print_json(rep)
        else:
            print(f"contract check: {rep['status']}")
            for p in rep["implementation"]["problems"] + (rep.get("drift", {}).get("problems", [])):
                print("  - " + p)
        return 0 if rep["ok"] else 1
    doc = skill_contract()
    if args.json:
        _print_json(doc)
    else:
        print(f"{doc['name']} {doc['version']} ({doc['skill_id']})")
        for c in doc["capabilities"]:
            print(f"  {c['capability']:<20} {', '.join(c['operations'])}")
        for u in doc["unsupported"]:
            print(f"  {u['capability']:<20} NOT IMPLEMENTED: {u['reason']}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    rep = doctor_report(args.ffmpeg_skill_dir, args.workspace, args.allowed_input or None)
    if args.json:
        _print_json(rep)
    else:
        print(format_doctor(rep))
    return 0 if rep["ok"] else 1


def cmd_validate(args: argparse.Namespace) -> int:
    policy = _policy(args)
    project = parse_request(_read_request(args.request), policy)
    _print_json(_envelope(project, {"status": "valid", "command": "validate"}))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    policy = _policy(args)
    project = parse_request(_read_request(args.request), policy)
    skill, versions = _engine(args)
    ex = Executor(project, skill, versions, _log if args.verbose else None)
    ex.probe_sources()
    plan = ex.plan(preview=not args.no_preview)
    doc = _envelope(project, {"status": "planned", "command": "plan", "schema": PLAN_SCHEMA, "dry_run": True,
                              "engine": {"ffmpeg-skill": skill.version, **versions}, "plan": plan})
    _print_json(doc)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    policy = _policy(args)
    project = parse_request(_read_request(args.request), policy)
    skill, versions = _engine(args)
    install_signal_handlers()
    ex = Executor(project, skill, versions, _log if args.verbose else None)
    ex.probe_sources()
    try:
        execution = ex.run()
    except _Failed as failed:
        doc = failed.error.envelope()
        doc.update({"schema": RESPONSE_SCHEMA, "skill": {"id": SKILL_ID, "version": VERSION}, "status": failed.doc["status"],
                    "execution": failed.doc, "project": project.to_dict(), "warnings": list(project.warnings)})
        _print_json(doc)
        return failed.error.exit_code
    reused = all(r["status"] == "reused" for r in execution["operations"])
    _print_json(_envelope(project, {"status": "reused" if reused else "completed", "command": "run",
                                    "engine": {"ffmpeg-skill": skill.version, **versions}, "execution": execution}))
    return 0


# ---------------------------------------------------------------- parser
def _add_common(p: argparse.ArgumentParser, request: bool) -> None:
    if request:
        p.add_argument("request", help="request JSON file, or - for stdin")
    p.add_argument("--json", action="store_true", help="print one JSON document on stdout")
    p.add_argument("--workspace", help="the only directory outputs may be written to (required for validate/plan/run)")
    p.add_argument("--allowed-input", action="append", metavar="ROOT", help="directory inputs may be read from (repeatable; default: the workspace)")
    p.add_argument("--ffmpeg-skill-dir", help="ffmpeg-skill checkout (default: VIDEO_EDITING_FFMPEG_SKILL_DIR or the usual locations)")
    p.add_argument("--verbose", action="store_true", help="progress on stderr")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="video-editing", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"video-editing-skill {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("skill", "contract"):
        s = sub.add_parser(name, help="machine-readable contract")
        _add_common(s, request=False)
        s.add_argument("--check", nargs="?", const="", metavar="FILE", default=None,
                       help="verify the contract against the implementation and docs; with FILE (or -) also report drift from that saved contract; exit 1 on any problem")
        s.set_defaults(func=cmd_skill)
    d = sub.add_parser("doctor", help="environment report")
    _add_common(d, request=False)
    d.set_defaults(func=cmd_doctor)
    v = sub.add_parser("validate", help="validate a request (no engine needed)")
    _add_common(v, request=True)
    v.set_defaults(func=cmd_validate, json=True)
    pl = sub.add_parser("plan", help="dry run: operation graph, timeline, tool calls; writes no media")
    _add_common(pl, request=True)
    pl.add_argument("--no-preview", action="store_true", help="skip ffmpeg-skill --dry-run command previews")
    pl.set_defaults(func=cmd_plan, json=True)
    r = sub.add_parser("run", help="execute the request")
    _add_common(r, request=True)
    r.set_defaults(func=cmd_run, json=True)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    _utf8_streams()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except EditError as exc:
        if getattr(args, "json", False):
            _print_json(exc.envelope())
        _log(f"error: [{exc.code}] {exc.message}")
        return exc.exit_code
    except KeyboardInterrupt:
        cancelled = EditError("CANCELLED", "interrupted")
        if getattr(args, "json", False):
            _print_json(cancelled.envelope())
        _log("interrupted")
        return cancelled.exit_code
    except Exception as exc:  # a bug: still one parseable document, never a traceback on stdout
        err = EditError("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
        if getattr(args, "json", False):
            _print_json(err.envelope())
        import traceback
        _log("".join(traceback.format_exc().splitlines(True)[-8:]))
        return err.exit_code


if __name__ == "__main__":
    sys.exit(main())
