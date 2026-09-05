"""The operation allowlist: every type this skill accepts, its arity, its typed parameters and the
ffmpeg-skill tool that executes it. Nothing outside this table can be requested.

Parameters are validated here into canonical Python values (Time / Fraction / int / str). Values that
end up inside an ffmpeg filter graph (pad colour, position) are restricted to closed vocabularies or
integers so no request string can reach a filter expression.
"""
import re
from fractions import Fraction
from typing import Any, Dict, List

from .errors import EditError
from .timebase import Time, fraction_text, parse_fraction

TRANSITIONS = ("fade", "dissolve", "wipeleft", "wiperight", "wipeup", "wipedown", "slideleft", "slideright",
               "circleopen", "circleclose", "fadeblack", "fadewhite", "smoothleft", "smoothright", "radial")
POSITIONS = ("top-left", "top", "top-right", "left", "center", "right", "bottom-left", "bottom", "bottom-right")
PRECISIONS = ("frame", "keyframe")
_COLOR = re.compile(r"^(black|white|gray|grey|red|green|blue|yellow|0x[0-9A-Fa-f]{6})$")
_ASPECT = re.compile(r"^([1-9]\d{0,3}):([1-9]\d{0,3})$")

# execution escape hatches and boundary settings: refused wherever they appear in a request (any level), with reason
# forbidden_key, before the unknown-key check, so a caller learns *why* rather than just that the key is unknown
FORBIDDEN_KEYS = ("command", "commands", "cmd", "argv", "args", "shell", "exec", "executable", "executables", "script", "binary",
                  "filter", "filters", "filter_complex", "filtergraph", "ffmpeg", "ffprobe",
                  "env", "environment", "cwd", "pythonpath",
                  "api_key", "api_token", "token", "secret", "password", "credentials",
                  "workspace", "allowed_input", "allowed_inputs", "allowed_input_roots", "ffmpeg_skill_dir", "engine_dir", "path_policy")

# ---- encoding profile (contract.encoding): what an output may ask for. Everything else is fixed by ffmpeg-skill 0.9.x
# (video_args / aac_args in its _common.py) and is reported, never configured, here.
X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")   # engine list minus placebo
CRF_MIN, CRF_MAX = 14, 28
ENCODING_DEFAULTS = {"crf": 18, "preset": "medium"}   # ffmpeg-skill's own defaults; reported as normalized values when nothing is asked
ENCODING = {
    "request_field": "project.outputs[].encoding (optional object)",
    "parameters": {"crf": f"integer {CRF_MIN}..{CRF_MAX}: x264 constant-quality factor (lower = larger, better; engine default {ENCODING_DEFAULTS['crf']}; +2 on HEVC for HDR sources)",
                   "preset": "|".join(X264_PRESETS) + f" (encoder speed / efficiency trade-off; engine default {ENCODING_DEFAULTS['preset']})"},
    "applies_to": "the operation that produces the output: its re-encode uses the profile; an intermediate feeding two outputs must be asked for one profile",
    "identity": "part of operation_id and idempotency_key when present, so a different profile is a different artifact",
    "fixed_by_engine": {"video_codec": "h264 (libx264, High, yuv420p, bt709 tags) for SDR sources; hevc (libx265, 10-bit, hvc1) for HDR sources",
                        "audio_codec": "aac 192 kb/s, source sample rate and layout (CONCAT: stereo)", "container": "from the output extension (.mp4 | .mov | .mkv)",
                        "resolution": "by RESIZE / FIT / FILL / CONCAT (frame_semantics), never by the profile", "frame_rate": "by params.fps of CONCAT / FIT / FILL / RESIZE; VFR sources are conformed to CFR"},
    "not_configurable": ["video codec choice", "bitrate / CBR / ABR modes", "pixel format / bit depth", "colour space tags", "audio codec, bitrate, sample rate, channel layout",
                         "keyframe interval / GOP", "two-pass encoding", "hardware encoders"],
    "refused": ["unknown keys (INVALID_REQUEST)", "crf or preset outside the vocabulary (INVALID_REQUEST)",
                "a profile on an output produced by TRIM / CUT with precision keyframe: stream copy ignores it (INVALID_REQUEST)",
                "two outputs of one operation with different profiles (DEPENDENCY_ERROR)"],
}

# ---- frame semantics (contract.frame_semantics): RESIZE, FIT and FILL never overlap. Targets follow ffmpeg-skill fit.py
# exactly (even(n) = round(n), +1 when odd), so the frame is normalized before execution and verified afterwards.
FRAME_SEMANTICS = {
    "RESIZE": {"changes": "frame size", "keeps": "the source aspect ratio; nothing is padded, cropped or stretched",
               "target": "width = params.width (even); height = even(width * source_height / source_width)",
               "when": "the picture must become smaller / larger and keep its shape"},
    "FIT": {"changes": "frame aspect ratio", "keeps": "every source pixel (scaled to fit inside, letterboxed / pillarboxed with pad_color)",
            "target": "width = params.width, else source_width when aspect <= source_aspect, else even(source_height * aspect); height = even(width / aspect)",
            "when": "a delivery aspect differs from the source and nothing may be lost"},
    "FILL": {"changes": "frame aspect ratio", "keeps": "the centre of the picture (scaled to cover, centre-cropped); edges are lost",
             "target": "same rule as FIT", "when": "a delivery aspect differs from the source and a full frame matters more than the edges"},
    "rules": ["a source whose rotation metadata is ±90 / 270 is measured with width and height swapped (as the engine does)",
              "even(n) = int(round(n)), +1 when odd (ffmpeg-skill fit.py)",
              "CONCAT (ffmpeg-skill join.py): params.width x params.height; only one given -> the other from the first input's aspect (round); none -> the first input's frame; then floored to even",
              "TRIM / CUT / SPEED / OVERLAY keep the input frame; an input with an odd width or height is refused before execution (INVALID_INPUT odd_frame) because the engine's encoders need even sizes",
              "no operation stretches (distorts) the picture; anamorphic output is not provided",
              "the normalized target frame is reported in plan.steps[].normalized and execution.operations[].normalized and verified on the output exactly"],
}

# ---- media policy (contract.media_policy): who handles what
MEDIA_POLICY = {
    "refused_before_execution": [
        "source with no video stream (audio-only files, corrupt containers): INVALID_INPUT no_video_stream",
        "video source without a duration (a still image or a broken container declared as video): INVALID_INPUT no_duration",
        "image source that does not decode to a frame: INVALID_INPUT image_undecodable",
        "OVERLAY on a video input without an audio stream: INVALID_INPUT audio_required (ffmpeg-skill 0.9.x overlay never terminates on it)",
        "TRIM / CUT / SPEED / OVERLAY on an input whose frame has an odd width or height: INVALID_INPUT odd_frame (the encoder needs even sizes; RESIZE / FIT / FILL / CONCAT normalize to even)",
        "CONCAT of HDR and SDR inputs: INVALID_INPUT hdr_mismatch (the engine encodes from the first input's colour system)",
        "unsupported input extension / output container: UNSUPPORTED_FORMAT", "ranges beyond the input duration, transitions longer than half an input: INVALID_TIME_RANGE",
        "engine tool / encoder / filter missing: TOOL_ERROR (not retryable)",
    ],
    "normalized_by_skill": [
        "times in any accepted form -> exact rational seconds", "target frame of RESIZE / FIT / FILL / CONCAT (frame_semantics)",
        "target duration of SPEED (input duration / factor)", "encoding profile -> typed engine flags (crf, preset)", "audio expectation per operation (kept / added / none)",
    ],
    "delegated_to_engine": [
        "scaling, padding, cropping filters and their exact pixel maths", "constant-frame-rate conform of variable-frame-rate sources (a warning is reported)",
        "resampling, silence insertion and stereo layout in CONCAT", "HDR sources encoded HEVC 10-bit (the output codec is hevc; a warning is reported)",
        "audio codec / bitrate / sample rate (AAC 192 kb/s)", "keyframe snapping for precision keyframe TRIM / CUT",
    ],
    "by_stream": {
        "video_only": "TRIM / CUT / SPEED / FIT / FILL / RESIZE / CONCAT: allowed; the output has no audio stream (validated). OVERLAY: refused (audio_required)",
        "video_and_audio": "all operations; the audio stream is kept (validated)",
        "audio_only": "refused as a source (no_video_stream); audio-only editing is not provided",
        "image": "OVERLAY.params.image only (png / jpg, alpha respected); an image in a video slot is DEPENDENCY_ERROR kind_mismatch",
        "mixed_audio_presence_in_concat": "allowed; the engine inserts silence for the inputs without audio and the output has audio (validated)",
        "different_resolution_in_concat": "allowed; conformed by params.mode to the normalized frame",
        "different_frame_rate_in_concat": "allowed; conformed to params.fps or the first input's rate",
        "variable_frame_rate": "allowed; conformed to constant fps by the engine (warning)",
        "hdr": "allowed alone (output hevc, warning); not mixed with SDR in CONCAT",
        "rotation_metadata": "honoured (a display matrix): the frame is measured as displayed; a legacy `rotate` tag is ignored by ffmpeg >= 5 and therefore by the probe",
        "odd_frame": "RESIZE / FIT / FILL / CONCAT normalize to even sizes; TRIM / CUT / SPEED / OVERLAY refuse it up front (odd_frame)",
    },
}

MAX_SPEED = Fraction(4)
MIN_SPEED = Fraction(1, 4)
MAX_DIMENSION = 8192

# type -> {"arity": "one" | "many", "tool": ffmpeg-skill tool, "capability": name, "summary": text}
OPERATIONS: Dict[str, Dict[str, Any]] = {
    "TRIM": {"arity": "one", "tool": "ffmpeg-skill/cut", "capability": "video.trim",
             "summary": "keep one source time range [start, end)"},
    "CUT": {"arity": "one", "tool": "ffmpeg-skill/cut", "capability": "video.cut",
            "summary": "keep several ranges of one input, joined in the order given (removes everything else)"},
    "CONCAT": {"arity": "many", "tool": "ffmpeg-skill/join", "capability": "video.concat",
               "summary": "join two or more inputs in order, optionally with a transition, conforming size / fps"},
    "SPEED": {"arity": "one", "tool": "ffmpeg-skill/fit", "capability": "video.speed",
              "summary": "retime by a constant factor (video and pitch-preserved audio)"},
    "FIT": {"arity": "one", "tool": "ffmpeg-skill/fit", "capability": "video.fit",
            "summary": "letterbox / pillarbox into an aspect ratio (nothing is cropped)"},
    "FILL": {"arity": "one", "tool": "ffmpeg-skill/fit", "capability": "video.fill",
             "summary": "centre-crop into an aspect ratio (edges are lost)"},
    "RESIZE": {"arity": "one", "tool": "ffmpeg-skill/fit", "capability": "video.resize",
               "summary": "scale to a width keeping the aspect ratio"},
    "OVERLAY": {"arity": "one", "tool": "ffmpeg-skill/overlay", "capability": "video.overlay",
                "summary": "composite a still image (logo, lower-third PNG) at a named position for a time range"},
}

# Media compatibility per operation: what every input must be, what the output keeps, and which mismatches are
# refused *before* anything reaches the engine (executor._check_media) or verified afterwards (executor._validate).
# "requires" is machine-checked against probes; the text fields are the contract's documentation of the rule.
MEDIA: Dict[str, Dict[str, Any]] = {
    "TRIM": {"inputs": "one video", "requires": {"video": True, "audio": False, "image": False},
             "output": {"frame_size": "as input", "audio": "as input", "fps": "as input"},
             "refused_before_execution": ["source without a video stream or duration", "range beyond the input duration", "input frame with an odd width or height (odd_frame)"]},
    "CUT": {"inputs": "one video", "requires": {"video": True, "audio": False, "image": False},
            "output": {"frame_size": "as input", "audio": "as input", "fps": "as input"},
            "refused_before_execution": ["source without a video stream or duration", "range beyond the input duration", "input frame with an odd width or height (odd_frame)"]},
    "CONCAT": {"inputs": "two or more videos (any sizes / frame rates; conformed by params.mode to params.width/height/fps or the first input)",
               "requires": {"video": True, "audio": False, "image": False},
               "output": {"frame_size": "params.width x params.height, else the first input", "fps": "params.fps, else the first input",
                          "audio": "stereo AAC when any input has audio (silence is inserted for inputs without); none when no input has audio"},
               "refused_before_execution": ["source without a video stream or duration", "an input shorter than twice the transition"]},
    "SPEED": {"inputs": "one video", "requires": {"video": True, "audio": False, "image": False},
              "output": {"frame_size": "as input", "audio": "as input, pitch preserved (atempo)", "fps": "as input", "duration": "input duration / factor"},
              "refused_before_execution": ["source without a video stream or duration", "factor outside [1/4, 4]", "input frame with an odd width or height (odd_frame)"]},
    "FIT": {"inputs": "one video", "requires": {"video": True, "audio": False, "image": False},
            "output": {"frame_size": "params.aspect (params.width when given; padded, nothing cropped)", "audio": "as input", "fps": "params.fps or as input"},
            "refused_before_execution": ["source without a video stream or duration"]},
    "FILL": {"inputs": "one video", "requires": {"video": True, "audio": False, "image": False},
             "output": {"frame_size": "params.aspect (params.width when given; centre-cropped)", "audio": "as input", "fps": "params.fps or as input"},
             "refused_before_execution": ["source without a video stream or duration"]},
    "RESIZE": {"inputs": "one video", "requires": {"video": True, "audio": False, "image": False},
               "output": {"frame_size": "params.width, height by the input aspect (even)", "audio": "as input", "fps": "params.fps or as input"},
               "refused_before_execution": ["source without a video stream or duration"]},
    "OVERLAY": {"inputs": "one video plus one image source (png / jpg; alpha respected)", "requires": {"video": True, "audio": True, "image": True},
                "output": {"frame_size": "as input", "audio": "as input", "fps": "as input"},
                "refused_before_execution": ["source without a video stream or duration", "image that does not decode",
                                             "video input without an audio stream: ffmpeg-skill 0.9.x overlay (-loop 1 image, -shortest) never terminates on it",
                                             "start / end beyond the input duration", "input frame with an odd width or height (odd_frame)"]},
}

# capabilities that video editing normally has but ffmpeg-skill 0.9.x has no tool for: declared as gaps,
# never as capabilities.
UNSUPPORTED: Dict[str, Dict[str, str]] = {
    "CROP": {"capability": "video.crop", "reason": "ffmpeg-skill has no pixel-rectangle crop tool (fit.py only crops to an aspect ratio); use FILL for aspect-ratio crops"},
    "FREEZE": {"capability": "video.freeze", "reason": "ffmpeg-skill has no freeze-frame tool"},
    "REVERSE": {"capability": "video.reverse", "reason": "ffmpeg-skill has no reverse tool"},
    "IMAGE_INSERT": {"capability": "video.image_insert", "reason": "ffmpeg-skill has no still-image-to-clip tool (join.py needs video inputs)"},
    "POSITION": {"capability": "video.position", "reason": "free placement of a video layer is not provided (overlay.py composites images only)"},
    "REORDER": {"capability": "video.reorder", "reason": "not a separate type: give CONCAT its inputs in the wanted order, or CUT its keep ranges in the wanted order"},
    "TRANSITION": {"capability": "video.transition", "reason": "not a separate type: set CONCAT params.transition"},
}


def _int(v: Any, what: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise EditError("INVALID_REQUEST", f"{what}: must be an integer")
    if v < lo or v > hi:
        raise EditError("INVALID_REQUEST", f"{what}: must be between {lo} and {hi}")
    return v


def _even(v: Any, what: str) -> int:
    n = _int(v, what, 2, MAX_DIMENSION)
    if n % 2:
        raise EditError("INVALID_REQUEST", f"{what}: must be even (codec constraint)")
    return n


def _enum(v: Any, what: str, allowed: tuple) -> str:
    if not isinstance(v, str) or v not in allowed:
        raise EditError("INVALID_REQUEST", f"{what}: must be one of {list(allowed)}")
    return v


def _color(v: Any, what: str) -> str:
    if not isinstance(v, str) or not _COLOR.match(v):
        raise EditError("INVALID_REQUEST", f"{what}: must be a named colour (black, white, ...) or 0xRRGGBB")
    return v


def _aspect(v: Any, what: str) -> str:
    if not isinstance(v, str) or not _ASPECT.match(v):
        raise EditError("INVALID_REQUEST", f"{what}: must look like W:H, e.g. 16:9")
    return v


def _range(raw: Any, what: str) -> Dict[str, Time]:
    if not isinstance(raw, dict) or set(raw) != {"start", "end"}:
        raise EditError("INVALID_REQUEST", f"{what}: must be an object with start and end")
    start, end = Time.parse(raw["start"], what + ".start"), Time.parse(raw["end"], what + ".end")
    if not start < end:
        raise EditError("INVALID_TIME_RANGE", f"{what}: start must be before end", {"start": start.text(), "end": end.text()})
    return {"start": start, "end": end}


def _keys(params: Dict[str, Any], what: str, allowed: tuple, required: tuple = ()) -> None:
    for k in params:
        if isinstance(k, str) and k.lower() in FORBIDDEN_KEYS:
            raise EditError("INVALID_REQUEST", f"{what}: key {k!r} is not accepted (this skill takes typed operations, never commands)",
                            {"reason": "forbidden_key"})
    extra = sorted(set(params) - set(allowed))
    if extra:
        raise EditError("INVALID_REQUEST", f"{what}: unknown parameters {extra}", {"allowed": list(allowed)})
    missing = sorted(set(required) - set(params))
    if missing:
        raise EditError("INVALID_REQUEST", f"{what}: missing parameters {missing}")


def _frame(params: Dict[str, Any], what: str, out: Dict[str, Any]) -> None:
    if "fps" in params:
        fps = parse_fraction(params["fps"], what + ".fps")
        if not (Fraction(1) <= fps <= Fraction(240)):
            raise EditError("INVALID_REQUEST", f"{what}.fps: must be between 1 and 240")
        out["fps"] = fps


def validate_params(op_type: str, params: Any, what: str) -> Dict[str, Any]:
    """Return canonical parameters for an allowlisted type; raise on anything else."""
    if op_type in UNSUPPORTED:
        raise EditError("UNSUPPORTED_OPERATION", f"{what}: {op_type} is not implemented: {UNSUPPORTED[op_type]['reason']}",
                        {"type": op_type, "supported": sorted(OPERATIONS)})
    if op_type not in OPERATIONS:
        raise EditError("UNSUPPORTED_OPERATION", f"{what}: unknown operation type", {"type": op_type, "supported": sorted(OPERATIONS)})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise EditError("INVALID_REQUEST", f"{what}.params: must be an object")
    p: Dict[str, Any] = {}
    if op_type == "TRIM":
        _keys(params, what, ("start", "end", "precision"), ("start", "end"))
        p.update(_range({"start": params["start"], "end": params["end"]}, what))
        p["precision"] = _enum(params.get("precision", "frame"), what + ".precision", PRECISIONS)
    elif op_type == "CUT":
        _keys(params, what, ("keep", "precision"), ("keep",))
        keep = params["keep"]
        if not isinstance(keep, list) or not keep or len(keep) > 500:
            raise EditError("INVALID_REQUEST", f"{what}.keep: must be a non-empty list of ranges (max 500)")
        p["keep"] = [_range(r, f"{what}.keep[{i}]") for i, r in enumerate(keep)]
        p["precision"] = _enum(params.get("precision", "frame"), what + ".precision", PRECISIONS)
    elif op_type == "CONCAT":
        _keys(params, what, ("transition", "width", "height", "fps", "mode", "pad_color"))
        tr = params.get("transition")
        if tr is not None:
            if not isinstance(tr, dict):
                raise EditError("INVALID_REQUEST", f"{what}.transition: must be an object")
            _keys(tr, what + ".transition", ("type", "duration"), ("type", "duration"))
            d = Time.parse(tr["duration"], what + ".transition.duration")
            if not (Fraction(1, 100) <= d.value <= Fraction(10)):
                raise EditError("INVALID_TIME_RANGE", f"{what}.transition.duration: must be between 0.01 and 10 seconds")
            p["transition"] = {"type": _enum(tr["type"], what + ".transition.type", TRANSITIONS), "duration": d}
        if "width" in params:
            p["width"] = _even(params["width"], what + ".width")
        if "height" in params:
            p["height"] = _even(params["height"], what + ".height")
        p["mode"] = _enum(params.get("mode", "pad"), what + ".mode", ("pad", "crop"))
        p["pad_color"] = _color(params.get("pad_color", "black"), what + ".pad_color")
        _frame(params, what, p)
    elif op_type == "SPEED":
        _keys(params, what, ("factor",), ("factor",))
        f = parse_fraction(params["factor"], what + ".factor")
        if not (MIN_SPEED <= f <= MAX_SPEED):
            raise EditError("INVALID_REQUEST", f"{what}.factor: must be between {fraction_text(MIN_SPEED)} and {fraction_text(MAX_SPEED)}")
        if f == 1:
            raise EditError("INVALID_REQUEST", f"{what}.factor: 1 changes nothing; drop the operation")
        p["factor"] = f
    elif op_type in ("FIT", "FILL"):
        _keys(params, what, ("aspect", "width", "fps") + (("pad_color",) if op_type == "FIT" else ()), ("aspect",))
        p["aspect"] = _aspect(params["aspect"], what + ".aspect")
        if "width" in params:
            p["width"] = _even(params["width"], what + ".width")
        if op_type == "FIT":
            p["pad_color"] = _color(params.get("pad_color", "black"), what + ".pad_color")
        _frame(params, what, p)
    elif op_type == "RESIZE":
        _keys(params, what, ("width", "fps"), ("width",))
        p["width"] = _even(params["width"], what + ".width")
        _frame(params, what, p)
    elif op_type == "OVERLAY":
        _keys(params, what, ("image", "position", "margin", "scale", "opacity", "start", "end", "fade"), ("image",))
        img = params["image"]
        if not isinstance(img, str) or not img:
            raise EditError("INVALID_REQUEST", f"{what}.image: must reference an image source id")
        p["image"] = img
        pos = params.get("position", "top-right")
        if isinstance(pos, dict):
            _keys(pos, what + ".position", ("x", "y"), ("x", "y"))
            p["position"] = {"x": _int(pos["x"], what + ".position.x", -MAX_DIMENSION, MAX_DIMENSION),
                             "y": _int(pos["y"], what + ".position.y", -MAX_DIMENSION, MAX_DIMENSION)}
        else:
            p["position"] = _enum(pos, what + ".position", POSITIONS)
        p["margin"] = _int(params.get("margin", 24), what + ".margin", 0, 1024)
        if "scale" in params:
            p["scale"] = _int(params["scale"], what + ".scale", 1, MAX_DIMENSION)
        op = params.get("opacity", 1)
        if isinstance(op, bool) or not isinstance(op, (int, float)) or not (0 < op <= 1):
            raise EditError("INVALID_REQUEST", f"{what}.opacity: must be a number in (0, 1]")
        p["opacity"] = Fraction(repr(float(op))).limit_denominator(1000)
        if "start" in params:
            p["start"] = Time.parse(params["start"], what + ".start")
        if "end" in params:
            p["end"] = Time.parse(params["end"], what + ".end")
        if "start" in p and "end" in p and not p["start"] < p["end"]:
            raise EditError("INVALID_TIME_RANGE", f"{what}: start must be before end")
        if "fade" in params:
            fd = parse_fraction(params["fade"], what + ".fade")
            if not (0 <= fd <= 10):
                raise EditError("INVALID_REQUEST", f"{what}.fade: must be between 0 and 10 seconds")
            p["fade"] = fd
    return p


def validate_encoding(raw: Any, what: str) -> Dict[str, Any]:
    """Canonical encoding profile ({crf?, preset?}); unknown keys and out-of-vocabulary values are refused."""
    if not isinstance(raw, dict):
        raise EditError("INVALID_REQUEST", f"{what}: must be an object")
    _keys(raw, what, ("crf", "preset"))
    out: Dict[str, Any] = {}
    if "crf" in raw:
        out["crf"] = _int(raw["crf"], what + ".crf", CRF_MIN, CRF_MAX)
    if "preset" in raw:
        out["preset"] = _enum(raw["preset"], what + ".preset", X264_PRESETS)
    if not out:
        raise EditError("INVALID_REQUEST", f"{what}: an empty profile changes nothing; drop it")
    return out


def even(n: Any) -> int:
    """ffmpeg-skill's even(): round to the nearest integer, then up to even. Frame targets follow it exactly."""
    v = int(round(float(n)))
    return v if v % 2 == 0 else v + 1


def params_to_json(p: Dict[str, Any]) -> Dict[str, Any]:
    """Canonical JSON form of validated parameters (Time -> {seconds, rational}, Fraction -> 'N/D')."""
    def conv(v: Any) -> Any:
        if isinstance(v, Time):
            return v.to_dict()
        if isinstance(v, Fraction):
            return fraction_text(v)
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v
    return conv(p)


def capability_list() -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    for t, spec in OPERATIONS.items():
        seen.setdefault(spec["capability"], {"capability": spec["capability"], "operations": [], "tool": spec["tool"]})["operations"].append(t)
    extra = {"video.transition": {"capability": "video.transition", "operations": ["CONCAT (params.transition)"], "tool": "ffmpeg-skill/join"},
             "video.reorder": {"capability": "video.reorder", "operations": ["CONCAT (input order)", "CUT (keep order)"], "tool": "ffmpeg-skill/cut | ffmpeg-skill/join"}}
    seen.update(extra)
    return [seen[k] for k in sorted(seen)]


def media_compatibility() -> Dict[str, Dict[str, Any]]:
    """Per operation type: input requirements, output guarantees and the mismatches refused before execution."""
    return {t: {"inputs": m["inputs"], "requires": dict(m["requires"]), "output": dict(m["output"]),
                "refused_before_execution": list(m["refused_before_execution"])} for t, m in sorted(MEDIA.items())}


def unsupported_list() -> List[Dict[str, str]]:
    return [{"type": t, "capability": u["capability"], "status": "NOT_IMPLEMENTED", "reason": u["reason"]}
            for t, u in sorted(UNSUPPORTED.items()) if t not in ("REORDER", "TRANSITION")]
