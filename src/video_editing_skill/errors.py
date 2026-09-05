"""Structured errors: one code vocabulary, one exit code per code, one JSON shape.

Every failure the process boundary can report is an EditError. `retryable` says whether the same
request may succeed later without a change (a timeout or a transient tool failure); a bad request
never is.
"""
from typing import Any, Dict, Optional

# (code, exit code, retryable by default)
_CODES = (
    ("INVALID_REQUEST", 2, False),        # request document malformed, unknown keys, wrong types
    ("INVALID_INPUT", 3, False),          # a referenced source is not a usable media / image file
    ("PATH_NOT_ALLOWED", 4, False),       # traversal, symlink escape, outside roots, reserved name, overwrite
    ("UNSUPPORTED_OPERATION", 5, False),  # operation type not in the contract (or not implementable here)
    ("UNSUPPORTED_FORMAT", 6, False),     # output container / extension not supported
    ("MISSING_INPUT", 7, False),          # a referenced file does not exist
    ("INVALID_TIME_RANGE", 8, False),     # start >= end, negative, beyond source duration
    ("DEPENDENCY_ERROR", 9, False),       # unknown reference, cycle, unused / conflicting operation
    ("TOOL_ERROR", 10, True),             # ffmpeg-skill / ffmpeg failed or is missing
    ("OUTPUT_ERROR", 11, False),          # output could not be written / moved
    ("VALIDATION_ERROR", 12, False),      # output exists but does not match what was requested
    ("CANCELLED", 130, True),             # interrupted (SIGINT / SIGTERM) or timed out
    ("INTERNAL_ERROR", 1, False),         # a bug: unexpected exception
)
ERROR_CODES = tuple(c for c, _, _ in _CODES)
EXIT_CODES = {c: e for c, e, _ in _CODES}
DEFAULT_RETRYABLE = {c: r for c, _, r in _CODES}


class EditError(Exception):
    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None, retryable: Optional[bool] = None):
        if code not in EXIT_CODES:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})
        self.retryable = DEFAULT_RETRYABLE[code] if retryable is None else bool(retryable)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.code]

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details}

    def envelope(self) -> Dict[str, Any]:
        return {"ok": False, "error": self.to_dict()}
