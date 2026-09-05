"""Typed edit request -> validated EditProject with an operation graph.

Model
  EditSource     a file the request may read (video or image), identified by content hash
  EditOperation  one allowlisted operation with canonical params, its input references and a
                 deterministic operation_id derived from (type, params, input identities)
  EditOutput     a final file, produced by one operation, inside the workspace
  EditProject    sources + operations + outputs, in a validated, topologically ordered graph

Reference names (`id` fields) are labels chosen by the caller; identity is the hash. Two requests that
describe the same edit of the same bytes get the same operation_ids whatever they call things.
"""
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from . import REQUEST_SCHEMA
from .canonical import sha256_file, stable_hash
from .errors import EditError
from .operations import FORBIDDEN_KEYS, OPERATIONS, params_to_json, validate_params
from .paths import IMAGE_EXTENSIONS, OUTPUT_EXTENSIONS, VIDEO_EXTENSIONS, PathPolicy

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MAX_SOURCES = 200
MAX_OPERATIONS = 500
MAX_OUTPUTS = 50


@dataclass
class EditSource:
    ref: str
    raw_path: str
    path: str            # resolved absolute path
    kind: str            # video | image
    sha256: str
    size: int

    @property
    def identity(self) -> str:
        return "sha256:" + self.sha256

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.ref, "kind": self.kind, "path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass
class EditOperation:
    ref: str
    type: str
    inputs: List[str]                    # refs of sources or operations
    params: Dict[str, Any]               # canonical, validated
    operation_id: str = ""
    depends_on: List[str] = field(default_factory=list)   # operation refs only
    source_refs: List[str] = field(default_factory=list)  # every source reachable upstream

    @property
    def tool(self) -> str:
        return OPERATIONS[self.type]["tool"]

    @property
    def capability(self) -> str:
        return OPERATIONS[self.type]["capability"]

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.ref, "operation_id": self.operation_id, "type": self.type, "capability": self.capability, "tool": self.tool,
                "inputs": list(self.inputs), "params": params_to_json(self.params), "depends_on": list(self.depends_on),
                "sources": list(self.source_refs)}


@dataclass
class EditOutput:
    ref: str
    operation: str
    raw_path: str
    path: str

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.ref, "operation": self.operation, "path": self.path}


@dataclass
class EditOptions:
    timeout_seconds: float = 3600.0
    overwrite: bool = False
    reuse: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {"timeout_seconds": self.timeout_seconds, "overwrite": self.overwrite, "reuse": self.reuse}


@dataclass
class EditProject:
    project_id: Optional[str]
    sources: Dict[str, EditSource]
    operations: Dict[str, EditOperation]
    order: List[str]                     # operation refs, topological
    outputs: Dict[str, EditOutput]
    options: EditOptions
    policy: PathPolicy
    warnings: List[str] = field(default_factory=list)

    @property
    def project_hash(self) -> str:
        return stable_hash({"operations": sorted(o.operation_id for o in self.operations.values()),
                            "outputs": sorted((o.ref, self.operations[o.operation].operation_id, os.path.basename(o.path)) for o in self.outputs.values())})

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.project_id, "project_hash": self.project_hash,
                "sources": [self.sources[k].to_dict() for k in sorted(self.sources)],
                "operations": [self.operations[r].to_dict() for r in self.order],
                "outputs": [self.outputs[k].to_dict() for k in sorted(self.outputs)],
                "options": self.options.to_dict()}


# ---------------------------------------------------------------- parsing helpers
def _obj(v: Any, what: str) -> Dict[str, Any]:
    if not isinstance(v, dict):
        raise EditError("INVALID_REQUEST", f"{what}: must be a JSON object")
    return v


def _keys(d: Dict[str, Any], what: str, allowed: tuple, required: tuple = ()) -> None:
    for k in d:
        if not isinstance(k, str):
            raise EditError("INVALID_REQUEST", f"{what}: keys must be strings")
        if k.lower() in FORBIDDEN_KEYS:
            raise EditError("INVALID_REQUEST", f"{what}: key {k!r} is not accepted (this skill takes typed operations, never commands)",
                            {"reason": "forbidden_key"})
    extra = sorted(set(d) - set(allowed))
    if extra:
        raise EditError("INVALID_REQUEST", f"{what}: unknown keys {extra}", {"allowed": list(allowed)})
    missing = sorted(set(required) - set(d))
    if missing:
        raise EditError("INVALID_REQUEST", f"{what}: missing keys {missing}")


def _ref(v: Any, what: str) -> str:
    if not isinstance(v, str) or not _ID.match(v):
        raise EditError("INVALID_REQUEST", f"{what}: id must match {_ID.pattern}")
    return v


def _list(v: Any, what: str, limit: int) -> List[Any]:
    if not isinstance(v, list):
        raise EditError("INVALID_REQUEST", f"{what}: must be a list")
    if len(v) > limit:
        raise EditError("INVALID_REQUEST", f"{what}: at most {limit} entries")
    return v


def parse_options(raw: Any) -> EditOptions:
    if raw is None:
        return EditOptions()
    d = _obj(raw, "options")
    _keys(d, "options", ("timeout_seconds", "overwrite", "reuse"))
    o = EditOptions()
    if "timeout_seconds" in d:
        t = d["timeout_seconds"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not (1 <= t <= 86400):
            raise EditError("INVALID_REQUEST", "options.timeout_seconds: must be a number between 1 and 86400")
        o.timeout_seconds = float(t)
    for k in ("overwrite", "reuse"):
        if k in d:
            if not isinstance(d[k], bool):
                raise EditError("INVALID_REQUEST", f"options.{k}: must be a boolean")
            setattr(o, k, d[k])
    return o


# ---------------------------------------------------------------- main entry
def parse_request(doc: Any, policy: PathPolicy, hash_sources: bool = True) -> EditProject:
    """Validate a request document against the schema, the path policy and the operation allowlist."""
    d = _obj(doc, "request")
    _keys(d, "request", ("schema", "project", "options"), ("schema", "project"))
    if d["schema"] != REQUEST_SCHEMA:
        raise EditError("INVALID_REQUEST", f"request.schema must be {REQUEST_SCHEMA!r}", {"got": d["schema"]})
    options = parse_options(d.get("options"))
    proj = _obj(d["project"], "project")
    _keys(proj, "project", ("id", "sources", "operations", "outputs"), ("sources", "operations", "outputs"))
    project_id = None
    if "id" in proj and proj["id"] is not None:
        project_id = _ref(proj["id"], "project.id")

    warnings: List[str] = []

    # ---- sources
    sources: Dict[str, EditSource] = {}
    for i, raw in enumerate(_list(proj["sources"], "project.sources", MAX_SOURCES)):
        what = f"project.sources[{i}]"
        s = _obj(raw, what)
        _keys(s, what, ("id", "path", "kind"), ("id", "path"))
        ref = _ref(s["id"], what + ".id")
        if ref in sources:
            raise EditError("INVALID_REQUEST", f"{what}: duplicate source id {ref!r}")
        kind = s.get("kind", "video")
        if kind not in ("video", "image"):
            raise EditError("INVALID_REQUEST", f"{what}.kind: must be video or image")
        exts = VIDEO_EXTENSIONS if kind == "video" else IMAGE_EXTENSIONS
        path = policy.resolve_input(s["path"], what + ".path", exts)
        digest = sha256_file(path) if hash_sources else ""
        sources[ref] = EditSource(ref, s["path"], path, kind, digest, os.path.getsize(path))
    if not sources:
        raise EditError("INVALID_REQUEST", "project.sources: at least one source is required")

    # ---- operations (syntax)
    ops: Dict[str, EditOperation] = {}
    for i, raw in enumerate(_list(proj["operations"], "project.operations", MAX_OPERATIONS)):
        what = f"project.operations[{i}]"
        o = _obj(raw, what)
        _keys(o, what, ("id", "type", "input", "inputs", "params"), ("id", "type"))
        ref = _ref(o["id"], what + ".id")
        if ref in ops:
            raise EditError("INVALID_REQUEST", f"{what}: duplicate operation id {ref!r}")
        if ref in sources:
            raise EditError("INVALID_REQUEST", f"{what}: id {ref!r} is already a source id")
        t = o["type"]
        if not isinstance(t, str):
            raise EditError("INVALID_REQUEST", f"{what}.type: must be a string")
        params = validate_params(t, o.get("params"), what)
        arity = OPERATIONS[t]["arity"]
        if arity == "one":
            if "inputs" in o or "input" not in o:
                raise EditError("INVALID_REQUEST", f"{what}: {t} takes exactly one 'input'")
            inputs = [_ref(o["input"], what + ".input")]
        else:
            if "input" in o or "inputs" not in o:
                raise EditError("INVALID_REQUEST", f"{what}: {t} takes an 'inputs' list")
            inputs = [_ref(x, f"{what}.inputs[{j}]") for j, x in enumerate(_list(o["inputs"], what + ".inputs", 100))]
            if len(inputs) < 2:
                raise EditError("INVALID_REQUEST", f"{what}: {t} needs at least two inputs")
        if t == "OVERLAY":
            inputs.append(params["image"])
        ops[ref] = EditOperation(ref, t, inputs, params)
    if not ops:
        raise EditError("INVALID_REQUEST", "project.operations: at least one operation is required")

    # ---- references, cycles, order
    for op in ops.values():
        for j, r in enumerate(op.inputs):
            is_image_slot = op.type == "OVERLAY" and j == len(op.inputs) - 1
            if r in sources:
                want = "image" if is_image_slot else "video"
                if sources[r].kind != want:
                    raise EditError("DEPENDENCY_ERROR", f"operation {op.ref!r}: input {r!r} must be a {want} source", {"reason": "kind_mismatch"})
            elif r in ops:
                if is_image_slot:
                    raise EditError("DEPENDENCY_ERROR", f"operation {op.ref!r}: params.image must reference an image source, not an operation")
                if r == op.ref:
                    raise EditError("DEPENDENCY_ERROR", f"operation {op.ref!r} depends on itself", {"reason": "cycle"})
            else:
                raise EditError("DEPENDENCY_ERROR", f"operation {op.ref!r}: unknown input {r!r}", {"reason": "unknown_reference"})
        op.depends_on = [r for r in op.inputs if r in ops]
    order = _toposort(ops)
    for ref in order:
        op = ops[ref]
        reach: List[str] = []
        for r in op.inputs:
            for sref in ([r] if r in sources else ops[r].source_refs):
                if sref not in reach:
                    reach.append(sref)
        op.source_refs = reach

    # ---- outputs
    outputs: Dict[str, EditOutput] = {}
    input_paths = [s.path for s in sources.values()]
    seen_paths: Set[str] = set()
    for i, raw in enumerate(_list(proj["outputs"], "project.outputs", MAX_OUTPUTS)):
        what = f"project.outputs[{i}]"
        o = _obj(raw, what)
        _keys(o, what, ("id", "operation", "path"), ("id", "operation", "path"))
        ref = _ref(o["id"], what + ".id")
        if ref in outputs:
            raise EditError("INVALID_REQUEST", f"{what}: duplicate output id {ref!r}")
        op_ref = _ref(o["operation"], what + ".operation")
        if op_ref not in ops:
            raise EditError("DEPENDENCY_ERROR", f"{what}: unknown operation {op_ref!r}", {"reason": "unknown_reference"})
        raw_path = o["path"]
        if not isinstance(raw_path, str):
            raise EditError("INVALID_REQUEST", f"{what}.path: must be a string")
        if os.path.isabs(raw_path) or re.match(r"^[A-Za-z]:", raw_path) or raw_path.startswith(("/", "\\")):
            raise EditError("PATH_NOT_ALLOWED", f"{what}.path: output paths are relative to the workspace, never absolute", {"reason": "absolute_output"})
        path = policy.resolve_output(raw_path, what + ".path", input_paths, overwrite=options.overwrite)
        if os.path.normcase(path) in seen_paths:
            raise EditError("DEPENDENCY_ERROR", f"{what}: two outputs share the path", {"reason": "conflicting_outputs"})
        seen_paths.add(os.path.normcase(path))
        outputs[ref] = EditOutput(ref, op_ref, raw_path, path)
    if not outputs:
        raise EditError("INVALID_REQUEST", "project.outputs: at least one output is required")

    # ---- every operation must feed an output (an orphan is a request error, not silent work)
    needed: Set[str] = set()
    stack: List[str] = [o.operation for o in outputs.values()]
    while stack:
        r = stack.pop()
        if r in needed:
            continue
        needed.add(r)
        stack.extend(ops[r].depends_on)
    orphans = sorted(set(ops) - needed)
    if orphans:
        raise EditError("DEPENDENCY_ERROR", f"operations {orphans} do not lead to any output", {"reason": "unused_operation"})

    # ---- deterministic ids (inputs first, so identity chains through the graph)
    for ref in order:
        op = ops[ref]
        ident = [sources[r].identity if r in sources else ops[r].operation_id for r in op.inputs]
        op.operation_id = "op_" + stable_hash({"type": op.type, "params": params_to_json(op.params), "inputs": ident})[:16]

    return EditProject(project_id, sources, ops, order, outputs, options, policy, warnings)


def _toposort(ops: Dict[str, EditOperation]) -> List[str]:
    """Deterministic topological order (Kahn, ties broken by ref). Raises on cycles."""
    indeg = {r: len(o.depends_on) for r, o in ops.items()}
    users: Dict[str, List[str]] = {r: [] for r in ops}
    for r, o in ops.items():
        for dep in o.depends_on:
            users[dep].append(r)
    ready = sorted(r for r, n in indeg.items() if n == 0)
    order: List[str] = []
    while ready:
        r = ready.pop(0)
        order.append(r)
        for u in sorted(users[r]):
            indeg[u] -= 1
            if indeg[u] == 0:
                ready.append(u)
                ready.sort()
    if len(order) != len(ops):
        stuck = sorted(r for r in ops if r not in order)
        raise EditError("DEPENDENCY_ERROR", f"operation graph has a cycle through {stuck}", {"reason": "cycle"})
    return order


__all__ = ["EditProject", "EditSource", "EditOperation", "EditOutput", "EditOptions", "parse_request", "OUTPUT_EXTENSIONS"]
