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
  no_xfade       doctor reports filter:xfade / filter:acrossfade missing (CONCAT unavailable, the rest fine)
  stale_reuse    probe reports no video stream for a *finished* work-dir intermediate (a reuse candidate), never for a partial
Probe facts: a video whose name contains "noaudio" has no audio stream; an image whose bytes start with "corrupt" does
not decode (no frame size); a video whose name contains "silent" is fine but has no audio (alias of noaudio); "hdr" in the
name -> hdr: true (the fake encodes such sources as hevc, like the engine); "rot90" -> rotation: 90 (stored 640x360, shown
360x640); "vfr" -> variable_frame_rate_suspected: true; "wide" -> 1280x720 @ 25 fps. Frame targets follow fit.py / join.py
exactly (even(): round then up to even). --crf / --preset are recorded in the output header (visible through probe as
"encoding" for the tests).
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
    def header(path):
        try:
            with open(path, "rb") as fh:
                head = fh.read()
            if head.startswith(b"FAKE{"):
                return json.loads(head[4:].decode())
        except (OSError, ValueError):
            pass
        return {}
    def even(n):
        v = int(round(n))
        return v if v % 2 == 0 else v + 1
    def probe_doc(path):
        h = header(path)
        base = os.path.basename(path)
        wide = "wide" in base and not h
        hdr = h.get("hdr", "hdr" in base)
        video = {"codec": h.get("codec", "hevc" if hdr else "h264"), "width": h.get("width", 1280 if wide else 640), "height": h.get("height", 720 if wide else 360),
                 "fps": h.get("fps", 25.0 if wide else 30.0), "rotation": 90 if ("rot90" in base and not h) else 0, "hdr": hdr,
                 "variable_frame_rate_suspected": ("vfr" in base and not h)}
        if h.get("encoding"):
            video["encoding"] = h["encoding"]
        if mode == "bad_probe" and ".partial." in path:
            video = None
        if mode == "stale_reuse" and os.sep + "work" + os.sep in path and ".partial." not in path:
            video = None
        dur = dur_of(path)
        if mode == "short" and ".partial." in path:
            dur = 0.5
        if path.endswith((".png", ".jpg", ".jpeg")):
            try:
                with open(path, "rb") as fh:
                    corrupt = fh.read(7) == b"corrupt"
            except OSError:
                corrupt = True
            return {"file": path, "duration": None, "size_bytes": 1, "video": None if corrupt else {"codec": "png", "width": 120, "height": 40, "pix_fmt": "rgba"}, "audio": None}
        base = os.path.basename(path)
        audio = None if ("noaudio" in base or "silent" in base) else {"codec": "aac", "channels": 2, "sample_rate": 48000}
        try:
            with open(path, "rb") as fh:
                head = fh.read()
            if head.startswith(b"FAKE{"):
                audio = None if json.loads(head[4:].decode()).get("noaudio") else audio
        except (OSError, ValueError):
            pass
        return {"file": path, "duration": dur, "size_bytes": os.path.getsize(path) if os.path.exists(path) else 0, "video": video, "audio": audio}
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
        inputs = [a for a in args[:args.index("-o")] if not a.startswith("--")]
        noaudio = all(("noaudio" in os.path.basename(p) or "silent" in os.path.basename(p) or (probe_doc(p).get("audio") is None)) for p in inputs if not p.endswith(".png")) if name != "join" else all(probe_doc(p).get("audio") is None for p in inputs)
        first = probe_doc(inputs[0])["video"]
        sw, sh = first["width"], first["height"]
        if first.get("rotation") in (90, -90, 270, -270):
            sw, sh = sh, sw
        width, height, fps = sw, sh, first["fps"]
        if flag("--fps"):
            fps = float(flag("--fps"))
        if name == "join":   # join.py rule
            if flag("--width") and flag("--height"):
                width, height = int(flag("--width")), int(flag("--height"))
            elif flag("--width"):
                width = int(flag("--width")); height = int(round(width * sh / sw))
            elif flag("--height"):
                height = int(flag("--height")); width = int(round(height * sw / sh))
            width, height = width - (width % 2), height - (height % 2)
        if name == "fit" and (flag("--aspect") or flag("--width")):
            src_ratio = sw / sh
            if flag("--aspect"):
                aw, ah = (int(x) for x in flag("--aspect").split(":"))
                ratio = aw / ah
            else:
                ratio = src_ratio
            width = even(int(flag("--width"))) if flag("--width") else even(sw if ratio <= src_ratio else sh * ratio)
            height = even(width / ratio)
        encoding = {"crf": int(flag("--crf", 18)), "preset": flag("--preset", "medium")}
        with open(out, "wb") as fh:
            fh.write(b"FAKE" + json.dumps({"duration": expected_duration(), "tool": name, "args": args, "noaudio": noaudio,
                                           "width": width, "height": height, "fps": fps, "hdr": first.get("hdr", False),
                                           "codec": "hevc" if first.get("hdr") else "h264", "encoding": encoding}).encode())
    if mode == "noisy":
        sys.stdout.write("some junk line\\n")
    emit({"status": "completed", "output": out, "dry_run": False, "commands": ["ffmpeg -i x " + out]})
''')


DOCTOR = textwrap.dedent('''
    import json, os, sys
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mode = open(os.path.join(ROOT, "MODE")).read().strip()
    available = ["ffmpeg", "ffprobe", "encoder:libx264", "encoder:aac", "filter:xfade", "filter:acrossfade", "filter:loudnorm"]
    if mode == "no_ffmpeg":
        print(json.dumps({"ok": False, "ffmpeg": None, "ffprobe": None, "missing": ["ffmpeg", "ffprobe"], "available": [], "python": "3"})); sys.exit(1)
    if mode == "no_xfade":
        available = [a for a in available if not a.startswith("filter:xfade") and not a.startswith("filter:acrossfade")]
        print(json.dumps({"ok": True, "ffmpeg": "fake-6.0", "ffprobe": "fake-6.0", "missing": ["filter:xfade", "filter:acrossfade"], "available": available, "python": "3"})); sys.exit(0)
    print(json.dumps({"ok": True, "ffmpeg": "fake-6.0", "ffprobe": "fake-6.0", "missing": [], "available": available, "python": "3"}))
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
