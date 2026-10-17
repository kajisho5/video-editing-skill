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
exactly (even(): round then up to even), including fit.py's aspect-only canvas rule before / from 0.16.10 by package.json version. --crf / --preset are recorded in the output header (visible through probe as
"encoding" for the tests).

Layouts (make_fake_skill(layout=...), written to <root>/LAYOUT; ADR-013's capability detection is tested against them):
  None        the original fake: _contract.py answers every call with the doctor document and the tools take any
              flag, so nothing is detected and every flag passes under its historical spelling
  0.10        a 0.10.0-shaped checkout: contract --json lists flags; --crf; fit.py has no --rotate / --flip
  0.x         0.11+ (and every 0.x after): as 0.10 plus fit.py --rotate / --flip
  1.x         1.10+: --crf and --quality both accepted, --overwrite exists (an existing output is replaced); a tool
              (dry runs included) refuses an output directory that does not exist
  2.x         2.0+: --crf removed (unknown argument, exit 2), --quality; an existing output is refused (kind: input)
              unless --overwrite; probe's hdr is PQ/HLG only and bt2020_or_hdr carries the old meaning
  help-only   2.x flags, but contract --json describes no tools: flags are only readable from each tool's --help
With a layout, a tool exits 2 (argparse "unrecognized arguments") on any flag its layout does not list. A source whose
name contains "bt2020" is BT.2020 SDR: the engine routes it like HDR (hevc), and only 2.x/help-only probe say hdr: false.
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
    LAYOUT = open(os.path.join(ROOT, "LAYOUT")).read().strip() if os.path.exists(os.path.join(ROOT, "LAYOUT")) else None
    FLAGS = json.load(open(os.path.join(ROOT, "FLAGS.json"))).get(name, []) if LAYOUT else None
    NEW_PROBE = LAYOUT in ("2.x", "help-only")
    if LAYOUT and "--help" in args:
        sys.stdout.write("usage: " + name + ".py [-h] input [-o OUTPUT] " + " ".join("[" + f + "]" for f in FLAGS) + "\\n"); sys.exit(0)
    if LAYOUT:
        bad = [a.split("=", 1)[0] for a in args if a.startswith("--") and a.split("=", 1)[0] not in FLAGS + ["--json", "--dry-run", "--output"]]
        if bad:
            sys.stderr.write(name + ".py: error: unrecognized arguments: " + " ".join(bad) + "\\n"); sys.exit(2)
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
        hdr = h.get("hdr", "hdr" in base or "bt2020" in base)            # the wide (1.x) meaning: what the encoders route on
        signal = h.get("signal", "hdr" in base)                           # PQ / HLG only
        video = {"codec": h.get("codec", "hevc" if hdr else "h264"), "width": h.get("width", 1280 if wide else 640), "height": h.get("height", 720 if wide else 360),
                 "fps": h.get("fps", 25.0 if wide else 30.0), "rotation": 90 if ("rot90" in base and not h) else 0, "hdr": signal if NEW_PROBE else hdr,
                 "variable_frame_rate_suspected": ("vfr" in base and not h)}
        if NEW_PROBE:
            video["bt2020_or_hdr"] = hdr
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
    if LAYOUT in ("1.x", "2.x", "help-only") and not os.path.isdir(os.path.dirname(os.path.abspath(out))):
        emit({"status": "failed", "error": {"kind": "input", "message": "output directory does not exist; create it first (this tool never creates directories)"}}); sys.exit(1)
    if NEW_PROBE and os.path.exists(out) and "--overwrite" not in args:
        emit({"status": "failed", "error": {"kind": "input", "message": "output exists; pass --overwrite"}}); sys.exit(1)
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
        first_wide = first.get("bt2020_or_hdr", first.get("hdr", False))
        first_signal = first.get("hdr", False) if NEW_PROBE else (first.get("hdr", False) and "bt2020" not in os.path.basename(inputs[0]))
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
            ver = tuple(int(x) for x in json.load(open(os.path.join(ROOT, "package.json")))["version"].split(".")[:3])
            if flag("--width"):
                width = even(int(flag("--width"))); height = even(width / ratio)
            elif ver >= (0, 16, 10):   # fit.py 0.16.10+: never exceeds the source in either dimension
                if ratio <= src_ratio:
                    height = even(sh); width = even(height * ratio)
                else:
                    width = even(sw); height = even(width / ratio)
            else:
                width = even(sw if ratio <= src_ratio else sh * ratio); height = even(width / ratio)
        if name == "fit" and flag("--rotate") in ("90", "270"):   # fit.py: a 90/270 turn swaps the displayed frame
            width, height = height, width
        encoding = {"crf": int(flag("--quality", flag("--crf", 18))), "preset": flag("--preset", "medium")}
        with open(out, "wb") as fh:
            fh.write(b"FAKE" + json.dumps({"duration": expected_duration(), "tool": name, "args": args, "noaudio": noaudio,
                                           "width": width, "height": height, "fps": fps, "hdr": first_wide, "signal": first_signal,
                                           "codec": "hevc" if first_wide else "h264", "encoding": encoding}).encode())
    if mode == "noisy":
        sys.stdout.write("some junk line\\n")
    emit({"status": "completed", "output": out, "dry_run": False, "commands": ["ffmpeg -i x " + out]})
''')


DOCTOR = textwrap.dedent('''
    import json, os, sys
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    mode = open(os.path.join(ROOT, "MODE")).read().strip()
    LAYOUT = open(os.path.join(ROOT, "LAYOUT")).read().strip() if os.path.exists(os.path.join(ROOT, "LAYOUT")) else None
    if LAYOUT and "doctor" not in sys.argv[1:]:   # contract --json: ToolSpecs with input_schema.properties[].cli
        flags = json.load(open(os.path.join(ROOT, "FLAGS.json"))) if LAYOUT != "help-only" else {}
        tools = [{"id": "ffmpeg-skill/" + t, "input_schema": {"properties": {f[2:].replace("-", "_"): {"cli": [f]} for f in fl}}} for t, fl in flags.items()]
        print(json.dumps({"contract_version": "1.0", "tools": tools})); sys.exit(0)
    available = ["ffmpeg", "ffprobe", "encoder:libx264", "encoder:aac", "filter:xfade", "filter:acrossfade", "filter:loudnorm"]
    if mode == "no_ffmpeg":
        print(json.dumps({"ok": False, "ffmpeg": None, "ffprobe": None, "missing": ["ffmpeg", "ffprobe"], "available": [], "python": "3"})); sys.exit(1)
    if mode == "no_xfade":
        available = [a for a in available if not a.startswith("filter:xfade") and not a.startswith("filter:acrossfade")]
        print(json.dumps({"ok": True, "ffmpeg": "fake-6.0", "ffprobe": "fake-6.0", "missing": ["filter:xfade", "filter:acrossfade"], "available": available, "python": "3"})); sys.exit(0)
    print(json.dumps({"ok": True, "ffmpeg": "fake-6.0", "ffprobe": "fake-6.0", "missing": [], "available": available, "python": "3"}))
''')


_BASE_FLAGS = {"probe": [], "cut": ["--start", "--end", "--segments", "--accurate"],
               "join": ["--transition", "--duration", "--width", "--height", "--fps", "--fit", "--pad-color"],
               "fit": ["--duration", "--method", "--max-speed", "--smooth", "--aspect", "--fit", "--width", "--pad-color", "--crop-x", "--crop-y", "--fps",
                       "--rotate", "--flip"],
               "overlay": ["--image", "--position", "--margin", "--scale", "--opacity", "--start", "--end", "--fade"]}
_LAYOUT_FLAGS = {"0.10": ["--crf", "--preset"], "0.x": ["--crf", "--preset"], "1.x": ["--crf", "--quality", "--preset", "--overwrite"],
                 "2.x": ["--quality", "--preset", "--overwrite"], "help-only": ["--quality", "--preset", "--overwrite"]}


def layout_flags(layout: str) -> dict:
    out = {}
    for tool, base in _BASE_FLAGS.items():
        flags = list(base) + ([] if tool == "probe" else _LAYOUT_FLAGS[layout])
        if layout == "0.10" and tool == "fit":
            flags = [f for f in flags if f not in ("--rotate", "--flip")]
        if tool == "probe" and layout in ("1.x", "2.x", "help-only"):
            flags.append("--overwrite")
        out[tool] = flags
    return out


def make_fake_skill(root: str, mode: str = "ok", version: str = "0.10.0", layout=None) -> str:
    scripts = os.path.join(root, "scripts")
    os.makedirs(scripts, exist_ok=True)
    for name in ("probe", "cut", "join", "fit", "overlay"):
        with open(os.path.join(scripts, name + ".py"), "w", encoding="utf-8") as fh:
            fh.write(SCRIPT)
    with open(os.path.join(scripts, "_contract.py"), "w", encoding="utf-8") as fh:
        fh.write(DOCTOR)
    with open(os.path.join(root, "package.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": "ffmpeg-skill", "version": version}, fh)
    if layout is not None:
        with open(os.path.join(root, "LAYOUT"), "w", encoding="utf-8") as fh:
            fh.write(layout)
        with open(os.path.join(root, "FLAGS.json"), "w", encoding="utf-8") as fh:
            json.dump(layout_flags(layout), fh)
    set_mode(root, mode)
    return root


def set_mode(root: str, mode: str) -> None:
    with open(os.path.join(root, "MODE"), "w", encoding="utf-8") as fh:
        fh.write(mode)
