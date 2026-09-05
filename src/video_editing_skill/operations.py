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
             "refused_before_execution": ["source without a video stream or duration", "range beyond the input duration"]},
    "CUT": {"inputs": "one video", "requires": {"video": True, "audio": False, "image": False},
            "output": {"frame_size": "as input", "audio": "as input", "fps": "as input"},
            "refused_before_execution": ["source without a video stream or duration", "range beyond the input duration"]},
    "CONCAT": {"inputs": "two or more videos (any sizes / frame rates; conformed by params.mode to params.width/height/fps or the first input)",
               "requires": {"video": True, "audio": False, "image": False},
               "output": {"frame_size": "params.width x params.height, else the first input", "fps": "params.fps, else the first input",
                          "audio": "stereo AAC when any input has audio (silence is inserted for inputs without); none when no input has audio"},
               "refused_before_execution": ["source without a video stream or duration", "an input shorter than twice the transition"]},
    "SPEED": {"inputs": "one video", "requires": {"video": True, "audio": False, "image": False},
              "output": {"frame_size": "as input", "audio": "as input, pitch preserved (atempo)", "fps": "as input", "duration": "input duration / factor"},
              "refused_before_execution": ["source without a video stream or duration", "factor outside [1/4, 4]"]},
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
                                             "start / end beyond the input duration"]},
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
