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
PRECISIONS = ("frame", "keyframe", "auto", "smart")
AUTO_TOLERANCE = "0.2"   # precision auto: the most a stream copy may deviate from the asked range before that cut is re-encoded (ADR-019)
ROTATIONS = (90, 180, 270)
FLIPS = ("h", "v")
SMOOTH_MODES = ("blend", "interpolate")
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
                "a profile on an output produced by TRIM / CUT with precision keyframe, auto or smart: stream copy ignores it (INVALID_REQUEST)",
                "two outputs of one operation with different profiles (DEPENDENCY_ERROR)"],
}

# ---- frame semantics (contract.frame_semantics): RESIZE, FIT, FILL and ROTATE never overlap. Targets follow
# ffmpeg-skill fit.py exactly (even(n) = round(n), +1 when odd), so the frame is normalized before execution and
# verified afterwards.
FRAME_SEMANTICS = {
    "RESIZE": {"changes": "frame size", "keeps": "the source aspect ratio; nothing is padded, cropped or stretched",
               "target": "width = params.width (even); height = even(width * source_height / source_width)",
               "when": "the picture must become smaller / larger and keep its shape"},
    "FIT": {"changes": "frame aspect ratio", "keeps": "every source pixel (scaled to fit inside, letterboxed / pillarboxed with pad_color)",
            "target": "width = params.width, else source_width when aspect <= source_aspect, else even(source_height * aspect); height = even(width / aspect)",
            "when": "a delivery aspect differs from the source and nothing may be lost"},
    "FILL": {"changes": "frame aspect ratio", "keeps": "the centre of the picture (scaled to cover, centre-cropped); edges are lost",
             "target": "same rule as FIT", "when": "a delivery aspect differs from the source and a full frame matters more than the edges"},
    "ROTATE": {"changes": "picture orientation only (a clockwise turn and/or a mirror); frame size changes only by the width x height swap a 90 / 270 turn implies -- never by scaling",
               "keeps": "every source pixel losslessly; no scaling, padding or cropping (like TRIM / CUT / SPEED / OVERLAY, unlike RESIZE / FIT / FILL)",
               "target": "width, height = source width, height (already measured as displayed); swapped when params.degrees is 90 or 270",
               "when": "a source was recorded or tagged with the wrong orientation, or needs mirroring, and no aspect or size change is wanted"},
    "CROP": {"changes": "frame size, by cutting away everything outside one pixel rectangle",
             "keeps": "the pixels inside the rectangle exactly (no scaling, padding or stretching); the aspect becomes the rectangle's",
             "target": "width, height = params.width, params.height (even); x, y are offsets in the input frame as displayed, and the rectangle must lie inside it",
             "when": "the caller already knows the exact region to keep (a measured letterbox, a saved crop, a region it chose); FILL when the goal is an aspect ratio"},
    "rules": ["a source whose rotation metadata is ±90 / 270 is measured with width and height swapped (as the engine does)",
              "even(n) = int(round(n)), +1 when odd (ffmpeg-skill fit.py)",
              "CONCAT (ffmpeg-skill join.py): params.width x params.height; only one given -> the other from the first input's aspect (round); none -> the first input's frame; then floored to even",
              "IMAGE_INSERT (ffmpeg-skill insert.py): params.width x params.height; only one given -> the other = even(given x the image aspect); none -> even(image width) x "
              "even(image height); the still is scaled to cover that frame and centre-cropped, never stretched",
              "PUNCH_IN keeps the input frame: the window is floor_even(width / scale) x floor_even(height / scale), centred on the subject box, moved only as far "
              "as needed to stay inside the frame (even offsets), cropped (crop.py) and scaled back to the input frame (fit.py --fit crop: never stretched)",
              "TRIM / CUT / SPEED / OVERLAY / ROTATE / FREEZE / REVERSE keep the input frame -- losslessly, no filter maths beyond a turn or a mirror for ROTATE, whose target "
              "frame is the source's width x height, swapped for a 90 / 270 degrees turn; an input with an odd width or height is refused before execution (INVALID_INPUT "
              "odd_frame) because the engine's encoders need even sizes",
              "no operation stretches (distorts) the picture; anamorphic output is not provided",
              "the normalized target frame is reported in plan.steps[].normalized and execution.operations[].normalized and verified on the output exactly"],
}

# ---- media policy (contract.media_policy): who handles what
MEDIA_POLICY = {
    "refused_before_execution": [
        "source with no video stream (audio-only files, corrupt containers): INVALID_INPUT no_video_stream",
        "video source without a duration (a still image or a broken container declared as video): INVALID_INPUT no_duration",
        "image source that does not decode to a frame: INVALID_INPUT image_undecodable",
        "TRIM / CUT / SPEED / OVERLAY / ROTATE / FREEZE / REVERSE on an input whose frame has an odd width or height: INVALID_INPUT odd_frame (the encoder needs even sizes; "
        "RESIZE / FIT / FILL / CONCAT / IMAGE_INSERT normalize to even)",
        "CONCAT of HDR and SDR inputs: INVALID_INPUT hdr_mismatch (the engine encodes from the first input's colour system)",
        "unsupported input extension / output container: UNSUPPORTED_FORMAT", "ranges beyond the input duration, transitions longer than half an input: INVALID_TIME_RANGE",
        "CROP rectangle outside the input frame as displayed: INVALID_REQUEST crop_outside_frame",
        "engine tool / encoder / filter missing: TOOL_ERROR (not retryable); FREEZE / REVERSE / IMAGE_INSERT / CROP need ffmpeg-skill/freeze / reverse / insert / crop, detected per checkout",
    ],
    "normalized_by_skill": [
        "times in any accepted form -> exact rational seconds", "target frame of RESIZE / FIT / FILL / CONCAT / CROP (frame_semantics)",
        "target duration of SPEED (input duration / factor), FREEZE (input duration + hold) and IMAGE_INSERT (params.duration)", "encoding profile -> typed engine flags (crf, preset)", "audio expectation per operation (kept / added / none)",
    ],
    "delegated_to_engine": [
        "scaling, padding, cropping filters and their exact pixel maths", "constant-frame-rate conform of variable-frame-rate sources (a warning is reported)",
        "resampling, silence insertion and stereo layout in CONCAT (an IMAGE_INSERT clip has no audio)", "scaling / centre-cropping a still to its IMAGE_INSERT frame", "silence under a FREEZE hold; reversing the audio with the picture in REVERSE", "HDR sources encoded HEVC 10-bit (the output codec is hevc; a warning is reported)",
        "audio codec / bitrate / sample rate (AAC 192 kb/s)", "keyframe snapping for precision keyframe TRIM / CUT", "copy-or-re-encode per cut for precision auto TRIM / CUT (cut.py --tolerance 0.2; the engine's choice is reported per cut)",
        "which GOPs a precision smart TRIM / CUT re-encodes and whether the result verifies against the source (cut.py --smart; a fallback to a whole re-encode is reported with its reason)",
    ],
    "by_stream": {
        "video_only": "all operations allowed, OVERLAY included (ffmpeg-skill >=0.10.0 bounds the looped-image overlay with an explicit -t instead of -shortest); the output has no audio stream (validated)",
        "video_and_audio": "all operations; the audio stream is kept (validated)",
        "audio_only": "refused as a source (no_video_stream); audio-only editing is not provided",
        "image": "OVERLAY.params.image only (png / jpg, alpha respected); an image in a video slot is DEPENDENCY_ERROR kind_mismatch",
        "mixed_audio_presence_in_concat": "allowed; the engine inserts silence for the inputs without audio and the output has audio (validated)",
        "different_resolution_in_concat": "allowed; conformed by params.mode to the normalized frame",
        "different_frame_rate_in_concat": "allowed; conformed to params.fps or the first input's rate",
        "variable_frame_rate": "allowed; conformed to constant fps by the engine (warning)",
        "hdr": "allowed alone (output hevc, warning); not mixed with SDR in CONCAT",
        "rotation_metadata": "honoured (a display matrix): the frame is measured as displayed; a legacy `rotate` tag is ignored by ffmpeg >= 5 and therefore by the probe",
        "odd_frame": "RESIZE / FIT / FILL / CONCAT / IMAGE_INSERT normalize to even sizes; TRIM / CUT / SPEED / OVERLAY / ROTATE / FREEZE / REVERSE refuse it up front (odd_frame)",
    },
}

MIN_HOLD = Fraction(1, 100)
MAX_HOLD = Fraction(600)
MIN_STILL = Fraction(1, 100)     # IMAGE_INSERT.duration bounds (same as FREEZE.hold: a held picture)
MAX_STILL = Fraction(600)
STILL_FPS_DEFAULT = Fraction(30)   # insert.py's own default, always passed explicitly so the clip never depends on it
MAX_PUNCH = Fraction(4)   # PUNCH_IN.scale: (1, 4]
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
    "ROTATE": {"arity": "one", "tool": "ffmpeg-skill/fit", "capability": "video.rotate",
               "summary": "turn the picture clockwise by 90/180/270 and/or mirror it horizontally or vertically (no scaling, padding or cropping)"},
    "FREEZE": {"arity": "one", "tool": "ffmpeg-skill/freeze", "capability": "video.freeze",
               "summary": "hold one frame for a number of seconds: at a time (the rest of the clip plays on after the hold) or, without one, the last frame at the end"},
    "REVERSE": {"arity": "one", "tool": "ffmpeg-skill/reverse", "capability": "video.reverse",
                "summary": "play the input backwards, picture and audio"},
    "IMAGE_INSERT": {"arity": "one", "tool": "ffmpeg-skill/insert", "capability": "video.image_insert",
                     "summary": "turn a still image (title card, end slate) into a silent video clip of a set duration that CONCAT and every other type can take"},
    "CROP": {"arity": "one", "tool": "ffmpeg-skill/crop", "capability": "video.crop",
             "summary": "keep exactly the pixel rectangle {x, y, width, height} the caller gives (no scaling, padding or aspect maths; FILL crops to an aspect ratio)"},
    "CONDENSE": {"arity": "one", "tool": "ffmpeg-skill/cut", "capability": "video.condense",
                 "summary": "apply a rule per labelled span (cut, keep, or speed by a factor) and keep everything else by params.default: approved "
                            "proposals (retakes, silences, static spans) become segments here"},
    "PUNCH_IN": {"arity": "one", "tool": "ffmpeg-skill/crop", "capability": "video.punch_in",
                 "summary": "zoom in by a scale on a subject box the caller gives (e.g. detection-skill's face box) for a time range: a hard cut in and out, "
                            "the window centred on the box and kept inside the frame, the frame size unchanged"},
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
               "output": {"frame_size": "params.width x params.height, else the first input",
                          "fps": "params.fps, else the first input; constant, with every input joined at whole frames of it (no transition or a stinger: "
                                 "an input at another rate or variable-rate is conformed to it, rounded to the nearest frame; one at that rate whose "
                                 "length is not whole frames is cut, rounded down; the timeline places every clip at those lengths)",
                          "audio": "stereo AAC when any input has audio (silence is inserted for inputs without); none when no input has audio"},
               "refused_before_execution": ["source without a video stream or duration", "an input shorter than twice the transition",
                                            "transition stinger: see contract.stinger.refused"]},
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
    "OVERLAY": {"inputs": "one video plus one image source (png / jpg; alpha respected)", "requires": {"video": True, "audio": False, "image": True},
                "output": {"frame_size": "as input", "audio": "as input", "fps": "as input"},
                "refused_before_execution": ["source without a video stream or duration", "image that does not decode",
                                             "start / end beyond the input duration", "input frame with an odd width or height (odd_frame)"]},
    "ROTATE": {"inputs": "one video", "requires": {"video": True, "audio": False, "image": False},
               "output": {"frame_size": "as input, with width and height swapped when params.degrees is 90 or 270", "audio": "as input", "fps": "as input"},
               "refused_before_execution": ["source without a video stream or duration", "input frame with an odd width or height (odd_frame)"]},
    "FREEZE": {"inputs": "one video", "requires": {"video": True, "audio": False, "image": False},
               "output": {"frame_size": "as input", "audio": "as input, silent during the hold", "fps": "as input",
                          "duration": "input duration + params.hold (the hold is a whole number of frames when params.at is given)"},
               "refused_before_execution": ["source without a video stream or duration", "params.at at or beyond the input duration",
                                            "input frame with an odd width or height (odd_frame)", "an ffmpeg-skill checkout without freeze.py (TOOL_ERROR)"]},
    "REVERSE": {"inputs": "one video (the engine buffers the whole input in memory: keep it short, TRIM first)", "requires": {"video": True, "audio": False, "image": False},
                "output": {"frame_size": "as input", "audio": "as input, reversed", "fps": "as input", "duration": "as input"},
                "refused_before_execution": ["source without a video stream or duration", "input frame with an odd width or height (odd_frame)",
                                             "an ffmpeg-skill checkout without reverse.py (TOOL_ERROR)"]},
    "IMAGE_INSERT": {"inputs": "one image source (png / jpg), never a video or an operation", "requires": {"video": False, "audio": False, "image": True},
                     "output": {"frame_size": "params.width x params.height; only one given -> the other from the image aspect; none -> the image size (each even; "
                                               "the image is scaled to cover the frame and centre-cropped, never stretched)",
                                "audio": "none (a silent clip: CONCAT inserts silence for it next to inputs with audio)", "fps": "params.fps (default 30)",
                                "duration": "params.duration"},
                     "refused_before_execution": ["input that is not an image source (DEPENDENCY_ERROR kind_mismatch)", "image that does not decode",
                                                  "an ffmpeg-skill checkout without insert.py (TOOL_ERROR)"]},
    "CONDENSE": {"inputs": "one video", "requires": {"video": True, "audio": False, "image": False},
                 "output": {"frame_size": "as input", "audio": "kept (stereo AAC: the pieces are joined by ffmpeg-skill/join); sped-up pieces keep their pitch (atempo)",
                            "fps": "as input", "duration": "the kept time plus each sped span's length / its factor"},
                 "refused_before_execution": ["source without a video stream or duration", "segments that overlap or run past the input duration",
                                              "a label without a rule (INVALID_REQUEST missing_rule)", "an edit that keeps nothing (INVALID_REQUEST nothing_kept)",
                                              "input frame with an odd width or height (odd_frame)"]},
    "PUNCH_IN": {"inputs": "one video", "requires": {"video": True, "audio": False, "image": False},
                 "output": {"frame_size": "as input", "audio": "kept (stereo AAC: the parts are joined by ffmpeg-skill/join)", "fps": "as input", "duration": "as input"},
                 "refused_before_execution": ["source without a video stream or duration", "start / end beyond the input duration",
                                              "a subject box outside the input frame (INVALID_REQUEST subject_outside_frame)",
                                              "a subject box larger than the zoomed window (INVALID_REQUEST subject_larger_than_window)",
                                              "input frame with an odd width or height (odd_frame)", "an ffmpeg-skill checkout without crop.py (TOOL_ERROR)"]},
    "CROP": {"inputs": "one video", "requires": {"video": True, "audio": False, "image": False},
             "output": {"frame_size": "params.width x params.height (each even)", "audio": "as input", "fps": "as input", "duration": "as input"},
             "refused_before_execution": ["source without a video stream or duration",
                                          "a rectangle that does not lie inside the input frame as displayed (INVALID_REQUEST crop_outside_frame)",
                                          "an ffmpeg-skill checkout without crop.py (TOOL_ERROR)"]},
}

# capabilities video editing normally has that this Skill does not map to a typed operation: declared as gaps, never as
# capabilities. A reason states what *this Skill* does not do, never what the engine lacks: which ffmpeg-skill tools exist
# is detected from the installed checkout (doctor: unsupported[].engine_tool_status), not written into the contract
# (ADR-014). `engine_tool` names the ffmpeg-skill tool that would execute the type, when there is one.
UNSUPPORTED: Dict[str, Dict[str, Any]] = {
    "POSITION": {"capability": "video.position", "engine_tool": None,
                 "reason": "free placement of a video layer is not provided: OVERLAY composites still images only; a video layer would extend OVERLAY (ADR-002)"},
    "REORDER": {"capability": "video.reorder", "engine_tool": None,
                "reason": "not a separate type: give CONCAT its inputs in the wanted order, or CUT its keep ranges in the wanted order"},
    "TRANSITION": {"capability": "video.transition", "engine_tool": None, "reason": "not a separate type: set CONCAT params.transition"},
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


def _int_choice(v: Any, what: str, allowed: tuple) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v not in allowed:
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


def _unit_float(v: Any, what: str) -> Fraction:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise EditError("INVALID_REQUEST", f"{what}: must be a number")
    if not (0 <= v <= 1):
        raise EditError("INVALID_REQUEST", f"{what}: must be between 0 and 1")
    return Fraction(repr(float(v))).limit_denominator(1000)


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
        if isinstance(tr, dict) and tr.get("type") == "stinger":
            # a graphic with alpha laid over each join; the clips switch at its cut point, where it covers the frame (ADR-023).
            # Its length is the file's; the cut point and the covered window are data the stinger's maker states (stinger.json)
            _keys(tr, what + ".transition", ("type", "stinger", "cut_point", "cover"), ("type", "stinger"))
            for key, reason, why in (("cut_point", "stinger_no_cut_point", "the frame the clips switch at"),
                                     ("cover", "stinger_no_cover", "the window in which the graphic covers the whole frame")):
                if tr.get(key) is None:   # stated by the stinger's maker (stinger.json); never guessed here
                    raise EditError("INVALID_REQUEST", f"{what}.transition: a stinger needs its {key} ({why}), as its stinger.json states it",
                                    {"reason": reason})
            if not isinstance(tr["stinger"], str) or not tr["stinger"]:
                raise EditError("INVALID_REQUEST", f"{what}.transition.stinger: must reference a video source id")
            cp = Time.parse(tr["cut_point"], what + ".transition.cut_point")
            cover = tr["cover"]
            if not isinstance(cover, dict):
                raise EditError("INVALID_REQUEST", f"{what}.transition.cover: must be an object {{start, end}}: the window the graphic covers the whole frame")
            cov = _range(cover, what + ".transition.cover")
            if not (cov["start"].value <= cp.value < cov["end"].value):
                raise EditError("INVALID_REQUEST", f"{what}.transition: cut_point {cp.text(3)}s is not inside the covered window "
                                f"[{cov['start'].text(3)}, {cov['end'].text(3)})s: the switch must happen under full cover",
                                {"reason": "stinger_not_covering"})
            p["transition"] = {"type": "stinger", "stinger": tr["stinger"], "cut_point": cp, "cover": cov}
            tr = None
        if tr is not None:
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
        _keys(params, what, ("factor", "smooth"), ("factor",))
        f = parse_fraction(params["factor"], what + ".factor")
        if not (MIN_SPEED <= f <= MAX_SPEED):
            raise EditError("INVALID_REQUEST", f"{what}.factor: must be between {fraction_text(MIN_SPEED)} and {fraction_text(MAX_SPEED)}")
        if f == 1:
            raise EditError("INVALID_REQUEST", f"{what}.factor: 1 changes nothing; drop the operation")
        p["factor"] = f
        if "smooth" in params:
            p["smooth"] = _enum(params["smooth"], what + ".smooth", SMOOTH_MODES)
    elif op_type in ("FIT", "FILL"):
        _keys(params, what, ("aspect", "width", "fps") + (("pad_color",) if op_type == "FIT" else ("anchor",)), ("aspect",))
        p["aspect"] = _aspect(params["aspect"], what + ".aspect")
        if "width" in params:
            p["width"] = _even(params["width"], what + ".width")
        if op_type == "FIT":
            p["pad_color"] = _color(params.get("pad_color", "black"), what + ".pad_color")
        elif "anchor" in params:
            a = params["anchor"]
            if not isinstance(a, dict):
                raise EditError("INVALID_REQUEST", f"{what}.anchor: must be an object with x and y")
            _keys(a, what + ".anchor", ("x", "y"), ("x", "y"))
            p["anchor"] = {"x": _unit_float(a["x"], what + ".anchor.x"), "y": _unit_float(a["y"], what + ".anchor.y")}
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
    elif op_type == "ROTATE":
        _keys(params, what, ("degrees", "flip"))
        if "degrees" in params:
            p["degrees"] = _int_choice(params["degrees"], what + ".degrees", ROTATIONS)
        if "flip" in params:
            p["flip"] = _enum(params["flip"], what + ".flip", FLIPS)
        if "degrees" not in p and "flip" not in p:
            raise EditError("INVALID_REQUEST", f"{what}: give degrees and/or flip; nothing to do")
    elif op_type == "FREEZE":
        _keys(params, what, ("hold", "at"), ("hold",))
        hold = Time.parse(params["hold"], what + ".hold")
        if not (MIN_HOLD <= hold.value <= MAX_HOLD):
            raise EditError("INVALID_TIME_RANGE", f"{what}.hold: must be between {float(MIN_HOLD):g} and {float(MAX_HOLD):g} seconds")
        p["hold"] = hold
        if "at" in params:
            p["at"] = Time.parse(params["at"], what + ".at")
    elif op_type == "REVERSE":
        _keys(params, what, ())
    elif op_type == "CROP":
        # crop.py's own rules: x, y >= 0, width / height even (4:2:0); inside the frame is checked once the input frame is
        # known (Executor._check_media), before anything runs. Never rounded: a given rectangle is kept exactly or refused.
        _keys(params, what, ("x", "y", "width", "height"), ("x", "y", "width", "height"))
        p["x"] = _int(params["x"], what + ".x", 0, MAX_DIMENSION)
        p["y"] = _int(params["y"], what + ".y", 0, MAX_DIMENSION)
        p["width"] = _even(params["width"], what + ".width")
        p["height"] = _even(params["height"], what + ".height")
    elif op_type == "CONDENSE":
        _keys(params, what, ("segments", "rules", "default", "output"), ("segments", "rules"))
        segs = params["segments"]
        if not isinstance(segs, list) or not segs or len(segs) > 5000:
            raise EditError("INVALID_REQUEST", f"{what}.segments: must be a non-empty list (max 5000) of {{start, end, label}}")
        out_segs: List[Dict[str, Any]] = []
        for i, sgm in enumerate(segs):
            if not isinstance(sgm, dict):
                raise EditError("INVALID_REQUEST", f"{what}.segments[{i}]: must be an object")
            _keys(sgm, f"{what}.segments[{i}]", ("start", "end", "label"), ("start", "end", "label"))
            r = _range({"start": sgm["start"], "end": sgm["end"]}, f"{what}.segments[{i}]")
            lab = sgm["label"]
            if not isinstance(lab, str) or not _LABEL.match(lab):
                raise EditError("INVALID_REQUEST", f"{what}.segments[{i}].label: must match {_LABEL.pattern}")
            out_segs.append({"start": r["start"], "end": r["end"], "label": lab})
        out_segs.sort(key=lambda x: x["start"].value)
        for a, b in zip(out_segs, out_segs[1:]):
            if b["start"].value < a["end"].value:
                raise EditError("INVALID_TIME_RANGE", f"{what}.segments: {a['label']} [{a['start'].text(3)}, {a['end'].text(3)}) overlaps "
                                f"{b['label']} [{b['start'].text(3)}, {b['end'].text(3)})", {"reason": "overlap"})
        p["segments"] = out_segs
        rules = params["rules"]
        if not isinstance(rules, dict) or not rules:
            raise EditError("INVALID_REQUEST", f"{what}.rules: must map each label to cut | keep | {{speed: factor}}")
        p["rules"] = {}
        for lab, rule in sorted(rules.items(), key=lambda kv: str(kv[0])):
            if not isinstance(lab, str) or not _LABEL.match(lab):
                raise EditError("INVALID_REQUEST", f"{what}.rules: label {lab!r} must match {_LABEL.pattern}")
            p["rules"][lab] = _span_rule(rule, f"{what}.rules.{lab}")
        missing = sorted({x["label"] for x in out_segs} - set(p["rules"]))
        if missing:
            raise EditError("INVALID_REQUEST", f"{what}.rules: no rule for labels {missing}", {"reason": "missing_rule", "labels": missing})
        p["default"] = _span_rule(params.get("default", "keep"), what + ".default")
        # output removed (ADR-027): a review file of only the spans the rules cut, joined in order, to check before cutting
        p["output"] = _enum(params.get("output", "result"), what + ".output", ("result", "removed"))
    elif op_type == "PUNCH_IN":
        # the subject box is the caller's (detection-skill's face box, ADR-001: no detection here); the window is computed
        # once the input frame is measured (punch_window) and refused there if it cannot hold the box
        _keys(params, what, ("start", "end", "scale", "subject"), ("start", "end", "scale", "subject"))
        p.update(_range({"start": params["start"], "end": params["end"]}, what))
        sc = parse_fraction(params["scale"], what + ".scale")
        if not (1 < sc <= MAX_PUNCH):
            raise EditError("INVALID_REQUEST", f"{what}.scale: must be above 1 and at most {fraction_text(MAX_PUNCH)}")
        p["scale"] = sc
        b = params["subject"]
        if not isinstance(b, dict):
            raise EditError("INVALID_REQUEST", f"{what}.subject: must be an object {{x, y, width, height}}")
        _keys(b, what + ".subject", ("x", "y", "width", "height"), ("x", "y", "width", "height"))
        p["subject"] = {"x": _int(b["x"], what + ".subject.x", 0, MAX_DIMENSION), "y": _int(b["y"], what + ".subject.y", 0, MAX_DIMENSION),
                        "width": _int(b["width"], what + ".subject.width", 1, MAX_DIMENSION), "height": _int(b["height"], what + ".subject.height", 1, MAX_DIMENSION)}
    elif op_type == "IMAGE_INSERT":
        _keys(params, what, ("duration", "width", "height", "fps"), ("duration",))
        dur = Time.parse(params["duration"], what + ".duration")
        if not (MIN_STILL <= dur.value <= MAX_STILL):
            raise EditError("INVALID_TIME_RANGE", f"{what}.duration: must be between {float(MIN_STILL):g} and {float(MAX_STILL):g} seconds")
        p["duration"] = dur
        for k in ("width", "height"):
            if k in params:
                p[k] = _even(params[k], f"{what}.{k}")
        _frame(params, what, p)
        p.setdefault("fps", STILL_FPS_DEFAULT)
    return p


_LABEL = re.compile(r"^[a-z0-9][a-z0-9_+.-]{0,39}$")


def _span_rule(rule: Any, what: str) -> Dict[str, Any]:
    """CONDENSE rule: "cut" | "keep" | {"speed": factor in [1/4, 4], not 1} -> {"action", "factor"?}."""
    if rule in ("cut", "keep"):
        return {"action": rule}
    if isinstance(rule, dict) and set(rule) == {"speed"}:
        f = parse_fraction(rule["speed"], what + ".speed")
        if not (MIN_SPEED <= f <= MAX_SPEED) or f == 1:
            raise EditError("INVALID_REQUEST", f"{what}.speed: must be between {fraction_text(MIN_SPEED)} and {fraction_text(MAX_SPEED)}, not 1")
        return {"action": "speed", "factor": f}
    raise EditError("INVALID_REQUEST", f"{what}: must be cut | keep | {{speed: factor}}")


def condense_played(params: Dict[str, Any], pieces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The pieces CONDENSE writes, in order: the kept and sped ones (output result), or only the cut ones at speed 1 (output removed)."""
    if params.get("output") == "removed":
        return [dict(pc, action="keep", reviewed=pc["action"]) for pc in pieces if pc["action"] == "cut"]
    return [pc for pc in pieces if pc["action"] != "cut"]


def condense_pieces(params: Dict[str, Any], duration: Time) -> List[Dict[str, Any]]:
    """CONDENSE's plan over [0, duration): every labelled span and every gap between them (rule: params.default), each with its
    rule, in order; neighbours with the same rule (and factor) are merged. Spans past the end are refused by the executor."""
    pieces: List[Dict[str, Any]] = []
    t = Time(Fraction(0))
    for s in params["segments"]:
        if t.value < s["start"].value:
            pieces.append({"start": t, "end": s["start"], "label": None, **params["default"]})
        end = s["end"] if s["end"].value <= duration.value else duration
        if s["start"].value < end.value:
            pieces.append({"start": s["start"], "end": end, "label": s["label"], **params["rules"][s["label"]]})
        t = end
    if t.value < duration.value:
        pieces.append({"start": t, "end": duration, "label": None, **params["default"]})
    merged: List[Dict[str, Any]] = []
    for pc in pieces:
        if merged and merged[-1]["action"] == pc["action"] and merged[-1].get("factor") == pc.get("factor") and merged[-1]["end"].value == pc["start"].value:
            merged[-1] = dict(merged[-1], end=pc["end"], label=merged[-1]["label"] if merged[-1]["label"] == pc["label"] else "+".join(
                x for x in (merged[-1]["label"], pc["label"]) if x) or None)
            continue
        merged.append(dict(pc))
    return merged


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


def punch_window(frame_w: int, frame_h: int, scale: Fraction, subject: Dict[str, int]) -> Dict[str, int]:
    """PUNCH_IN's crop window in the input frame (as displayed): floor_even(W / scale) x floor_even(H / scale), centred on the
    subject box, then moved only as far as needed to stay inside the frame; offsets even where the box allows. Raises when
    the box is outside the frame or larger than the window (it could not stay in view)."""
    x, y, bw, bh = subject["x"], subject["y"], subject["width"], subject["height"]
    if x + bw > frame_w or y + bh > frame_h:
        raise EditError("INVALID_REQUEST", f"PUNCH_IN: subject box ({x},{y},{bw}x{bh}) is not inside the input frame {frame_w}x{frame_h}",
                        {"reason": "subject_outside_frame", "frame": [frame_w, frame_h], "subject": dict(subject)})
    w = int(Fraction(frame_w) / scale) // 2 * 2
    h = int(Fraction(frame_h) / scale) // 2 * 2
    if bw > w or bh > h:
        raise EditError("INVALID_REQUEST", f"PUNCH_IN: subject box {bw}x{bh} does not fit the {w}x{h} window a {fraction_text(scale)}x zoom leaves; "
                        "use a smaller scale", {"reason": "subject_larger_than_window", "window": [w, h], "subject": dict(subject)})

    def place(pos: int, size: int, win: int, limit: int) -> int:
        lo, hi = max(0, pos + size - win), min(pos, limit - win)   # every offset that keeps the box in the window and the window in the frame
        want = min(max(int(round(pos + Fraction(size, 2) - Fraction(win, 2))), lo), hi)
        for cand in (want - want % 2, want + want % 2):
            if lo <= cand <= hi:
                return cand
        return want
    return {"x": place(x, bw, w, frame_w), "y": place(y, bh, h, frame_h), "width": w, "height": h}


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


def unsupported_list() -> List[Dict[str, Any]]:
    return [{"type": t, "capability": u["capability"], "status": "NOT_IMPLEMENTED", "reason": u["reason"], "engine_tool": u["engine_tool"]}
            for t, u in sorted(UNSUPPORTED.items()) if t not in ("REORDER", "TRANSITION")]
