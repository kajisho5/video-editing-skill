"""A fake ffmpeg-skill checkout for error-contract tests (no ffmpeg needed).

It is a test double for the *engine boundary only*: scripts/probe.py, cut.py, join.py, fit.py, overlay.py that
speak ffmpeg-skill's --json protocol and behave according to a MODE file. It is never used to claim real
integration; tests/test_integration.py runs the real ffmpeg-skill.

Modes (written to <root>/MODE):
  ok             tool writes a small file and probe reports a 2 s video
  no_output      tool exits 0 with status completed but writes nothing
  bad_probe      tool writes a file; probe reports no video stream
  short          tool writes a file; probe reports 0.5 s instead of the expected duration
  ffmpeg_fail    tool exits 1 with {"status": "failed", "error": {"kind": "ffmpeg"}}
  missing_tool   tool exits 127 with kind missing_tool
  hang           tool sleeps 60 s (for timeout / cancellation)
  noisy          like ok, but prints junk before the JSON on stdout
  no_ffmpeg      doctor reports ffmpeg / ffprobe missing (tools never run)
"""
import json
import os
import textwrap

SCRIPT = textwrap.dedent('''
    import json, os, sys, time
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mode = open(os.path.join(ROOT, "MODE")).read().strip()
    name = os.path.basename(__file__)[:-3]
    args = sys.argv[1:]
    dry = "--dry-run" in args
    def emit(doc):
        sys.stdout.write(json.dumps(doc) + "\\n")
    def dur_of(path):
        try:
            with open(path, "rb") as fh:
                head = fh.read()
            if head.startswith(b"FAKE{"):
                return float(json.loads(head[4:].decode())["duration"])
        except (OSError, ValueError):
            pass
        return 2.0  # every real (fixture) source is "2 s"
    def probe_doc(path):
        video = {"codec": "h264", "width": 640, "height": 360, "fps": 30.0}
        if mode == "bad_probe" and ".partial." in path:
            video = None
        dur = dur_of(path)
        if mode == "short" and ".partial." in path:
            dur = 0.5
        if path.endswith(".png"):
            return {"file": path, "duration": None, "size_bytes": 1, "video": {"codec": "png", "width": 120, "height": 40}, "audio": None}
        return {"file": path, "duration": dur, "size_bytes": os.path.getsize(path) if os.path.exists(path) else 0, "video": video, "audio": None}
    def flag(name, default=None):
        for a in args:
            if a.startswith(name + "="):
                return a.split("=", 1)[1]
        return args[args.index(name) + 1] if name in args else default
    def expected_duration():
        inputs = [a for a in args[:args.index("-o")]]
        if name == "cut":
            if flag("--segments"):
                return sum(float(seg.split("-")[1]) - float(seg.split("-")[0]) for seg in flag("--segments").split(","))
            return float(flag("--end")) - float(flag("--start", "0"))
        if name == "fit" and flag("--duration"):
            return float(flag("--duration"))
        if name == "join":
            return sum(dur_of(p) for p in inputs) - float(flag("--duration", "0")) * (len(inputs) - 1)
        return dur_of(inputs[0])
    if name == "probe":
        emit(probe_doc(args[0])); sys.exit(0)
    out = args[args.index("-o") + 1]
    if mode == "hang":
        time.sleep(60)
    if mode == "missing_tool":
        emit({"status": "failed", "error": {"kind": "missing_tool", "message": "ffmpeg not found"}}); sys.exit(127)
    if mode == "ffmpeg_fail":
        sys.stderr.write("error: command failed (1): ffmpeg\\nConversion failed!\\n")
        emit({"status": "failed", "error": {"kind": "ffmpeg", "message": "command failed (1): ffmpeg"}}); sys.exit(1)
    if dry:
        emit({"status": "completed", "output": out, "dry_run": True, "commands": ["ffmpeg -i x " + out]}); sys.exit(0)
    if mode != "no_output":
        with open(out, "wb") as fh:
            fh.write(b"FAKE" + json.dumps({"duration": expected_duration(), "tool": name, "args": args}).encode())
    if mode == "noisy":
        sys.stdout.write("some junk line\\n")
    emit({"status": "completed", "output": out, "dry_run": False, "commands": ["ffmpeg -i x " + out]})
''')


DOCTOR = textwrap.dedent('''
    import json, os, sys
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mode = open(os.path.join(ROOT, "MODE")).read().strip()
    if mode == "no_ffmpeg":
        print(json.dumps({"ok": False, "ffmpeg": None, "ffprobe": None, "missing": ["ffmpeg", "ffprobe"], "python": "3"})); sys.exit(1)
    print(json.dumps({"ok": True, "ffmpeg": "fake-6.0", "ffprobe": "fake-6.0", "missing": [], "python": "3"}))
''')


def make_fake_skill(root: str, mode: str = "ok", version: str = "0.9.0") -> str:
    scripts = os.path.join(root, "scripts")
    os.makedirs(scripts, exist_ok=True)
    for name in ("probe", "cut", "join", "fit", "overlay"):
        with open(os.path.join(scripts, name + ".py"), "w", encoding="utf-8") as fh:
            fh.write(SCRIPT)
    with open(os.path.join(scripts, "_contract.py"), "w", encoding="utf-8") as fh:
        fh.write(DOCTOR)
    with open(os.path.join(root, "package.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": "ffmpeg-skill", "version": version}, fh)
    set_mode(root, mode)
    return root


def set_mode(root: str, mode: str) -> None:
    with open(os.path.join(root, "MODE"), "w", encoding="utf-8") as fh:
        fh.write(mode)
