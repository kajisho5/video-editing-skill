"""Workspace boundary for inputs and outputs.

Vocabulary
  allowed input root  a directory the request may read media from (resolved, symlinks followed)
  workspace           the only directory this skill writes to (outputs and intermediates)

Rules (docs/security.md)
- containment is decided on the resolved path with path components, never a string prefix
  (/w/media is not a prefix match for /w/media_evil); posixpath / ntpath are injectable for tests;
- '..' in a raw request path is refused before resolution;
- a symlink whose target leaves the root is refused (symlink escape);
- outputs must resolve inside the workspace, must not be an input, must not already exist unless
  options.overwrite is true, and their extension must be one this skill knows how to validate;
- Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9) are refused in any component
  on every platform, as are NUL bytes, trailing dots/spaces and characters Windows cannot store.
"""
import os
import posixpath
import stat
from typing import Any, Iterable, List, Optional

from .errors import EditError

VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".m4v", ".webm", ".mts", ".m2ts", ".avi", ".mxf", ".ts")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")
OUTPUT_EXTENSIONS = (".mp4", ".mov", ".mkv")

_RESERVED = {"CON", "PRN", "AUX", "NUL", "CLOCK$"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
_BAD_CHARS = set('<>:"|?*') | {chr(i) for i in range(32)}


def is_within(root: str, path: str, module: Any = os.path) -> bool:
    """Component-wise containment of an absolute `path` inside an absolute `root`."""
    root_n = module.normcase(module.normpath(root))
    path_n = module.normcase(module.normpath(path))
    try:
        return module.commonpath([root_n, path_n]) == root_n
    except ValueError:  # different drives, UNC vs drive, mixed absolute/relative
        return False


def has_traversal(raw: str) -> bool:
    return any(part == ".." for part in raw.replace("\\", "/").split("/"))


def reserved_component(raw: str) -> Optional[str]:
    """The first path component that Windows would refuse or treat as a device, else None."""
    for part in raw.replace("\\", "/").split("/"):
        if not part or part in (".", ".."):  # '.' is the current directory; '..' is handled by has_traversal
            continue
        stem = part.split(".")[0].strip().upper()
        if stem in _RESERVED:
            return part
        body = part
        if len(body) >= 2 and body[1] == ":" and body[0].isalpha():  # drive letter prefix
            body = body[2:]
        if any(ch in _BAD_CHARS for ch in body):
            return part
        if part != part.rstrip(" ."):
            return part
    return None


def check_raw(raw: Any, what: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise EditError("INVALID_REQUEST", f"{what}: path must be a non-empty string without NUL")
    if len(raw) > 4096:
        raise EditError("INVALID_REQUEST", f"{what}: path too long")
    if has_traversal(raw):
        raise EditError("PATH_NOT_ALLOWED", f"{what}: path contains '..'", {"reason": "traversal"})
    bad = reserved_component(raw)
    if bad is not None:
        raise EditError("PATH_NOT_ALLOWED", f"{what}: path component {bad!r} is a reserved name or contains characters not allowed",
                        {"reason": "reserved_name"})
    return raw


class PathPolicy:
    def __init__(self, workspace: str, allowed_input_roots: Optional[Iterable[str]] = None):
        if not isinstance(workspace, str) or not workspace:
            raise EditError("INVALID_REQUEST", "workspace must be a non-empty path")
        check_raw(workspace, "workspace")
        self.workspace = os.path.realpath(os.path.abspath(workspace))
        if not os.path.isdir(self.workspace):
            raise EditError("PATH_NOT_ALLOWED", "workspace is not an existing directory", {"reason": "workspace_missing"})
        roots: List[str] = []
        raw_roots: List[str] = [os.path.abspath(workspace)]
        for r in allowed_input_roots or []:
            check_raw(r, "allowed_input_roots")
            rr = os.path.realpath(os.path.abspath(r))
            if not os.path.isdir(rr):
                raise EditError("PATH_NOT_ALLOWED", f"allowed input root is not a directory: {os.path.basename(r) or r}", {"reason": "root_not_directory"})
            roots.append(rr)
            raw_roots.append(os.path.abspath(r))
        self.allowed_input_roots = roots or [self.workspace]
        # the roots as given (before symlink resolution): a path that *looks* inside one of them but resolves
        # outside is a symlink escape; a path that never looked inside is simply outside. (/var vs /private/var
        # on macOS and 8.3 short names on Windows make "raw != resolved" useless as the escape signal.)
        self._apparent_roots = raw_roots[1:] if roots else raw_roots[:1]

    # ---- inputs
    def resolve_input(self, raw: str, what: str, extensions: Iterable[str]) -> str:
        """Resolved absolute path of an authorised, existing, readable regular file inside an allowed root."""
        check_raw(raw, what)
        absolute = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(self.workspace, raw))
        resolved = os.path.realpath(absolute)
        if not any(is_within(root, resolved) for root in self.allowed_input_roots):
            apparent = os.path.normpath(absolute)
            escaped = any(is_within(r, apparent) for r in self._apparent_roots + self.allowed_input_roots)
            raise EditError("PATH_NOT_ALLOWED", f"{what}: input is outside the allowed input roots: {os.path.basename(raw) or raw}",
                            {"reason": "symlink_escape" if escaped else "outside_allowed_roots"})
        if not os.path.lexists(absolute) or not os.path.exists(resolved):
            raise EditError("MISSING_INPUT", f"{what}: input not found: {raw}")
        st = os.stat(resolved)
        if not stat.S_ISREG(st.st_mode):
            raise EditError("INVALID_INPUT", f"{what}: input is not a regular file: {raw}", {"reason": "not_regular_file"})
        if st.st_size == 0:
            raise EditError("INVALID_INPUT", f"{what}: input is empty: {raw}")
        if not os.access(resolved, os.R_OK):
            raise EditError("INVALID_INPUT", f"{what}: input is not readable: {raw}")
        ext = os.path.splitext(resolved)[1].lower()
        if ext not in tuple(extensions):
            raise EditError("UNSUPPORTED_FORMAT", f"{what}: extension {ext or '(none)'} is not supported", {"supported": sorted(extensions)})
        return resolved

    # ---- outputs
    def resolve_output(self, raw: str, what: str, inputs: Iterable[str], overwrite: bool = False) -> str:
        check_raw(raw, what)
        absolute = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(self.workspace, raw))
        resolved = self._resolve_deepest(absolute)
        if not is_within(self.workspace, resolved) or resolved == self.workspace:
            raise EditError("PATH_NOT_ALLOWED", f"{what}: output must be inside the workspace: {os.path.basename(raw) or raw}",
                            {"reason": "workspace_escape"})
        ext = os.path.splitext(resolved)[1].lower()
        if ext not in OUTPUT_EXTENSIONS:
            raise EditError("UNSUPPORTED_FORMAT", f"{what}: output extension {ext or '(none)'} is not supported", {"supported": list(OUTPUT_EXTENSIONS)})
        for i in inputs:
            if os.path.normcase(i) == os.path.normcase(resolved):
                raise EditError("PATH_NOT_ALLOWED", f"{what}: output would overwrite an input", {"reason": "overwrite_input"})
        if os.path.lexists(absolute) or os.path.lexists(resolved):
            if os.path.isdir(resolved):
                raise EditError("PATH_NOT_ALLOWED", f"{what}: output path is a directory", {"reason": "not_regular_file"})
            if not overwrite:
                raise EditError("PATH_NOT_ALLOWED", f"{what}: output already exists (set options.overwrite to replace it)",
                                {"reason": "exists"})
        return resolved

    def _resolve_deepest(self, absolute: str) -> str:
        """Resolve the deepest existing ancestor so a symlinked parent cannot move a write elsewhere."""
        probe = absolute
        rest: List[str] = []
        while not os.path.lexists(probe):
            parent, name = os.path.split(probe)
            if parent == probe:
                break
            rest.insert(0, name)
            probe = parent
        resolved = os.path.realpath(probe)
        return os.path.join(resolved, *rest) if rest else resolved

    def work_dir(self, create: bool = True) -> str:
        """<workspace>/.video-editing/work, verified inside the workspace; created only when `create` (run, not plan)."""
        base = os.path.join(self.workspace, ".video-editing", "work")
        if create:
            os.makedirs(base, exist_ok=True)
        if not is_within(self.workspace, os.path.realpath(base)):
            raise EditError("PATH_NOT_ALLOWED", "work directory resolves outside the workspace", {"reason": "workspace_escape"})
        return base

    def describe(self) -> dict:
        return {"workspace": self.workspace, "allowed_input_roots": list(self.allowed_input_roots),
                "input_rule": "regular files under allowed roots (symlinks resolved, '..' refused, reserved names refused)",
                "output_rule": "inside the workspace, never an input, never an existing file unless overwrite"}


__all__ = ["PathPolicy", "is_within", "has_traversal", "reserved_component", "check_raw", "posixpath",
           "VIDEO_EXTENSIONS", "IMAGE_EXTENSIONS", "OUTPUT_EXTENSIONS"]
