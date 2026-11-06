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
from .operations import FORBIDDEN_KEYS, OPERATIONS, params_to_json, validate_encoding, validate_params
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
    ai_generated: Optional[Dict[str, str]] = None   # {generator, licence} as declared by the caller (ADR-022); never inferred

    @property
    def identity(self) -> str:
        return "sha256:" + self.sha256

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"id": self.ref, "kind": self.kind, "path": self.path, "sha256": self.sha256, "size": self.size}
        if self.ai_generated is not None:
            d["ai_generated"] = dict(self.ai_generated, provenance="DECLARED")
        return d


@dataclass
class EditOperation:
    ref: str
    type: str
    inputs: List[str]                    # refs of sources or operations
    params: Dict[str, Any]               # canonical, validated
    operation_id: str = ""
    depends_on: List[str] = field(default_factory=list)   # operation refs only
    source_refs: List[str] = field(default_factory=list)  # every source reachable upstream
    encoding: Optional[Dict[str, Any]] = None             # profile asked for by the output(s) this operation produces

    @property
    def stinger(self) -> Optional[str]:
        """The stinger source of a CONCAT with transition stinger (ADR-023); it is the last entry of `inputs`."""
        tr = self.params.get("transition") if self.type == "CONCAT" else None
        return tr["stinger"] if tr and tr.get("type") == "stinger" else None

    @property
    def video_inputs(self) -> List[str]:
        """The inputs that are picture slots: all but OVERLAY's image and CONCAT's stinger."""
        return self.inputs[:-1] if (self.type == "OVERLAY" or self.stinger is not None) else list(self.inputs)

    @property
    def tool(self) -> str:
        return OPERATIONS[self.type]["tool"]

    @property
    def capability(self) -> str:
        return OPERATIONS[self.type]["capability"]

    def to_dict(self) -> Dict[str, Any]:
        d = {"id": self.ref, "operation_id": self.operation_id, "type": self.type, "capability": self.capability, "tool": self.tool,
             "inputs": list(self.inputs), "params": params_to_json(self.params), "depends_on": list(self.depends_on),
             "sources": list(self.source_refs)}
        if self.encoding:
            d["encoding"] = dict(self.encoding)
        return d


@dataclass
class EditOutput:
    ref: str
    operation: str
    raw_path: str
    path: str
    encoding: Optional[Dict[str, Any]] = None
    max_bytes: Optional[int] = None   # outputs[].max_size: written by ffmpeg-skill export --max-mb, two-pass, retried to fit (ADR-028)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"id": self.ref, "operation": self.operation, "path": self.path}
        if self.encoding:
            d["encoding"] = dict(self.encoding)
        if self.max_bytes is not None:
            d["max_bytes"] = self.max_bytes
        return d


@dataclass
class EditOptions:
    timeout_seconds: float = 3600.0
    overwrite: bool = False
    reuse: bool = True
    segment_cache: bool = False   # CUT renders each keep range alone, cached by content, joined by stream copy (ADR-018)

    def to_dict(self) -> Dict[str, Any]:
        return {"timeout_seconds": self.timeout_seconds, "overwrite": self.overwrite, "reuse": self.reuse, "segment_cache": self.segment_cache}


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
    _keys(d, "options", ("timeout_seconds", "overwrite", "reuse", "segment_cache"))
    o = EditOptions()
    if "timeout_seconds" in d:
        t = d["timeout_seconds"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not (1 <= t <= 86400):
            raise EditError("INVALID_REQUEST", "options.timeout_seconds: must be a number between 1 and 86400")
        o.timeout_seconds = float(t)
    for k in ("overwrite", "reuse", "segment_cache"):
        if k in d:
            if not isinstance(d[k], bool):
                raise EditError("INVALID_REQUEST", f"options.{k}: must be a boolean")
            setattr(o, k, d[k])
    return o


_LABEL = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")


def _ai_generated(raw: Any, what: str) -> Optional[Dict[str, str]]:
    """A source the caller declares AI-generated: {generator, licence}, both short printable text. Reported as declared
    (provenance DECLARED) and carried to every operation and output it reaches; it is never inferred from the media."""
    if raw is None:
        return None
    d = _obj(raw, what)
    _keys(d, what, ("generator", "licence"), ("generator", "licence"))
    out = {}
    for k in ("generator", "licence"):
        if not isinstance(d[k], str) or not _LABEL.fullmatch(d[k]) or not d[k].strip():
            raise EditError("INVALID_REQUEST", f"{what}.{k}: must be 1..200 printable characters")
        out[k] = d[k]
    return out


_SIZE_UNITS = {"bytes": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3}


def _max_size(raw: Any, what: str) -> int:
    """{mb: 25} (or bytes / kb / gb / kib / mib / gib: exactly one key) -> bytes. MB is 1,000,000 bytes, MiB 1,048,576."""
    d = _obj(raw, what)
    if len(d) != 1 or next(iter(d)) not in _SIZE_UNITS:
        raise EditError("INVALID_REQUEST", f"{what}: must be one of {{bytes | kb | mb | gb | kib | mib | gib: number}}", {"allowed": list(_SIZE_UNITS)})
    unit, v = next(iter(d.items()))
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0:
        raise EditError("INVALID_REQUEST", f"{what}.{unit}: must be a positive number")
    n = int(v * _SIZE_UNITS[unit])
    if not (100_000 <= n <= 50 * 1000 ** 3):
        raise EditError("INVALID_REQUEST", f"{what}: must be between 100 kB and 50 GB")
    return n


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
        _keys(s, what, ("id", "path", "kind", "ai_generated"), ("id", "path"))
        ref = _ref(s["id"], what + ".id")
        if ref in sources:
            raise EditError("INVALID_REQUEST", f"{what}: duplicate source id {ref!r}")
        kind = s.get("kind", "video")
        if kind not in ("video", "image"):
            raise EditError("INVALID_REQUEST", f"{what}.kind: must be video or image")
        exts = VIDEO_EXTENSIONS if kind == "video" else IMAGE_EXTENSIONS
        path = policy.resolve_input(s["path"], what + ".path", exts)
        digest = sha256_file(path) if hash_sources else ""
        sources[ref] = EditSource(ref, s["path"], path, kind, digest, os.path.getsize(path), _ai_generated(s.get("ai_generated"), what + ".ai_generated"))
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
        if t == "CONCAT" and (params.get("transition") or {}).get("type") == "stinger":
            inputs.append(params["transition"]["stinger"])
        ops[ref] = EditOperation(ref, t, inputs, params)
    if not ops:
        raise EditError("INVALID_REQUEST", "project.operations: at least one operation is required")

    # ---- references, cycles, order
    for op in ops.values():
        for j, r in enumerate(op.inputs):
            # image slots: OVERLAY's params.image and IMAGE_INSERT's input (a still becomes a clip there, ADR-015); every other slot is video
            is_image_slot = (op.type == "OVERLAY" and j == len(op.inputs) - 1) or op.type == "IMAGE_INSERT"
            if op.stinger is not None and j == len(op.inputs) - 1 and r not in sources:   # the stinger keeps its alpha only as a source file
                raise EditError("DEPENDENCY_ERROR", f"operation {op.ref!r}: transition.stinger must reference a video source (a file with alpha), not an operation",
                                {"reason": "kind_mismatch"})
            if r in sources:
                want = "image" if is_image_slot else "video"
                if sources[r].kind != want:
                    hint = "; turn a still into a clip with IMAGE_INSERT first" if want == "video" else ""
                    raise EditError("DEPENDENCY_ERROR", f"operation {op.ref!r}: input {r!r} must be a {want} source{hint}", {"reason": "kind_mismatch"})
            elif r in ops:
                if is_image_slot:
                    slot = "params.image" if op.type == "OVERLAY" else "input"
                    raise EditError("DEPENDENCY_ERROR", f"operation {op.ref!r}: {slot} must reference an image source, not an operation", {"reason": "kind_mismatch"})
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
        _keys(o, what, ("id", "operation", "path", "encoding", "max_size"), ("id", "operation", "path"))
        ref = _ref(o["id"], what + ".id")
        if ref in outputs:
            raise EditError("INVALID_REQUEST", f"{what}: duplicate output id {ref!r}")
        op_ref = _ref(o["operation"], what + ".operation")
        if op_ref not in ops:
            raise EditError("DEPENDENCY_ERROR", f"{what}: unknown operation {op_ref!r}", {"reason": "unknown_reference"})
        encoding = validate_encoding(o["encoding"], what + ".encoding") if o.get("encoding") is not None else None
        if encoding is not None:
            producer = ops[op_ref]
            if producer.type in ("TRIM", "CUT") and producer.params.get("precision") in ("keyframe", "auto"):
                raise EditError("INVALID_REQUEST", f"{what}.encoding: {producer.type} with precision {producer.params['precision']} may stream-copy and ignore a profile; "
                                "use precision frame",
                                {"reason": "encoding_needs_reencode"})
            if producer.encoding is not None and producer.encoding != encoding:
                raise EditError("DEPENDENCY_ERROR", f"{what}: operation {op_ref!r} is asked for two different encoding profiles", {"reason": "conflicting_encodings"})
            producer.encoding = encoding
        raw_path = o["path"]
        if not isinstance(raw_path, str):
            raise EditError("INVALID_REQUEST", f"{what}.path: must be a string")
        if os.path.isabs(raw_path) or re.match(r"^[A-Za-z]:", raw_path) or raw_path.startswith(("/", "\\")):
            raise EditError("PATH_NOT_ALLOWED", f"{what}.path: output paths are relative to the workspace, never absolute", {"reason": "absolute_output"})
        path = policy.resolve_output(raw_path, what + ".path", input_paths, overwrite=options.overwrite)
        if os.path.normcase(path) in seen_paths:
            raise EditError("DEPENDENCY_ERROR", f"{what}: two outputs share the path", {"reason": "conflicting_outputs"})
        seen_paths.add(os.path.normcase(path))
        max_bytes = _max_size(o["max_size"], what + ".max_size") if o.get("max_size") is not None else None
        if max_bytes is not None:
            if encoding is not None:
                raise EditError("INVALID_REQUEST", f"{what}: max_size sets the bitrate from the size and encoding sets a quality; give one of them",
                                {"reason": "size_and_encoding"})
            if os.path.splitext(raw_path)[1].lower() != ".mp4":
                raise EditError("INVALID_REQUEST", f"{what}: max_size writes H.264 / AAC in .mp4; use an .mp4 path", {"reason": "size_needs_mp4"})
        outputs[ref] = EditOutput(ref, op_ref, raw_path, path, encoding, max_bytes)
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

    # ---- deterministic ids (inputs first, so identity chains through the graph); an encoding profile is part of the
    # identity only when one was asked for, so every id of a request without profiles is unchanged
    for ref in order:
        op = ops[ref]
        ident = [sources[r].identity if r in sources else ops[r].operation_id for r in op.inputs]
        key: Dict[str, Any] = {"type": op.type, "params": params_to_json(op.params), "inputs": ident}
        if op.encoding:
            key["encoding"] = dict(op.encoding)
        op.operation_id = "op_" + stable_hash(key)[:16]

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
