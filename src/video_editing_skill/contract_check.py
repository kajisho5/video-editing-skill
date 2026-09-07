"""Contract consistency: (a) the live contract against the implementation it describes, (b) a saved
contract document against the live one (drift). Both return a list of problems; empty means consistent.

`video-editing contract --check FILE|-` runs both and exits 1 on any problem, so CI can pin a golden copy
(tests/contract/contract.json) and fail when a field the agent relies on changes without a review.
"""
import os
from typing import Any, Dict, List, Optional

from . import CONTRACT_SCHEMA, CONTRACT_VERSION, SKILL_ID, VERSION
from .compiler import ALLOWED_FLAGS, compile_operation
from .contract import PINNED_BLOCKS, TOOL_REQUIREMENTS, ffmpeg_skill_version_range, skill_contract
from .errors import ERROR_CODES, EXIT_CODES
from .ffmpeg_skill import REQUIRED_TOOLS
from .operations import CRF_MAX, CRF_MIN, ENCODING, FRAME_SEMANTICS, MEDIA, MEDIA_POLICY, OPERATIONS, UNSUPPORTED, X264_PRESETS, capability_list, media_compatibility, validate_encoding
from .project import EditOperation

# fields of a ToolSpec an agent-side registry keys on; a change in any of them is a contract change
PINNED_TOOL_FIELDS = ("tool_id", "skill_id", "version", "operation_type", "capability", "required_capabilities", "inputs", "input_arity",
                      "produces_output", "deterministic", "result_keys", "executed_by", "kind")
PINNED_TOP_FIELDS = ("schema", "skill_id", "contract_version", "role", "capability_names", "schemas", "formats", "not_provided")
DOCS = ("README.md", "SKILL.md")


def _sample_params(t: str) -> Dict[str, Any]:  # noqa: C901
    """One valid parameter set per type, used to compile every operation and check its flags."""
    from .timebase import Time
    from fractions import Fraction
    one, two = Time.parse(1), Time.parse(2)
    samples: Dict[str, Dict[str, Any]] = {
        "TRIM": {"start": one, "end": two, "precision": "frame"},
        "CUT": {"keep": [{"start": one, "end": two}], "precision": "keyframe"},
        "CONCAT": {"transition": {"type": "fade", "duration": one}, "width": 640, "height": 360, "fps": Fraction(30), "mode": "pad", "pad_color": "black"},
        "SPEED": {"factor": Fraction(2)},
        "FIT": {"aspect": "16:9", "width": 640, "pad_color": "black", "fps": Fraction(30)},
        "FILL": {"aspect": "1:1", "width": 360, "anchor": {"x": 0.25, "y": 0.5}},
        "RESIZE": {"width": 320},
        "OVERLAY": {"image": "logo", "position": {"x": -10, "y": 10}, "margin": 24, "scale": 60, "opacity": Fraction(1, 2), "start": one, "end": two, "fade": Fraction(1, 2)},
    }
    return samples[t]


def verify_implementation(contract: Optional[Dict[str, Any]] = None, root: Optional[str] = None) -> List[str]:
    """Problems between the (live) contract and the code / docs."""
    c = contract or skill_contract()
    problems: List[str] = []
    if (c.get("schema") != CONTRACT_SCHEMA or c.get("skill_id") != SKILL_ID or c.get("version") != VERSION
            or c.get("contract_version") != CONTRACT_VERSION):
        problems.append("contract header does not match the package")
    ops_in_contract = set(c.get("operations", {}))
    if ops_in_contract != set(OPERATIONS):
        problems.append(f"contract.operations {sorted(ops_in_contract)} != allowlist {sorted(OPERATIONS)}")
    tools = {t["tool_id"]: t for t in c.get("tools", [])}
    for t, spec in OPERATIONS.items():
        tid = f"{SKILL_ID}/{t.lower()}"
        ts = tools.get(tid)
        if ts is None:
            problems.append(f"no ToolSpec for {t}")
            continue
        if ts["executed_by"] != spec["tool"] or ts["capability"] != spec["capability"] or ts["input_arity"] != spec["arity"]:
            problems.append(f"ToolSpec {tid} disagrees with operations.OPERATIONS[{t}]")
        if ts["required_capabilities"] != TOOL_REQUIREMENTS[spec["tool"]]:
            problems.append(f"ToolSpec {tid} required_capabilities differ from TOOL_REQUIREMENTS")
        if not ts["produces_output"] or not ts["deterministic"] or ts["kind"] != "transform":
            problems.append(f"ToolSpec {tid}: editing tools are deterministic transforms that produce output")
        script = spec["tool"].split("/", 1)[1]
        if script not in ALLOWED_FLAGS or script not in REQUIRED_TOOLS:
            problems.append(f"{t}: engine tool {spec['tool']} is not in ALLOWED_FLAGS / REQUIRED_TOOLS")
        # compile with a sample and prove every flag is allowlisted for the tool
        op = EditOperation("x", t, ["a", "b", "logo"] if t == "CONCAT" else (["a", "logo"] if t == "OVERLAY" else ["a"]), _sample_params(t))
        step = compile_operation(op)
        if step.tool != spec["tool"]:
            problems.append(f"{t}: compiler targets {step.tool}, contract says {spec['tool']}")
        try:
            from .timebase import Time
            step.argv_for(["/in/a.mp4", "/in/b.mp4"] if t == "CONCAT" else ["/in/a.mp4"], "/ws/o.mp4", "/in/logo.png" if t == "OVERLAY" else None, Time.parse(10))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{t}: compiled flags are not all allowlisted: {exc}")
    for tid in tools:
        if tid.split("/", 1)[1].upper() not in OPERATIONS:
            problems.append(f"ToolSpec {tid} has no operation")
    declared = {x["capability"] for x in c.get("capabilities", [])}
    if declared != {x["capability"] for x in capability_list()}:
        problems.append("contract.capabilities differ from operations.capability_list()")
    for u in c.get("unsupported", []):
        if u["capability"] in declared:
            problems.append(f"{u['capability']} is both supported and unsupported")
        if u["type"] in OPERATIONS or u["type"] not in UNSUPPORTED:
            problems.append(f"unsupported entry {u['type']} is not in operations.UNSUPPORTED")
    errs = c.get("errors", {})
    if list(errs.get("codes", [])) != list(ERROR_CODES) or errs.get("exit_codes") != EXIT_CODES:
        problems.append("contract.errors differ from errors.py")
    ex = c.get("execution", {})
    for k in ("shell", "arbitrary_executables", "raw_ffmpeg_arguments", "filter_strings", "network", "ai", "input_mutation"):
        if ex.get(k) is not False:
            problems.append(f"execution.{k} must be false")
    # media compatibility: one entry per operation, every operation needs video, and the contract mirrors the table
    if set(MEDIA) != set(OPERATIONS):
        problems.append(f"operations.MEDIA {sorted(MEDIA)} != allowlist {sorted(OPERATIONS)}")
    for t, m in MEDIA.items():
        if set(m) != {"inputs", "requires", "output", "refused_before_execution"} or set(m["requires"]) != {"video", "audio", "image"} or m["requires"]["video"] is not True:
            problems.append(f"MEDIA[{t}] is malformed")
        if m["requires"]["image"] != (t == "OVERLAY"):
            problems.append(f"MEDIA[{t}].requires.image disagrees with the operation's inputs")
    if c.get("media_compatibility") != media_compatibility():
        problems.append("contract.media_compatibility differs from operations.MEDIA")
    for t in OPERATIONS:
        ts = tools.get(f"{SKILL_ID}/{t.lower()}") or {}
        if ts.get("media", {}).get("requires") != MEDIA[t]["requires"]:
            problems.append(f"ToolSpec {t}: media.requires differs from operations.MEDIA")
    ver = c.get("versioning", {})
    if (tuple(ver.get("pinned_blocks", [])) != PINNED_BLOCKS or ver.get("version") != VERSION
            or ver.get("contract_version") != CONTRACT_VERSION):
        problems.append("contract.versioning does not name the pinned blocks / version / contract_version")
    # dependencies: must name exactly the range version_supported() already enforces at runtime - never an exact pin
    if c.get("dependencies") != [{"skill_id": "ffmpeg-skill", "version_range": ffmpeg_skill_version_range()}]:
        problems.append("contract.dependencies does not match the ffmpeg-skill version range this Skill enforces")
    # encoding profile: the contract's parameter list is exactly what validate_encoding accepts, every flag is allowlisted for every re-encoding tool
    enc = c.get("encoding") or {}
    if enc != ENCODING or set(enc.get("parameters", {})) != {"crf", "preset"}:
        problems.append("contract.encoding differs from operations.ENCODING")
    try:
        validate_encoding({"crf": CRF_MIN, "preset": X264_PRESETS[0]}, "x")
        validate_encoding({"crf": CRF_MAX, "preset": X264_PRESETS[-1]}, "x")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"encoding vocabulary is not accepted by validate_encoding: {exc}")
    for bad in ({"crf": CRF_MIN - 1}, {"crf": CRF_MAX + 1}, {"preset": "placebo"}, {"bitrate": "5M"}, {"codec": "libx265"}):
        try:
            validate_encoding(bad, "x")
            problems.append(f"validate_encoding accepted {bad}")
        except Exception:  # noqa: BLE001
            pass
    for script in ("cut", "join", "fit", "overlay"):
        if not {"crf", "preset"} <= set(ALLOWED_FLAGS[script]):
            problems.append(f"encoding flags are not allowlisted for ffmpeg-skill/{script}")
    if c.get("frame_semantics") != FRAME_SEMANTICS or set(FRAME_SEMANTICS) != {"RESIZE", "FIT", "FILL", "rules"}:
        problems.append("contract.frame_semantics differs from operations.FRAME_SEMANTICS")
    if c.get("media_policy") != MEDIA_POLICY or set(MEDIA_POLICY) != {"refused_before_execution", "normalized_by_skill", "delegated_to_engine", "by_stream"}:
        problems.append("contract.media_policy differs from operations.MEDIA_POLICY")
    if "0.3.0" not in (ver.get("next") or {}):
        problems.append("contract.versioning.next does not describe 0.3.0")
    problems += verify_docs(root)
    return problems


def verify_docs(root: Optional[str] = None) -> List[str]:
    """README / SKILL.md must name every supported type and every unsupported type, and nothing else as an operation."""
    root = root or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    problems: List[str] = []
    for name in DOCS:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue  # an installed wheel has no docs; only a checkout is checked
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for t in OPERATIONS:
            if f"`{t}`" not in text and f"| {t}" not in text and f" {t} " not in text and f"{t}," not in text and f"{t} " not in text:
                problems.append(f"{name} does not mention supported operation {t}")
        if name == "README.md":
            for t in UNSUPPORTED:
                if t in ("REORDER", "TRANSITION"):
                    continue
                if f"`{t}`" not in text:
                    problems.append(f"README.md does not list unsupported operation {t}")
            if "Not implemented" not in text and "NOT IMPLEMENTED" not in text and "not implemented" not in text:
                problems.append("README.md has no not-implemented section")
    return problems


def check_saved(saved: Any, live: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Drift between a saved contract document and the live one. Every difference is classified: `breaking` when it touches
    a pinned block or a pinned ToolSpec field (agents compare those verbatim and must re-pin after a version bump),
    `additive` when it only adds keys outside them. Any difference is drift (the golden copy is regenerated deliberately)."""
    live = live or skill_contract()
    problems: List[str] = []
    additions: List[str] = []
    if not isinstance(saved, dict):
        return {"status": "drift", "compatibility": "breaking", "problems": ["saved contract is not a JSON object"], "additions": []}
    if saved.get("schema") != live["schema"]:
        problems.append(f"schema {saved.get('schema')!r} != {live['schema']!r}")
    for k in PINNED_BLOCKS:
        if saved.get(k) != live.get(k) and k not in ("schema",):
            problems.append(f"pinned block {k} changed")
    for k in PINNED_TOP_FIELDS:
        if saved.get(k) != live.get(k) and k not in PINNED_BLOCKS:
            problems.append(f"{k} changed")
    for k in sorted(set(live) - set(saved)):
        additions.append(f"top-level key added: {k}")
    for k in sorted(set(saved) - set(live)):
        problems.append(f"top-level key removed: {k}")
    for k in sorted(set(live) & set(saved)):
        if k in PINNED_BLOCKS or k in PINNED_TOP_FIELDS or k in ("tools", "operations", "unsupported", "capabilities", "errors", "execution", "request_shape", "response_shape"):
            continue
        if saved[k] != live[k]:
            additions.append(f"non-pinned block changed: {k}")
    s_tools = {str(t.get("tool_id")): t for t in saved.get("tools", []) if isinstance(t, dict)}
    l_tools = {t["tool_id"]: t for t in live["tools"]}
    for tid in sorted(set(s_tools) | set(l_tools)):
        if tid not in s_tools:
            problems.append(f"tool added: {tid}")
        elif tid not in l_tools:
            problems.append(f"tool removed: {tid}")
        else:
            for f in PINNED_TOOL_FIELDS:
                if s_tools[tid].get(f) != l_tools[tid].get(f):
                    problems.append(f"{tid}.{f} changed")
            for f in sorted(set(l_tools[tid]) - set(s_tools[tid])):
                additions.append(f"{tid}.{f} added")
            for f in sorted(set(s_tools[tid]) - set(l_tools[tid])):
                problems.append(f"{tid}.{f} removed")
            for f in sorted((set(l_tools[tid]) & set(s_tools[tid])) - set(PINNED_TOOL_FIELDS)):
                if s_tools[tid][f] != l_tools[tid][f]:
                    additions.append(f"{tid}.{f} changed (not pinned)")
    if saved.get("operations") != live["operations"]:
        problems.append("operations (types / parameters) changed")
    if saved.get("unsupported") != live["unsupported"]:
        problems.append("unsupported list changed")
    if saved.get("capabilities") != live["capabilities"]:
        problems.append("capabilities changed")
    if saved.get("errors") != live["errors"]:
        problems.append("errors (codes / exit codes / retryable) changed")
    if saved.get("execution") != live["execution"]:
        problems.append("execution block changed")
    if saved.get("request_shape") != live["request_shape"]:
        problems.append("request shape changed")
    if saved.get("response_shape") != live["response_shape"]:
        additions.append("response shape changed (additive fields only are allowed within a version)")
    status = "ok" if not problems and not additions else "drift"
    compatibility = "breaking" if problems else ("additive" if additions else "none")
    return {"status": status, "compatibility": compatibility, "problems": problems, "additions": additions}


def run_check(saved: Any = None) -> Dict[str, Any]:
    live = skill_contract()
    impl = verify_implementation(live)
    doc: Dict[str, Any] = {"schema": "video-editing/contract-check@1", "skill": {"id": SKILL_ID, "version": VERSION},
                           "implementation": {"status": "ok" if not impl else "inconsistent", "problems": impl}}
    if saved is not None:
        doc["drift"] = check_saved(saved, live)
        doc["drift"]["problems"] = [f"[breaking] {p}" for p in doc["drift"]["problems"]] + [f"[additive] {a}" for a in doc["drift"]["additions"]]
    doc["ok"] = not impl and (saved is None or doc["drift"]["status"] == "ok")
    doc["status"] = "ok" if doc["ok"] else "fail"
    return doc
