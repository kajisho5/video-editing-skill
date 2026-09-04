"""Compiler: EditOperation -> one typed ffmpeg-skill call.

The output of compilation is a Step: the tool id and a dict of typed arguments that map 1:1 onto the
tool's argparse flags (ffmpeg-skill contract: `key -> --key`, `output -> -o`, positionals first).
argv is only materialised at execution time, from resolved paths, by `argv_for`. No step can carry a
flag that is not listed in ALLOWED_FLAGS for its tool.
"""
import os
from fractions import Fraction
from typing import Any, Dict, List, Optional

from .errors import EditError
from .project import EditOperation, EditProject
from .timebase import Time, fraction_text

ALLOWED_FLAGS: Dict[str, tuple] = {
    "cut": ("start", "end", "segments", "accurate"),
    "join": ("transition", "duration", "width", "height", "fps", "fit", "pad_color"),
    "fit": ("duration", "method", "max_speed", "aspect", "fit", "width", "pad_color", "fps"),
    "overlay": ("image", "position", "margin", "scale", "opacity", "start", "end", "fade"),
    "probe": (),
}


def _fps_text(f: Fraction) -> str:
    return str(f.numerator) if f.denominator == 1 else f"{float(f):.6f}".rstrip("0").rstrip(".")


class Step:
    def __init__(self, op: EditOperation, tool: str, args: Dict[str, Any], positional_inputs: List[str], image_input: Optional[str] = None,
                 needs_input_duration: bool = False):
        self.op = op
        self.tool = tool                      # ffmpeg-skill/<name>
        self.args = args                      # typed flags (no paths)
        self.positional_inputs = positional_inputs
        self.image_input = image_input
        self.needs_input_duration = needs_input_duration

    @property
    def script(self) -> str:
        return self.tool.split("/", 1)[1]

    def to_dict(self) -> Dict[str, Any]:
        return {"operation": self.op.ref, "operation_id": self.op.operation_id, "type": self.op.type, "tool": self.tool,
                "inputs": list(self.positional_inputs) + ([self.image_input] if self.image_input else []),
                "arguments": dict(self.args),
                "duration_from_input": self.needs_input_duration}

    def argv_for(self, inputs: List[str], output: str, image: Optional[str] = None, input_duration: Optional[Time] = None) -> List[str]:
        """Materialise argv from resolved paths. Paths are positional / -o / --image only."""
        allowed = ALLOWED_FLAGS[self.script]
        argv: List[str] = list(inputs) + ["-o", output]
        args = dict(self.args)
        if self.needs_input_duration:
            if input_duration is None:
                raise EditError("INTERNAL_ERROR", "SPEED needs the input duration")
            factor = Fraction(args.pop("_factor"))
            args["duration"] = input_duration.scale(1 / factor).tool_arg()
        if image is not None:
            args["image"] = image
        for key, val in args.items():
            if key not in allowed:
                raise EditError("INTERNAL_ERROR", f"flag {key!r} is not allowed for {self.tool}")
            flag = "--" + key.replace("_", "-")
            if val is True:
                argv.append(flag)
            elif val is False or val is None:
                continue
            elif str(val).startswith("-"):
                argv.append(f"{flag}={val}")  # a negative value must not look like an option to argparse
            else:
                argv += [flag, str(val)]
        return argv


def compile_operation(op: EditOperation) -> Step:
    p = op.params
    video_inputs = op.inputs[:-1] if op.type == "OVERLAY" else op.inputs
    if op.type == "TRIM":
        args: Dict[str, Any] = {"start": p["start"].tool_arg(), "end": p["end"].tool_arg()}
        if p["precision"] == "frame":
            args["accurate"] = True
        return Step(op, "ffmpeg-skill/cut", args, video_inputs)
    if op.type == "CUT":
        segs = ",".join(f"{r['start'].tool_arg()}-{r['end'].tool_arg()}" for r in p["keep"])
        args = {"segments": segs}
        if p["precision"] == "frame":
            args["accurate"] = True
        return Step(op, "ffmpeg-skill/cut", args, video_inputs)
    if op.type == "CONCAT":
        tr = p.get("transition")
        args = {"transition": tr["type"] if tr else "none", "duration": tr["duration"].tool_arg() if tr else "0",
                "fit": p["mode"], "pad_color": p["pad_color"]}
        for k in ("width", "height"):
            if k in p:
                args[k] = p[k]
        if "fps" in p:
            args["fps"] = _fps_text(p["fps"])
        return Step(op, "ffmpeg-skill/join", args, video_inputs)
    if op.type == "SPEED":
        args = {"_factor": fraction_text(p["factor"]), "method": "speed", "max_speed": "4"}
        return Step(op, "ffmpeg-skill/fit", args, video_inputs, needs_input_duration=True)
    if op.type in ("FIT", "FILL"):
        args = {"aspect": p["aspect"], "fit": "pad" if op.type == "FIT" else "crop"}
        if op.type == "FIT":
            args["pad_color"] = p["pad_color"]
        if "width" in p:
            args["width"] = p["width"]
        if "fps" in p:
            args["fps"] = _fps_text(p["fps"])
        return Step(op, "ffmpeg-skill/fit", args, video_inputs)
    if op.type == "RESIZE":
        args = {"width": p["width"]}
        if "fps" in p:
            args["fps"] = _fps_text(p["fps"])
        return Step(op, "ffmpeg-skill/fit", args, video_inputs)
    if op.type == "OVERLAY":
        pos = p["position"]
        args = {"position": pos if isinstance(pos, str) else f"{pos['x']},{pos['y']}", "margin": p["margin"]}
        if "scale" in p:
            args["scale"] = p["scale"]
        if p["opacity"] != 1:
            args["opacity"] = f"{float(p['opacity']):.3f}"
        for k in ("start", "end"):
            if k in p:
                args[k] = p[k].tool_arg()
        if p.get("fade"):
            args["fade"] = f"{float(p['fade']):.3f}"
        return Step(op, "ffmpeg-skill/overlay", args, video_inputs, image_input=op.inputs[-1])
    raise EditError("UNSUPPORTED_OPERATION", f"no compiler for {op.type}")


def compile_project(project: EditProject) -> Dict[str, Step]:
    return {ref: compile_operation(project.operations[ref]) for ref in project.order}


def output_extension(project: EditProject, op_ref: str) -> str:
    """Container extension the operation must write: that of its outputs (all equal) or .mp4."""
    exts = sorted({os.path.splitext(o.path)[1].lower() for o in project.outputs.values() if o.operation == op_ref})
    if len(exts) > 1:
        raise EditError("DEPENDENCY_ERROR", f"operation {op_ref!r} feeds outputs with different containers {exts}", {"reason": "conflicting_outputs"})
    return exts[0] if exts else ".mp4"
