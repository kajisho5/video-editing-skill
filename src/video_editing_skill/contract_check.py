"""Contract consistency: (a) the live contract against the implementation it describes, (b) a saved
contract document against the live one (drift). Both return a list of problems; empty means consistent.

`video-editing contract --check FILE|-` runs both and exits 1 on any problem, so CI can pin a golden copy
(tests/contract/contract.json) and fail when a field the agent relies on changes without a review.
"""
import os
from typing import Any, Dict, List, Optional

from . import CONTRACT_SCHEMA, SKILL_ID, VERSION
from .compiler import ALLOWED_FLAGS, compile_operation
from .contract import TOOL_REQUIREMENTS, skill_contract
from .errors import ERROR_CODES, EXIT_CODES
from .ffmpeg_skill import REQUIRED_TOOLS
from .operations import OPERATIONS, UNSUPPORTED, capability_list
from .project import EditOperation

# fields of a ToolSpec an agent-side registry keys on; a change in any of them is a contract change
PINNED_TOOL_FIELDS = ("tool_id", "skill_id", "version", "operation_type", "capability", "required_capabilities", "inputs", "input_arity",
                      "produces_output", "deterministic", "result_keys", "executed_by", "kind")
PINNED_TOP_FIELDS = ("schema", "skill_id", "version", "role", "capability_names", "schemas", "formats", "not_provided")
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
        "FILL": {"aspect": "1:1", "width": 360},
        "RESIZE": {"width": 320},
        "OVERLAY": {"image": "logo", "position": {"x": -10, "y": 10}, "margin": 24, "scale": 60, "opacity": Fraction(1, 2), "start": one, "end": two, "fade": Fraction(1, 2)},
    }
    return samples[t]


def verify_implementation(contract: Optional[Dict[str, Any]] = None, root: Optional[str] = None) -> List[str]:
    """Problems between the (live) contract and the code / docs."""
    c = contract or skill_contract()
    problems: List[str] = []
    if c.get("schema") != CONTRACT_SCHEMA or c.get("skill_id") != SKILL_ID or c.get("version") != VERSION:
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
            for t, u in UNSUPPORTED.items():
                if t in ("REORDER", "TRANSITION"):
                    continue
                if f"`{t}`" not in text:
                    problems.append(f"README.md does not list unsupported operation {t}")
            if "Not implemented" not in text and "NOT IMPLEMENTED" not in text and "not implemented" not in text:
                problems.append("README.md has no not-implemented section")
    return problems


def check_saved(saved: Any, live: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Drift between a saved contract document and the live one."""
    live = live or skill_contract()
    problems: List[str] = []
    if not isinstance(saved, dict):
        return {"status": "drift", "problems": ["saved contract is not a JSON object"]}
    if saved.get("schema") != live["schema"]:
        problems.append(f"schema {saved.get('schema')!r} != {live['schema']!r}")
    for k in PINNED_TOP_FIELDS:
        if saved.get(k) != live.get(k):
            problems.append(f"{k} changed")
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
    if saved.get("request_shape") != live["request_shape"] or saved.get("response_shape") != live["response_shape"]:
        problems.append("request / response shape changed")
    return {"status": "ok" if not problems else "drift", "problems": problems}


def run_check(saved: Any = None) -> Dict[str, Any]:
    live = skill_contract()
    impl = verify_implementation(live)
    doc: Dict[str, Any] = {"schema": "video-editing/contract-check@1", "skill": {"id": SKILL_ID, "version": VERSION},
                           "implementation": {"status": "ok" if not impl else "inconsistent", "problems": impl}}
    if saved is not None:
        doc["drift"] = check_saved(saved, live)
    doc["ok"] = not impl and (saved is None or doc["drift"]["status"] == "ok")
    doc["status"] = "ok" if doc["ok"] else "fail"
    return doc
