"""Exact time. Every edit point is a rational number of seconds (fractions.Fraction), never a float.

Accepted request forms (all converted losslessly):
  12.5                      number of seconds (decimal digits are read as text, so 0.1 is exactly 1/10)
  "12.5" | "1:30" | "00:01:30.250" | "00:01:30,250"   seconds / mm:ss / hh:mm:ss(.ms)
  {"seconds": "12.5"}       explicit seconds as a string
  {"rational": "25/2"}      numerator/denominator
  {"frames": 300, "timebase": "1/30"} or {"frames": 300, "fps": "30000/1001"}   frame count at a rate

Serialised form (stable, machine-readable): {"seconds": "12.500000", "rational": "25/2"}.
"""
import re
from fractions import Fraction
from typing import Any, Dict, Union

from .errors import EditError

_NUM = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")
_MAX_SECONDS = Fraction(10 * 24 * 3600)  # 10 days: anything longer is a malformed request


class Time:
    __slots__ = ("value",)

    def __init__(self, value: Fraction):
        if not isinstance(value, Fraction):
            raise TypeError("Time needs a Fraction")
        self.value = value

    # ---- construction
    @classmethod
    def parse(cls, raw: Any, what: str = "time") -> "Time":
        if isinstance(raw, bool):
            raise EditError("INVALID_REQUEST", f"{what}: booleans are not times")
        if isinstance(raw, int):
            return cls._check(Fraction(raw), what)
        if isinstance(raw, float):
            if raw != raw or raw in (float("inf"), float("-inf")):
                raise EditError("INVALID_REQUEST", f"{what}: not a finite number")
            return cls._check(Fraction(repr(raw)), what)
        if isinstance(raw, str):
            return cls._check(cls._parse_text(raw, what), what)
        if isinstance(raw, dict):
            keys = set(raw)
            if keys == {"seconds"}:
                return cls._check(cls._parse_text(str(raw["seconds"]), what), what)
            if keys == {"rational"}:
                return cls._check(cls._parse_rational(raw["rational"], what), what)
            if keys in ({"frames", "timebase"}, {"frames", "fps"}):
                frames = raw["frames"]
                if isinstance(frames, bool) or not isinstance(frames, int) or frames < 0:
                    raise EditError("INVALID_REQUEST", f"{what}: frames must be a non-negative integer")
                if "timebase" in raw:
                    tb = cls._parse_rational(raw["timebase"], what + ".timebase")
                    if tb <= 0:
                        raise EditError("INVALID_REQUEST", f"{what}: timebase must be positive")
                    return cls._check(Fraction(frames) * tb, what)
                fps = cls._parse_rational(raw["fps"], what + ".fps")
                if fps <= 0:
                    raise EditError("INVALID_REQUEST", f"{what}: fps must be positive")
                return cls._check(Fraction(frames) / fps, what)
            raise EditError("INVALID_REQUEST", f"{what}: a time object needs exactly one of seconds | rational | frames+timebase | frames+fps")
        raise EditError("INVALID_REQUEST", f"{what}: must be a number, a time string or a time object")

    @classmethod
    def _check(cls, v: Fraction, what: str) -> "Time":
        if v < 0:
            raise EditError("INVALID_TIME_RANGE", f"{what}: negative time {v}")
        if v > _MAX_SECONDS:
            raise EditError("INVALID_TIME_RANGE", f"{what}: time {float(v):.0f}s exceeds the {int(_MAX_SECONDS)}s limit")
        return cls(v)

    @staticmethod
    def _parse_text(text: str, what: str) -> Fraction:
        t = text.strip().replace(",", ".")
        if not t or len(t) > 64:
            raise EditError("INVALID_REQUEST", f"{what}: empty or too long time string")
        parts = t.split(":")
        if len(parts) > 3:
            raise EditError("INVALID_REQUEST", f"{what}: bad time string {text!r}")
        total = Fraction(0)
        for i, part in enumerate(parts):
            if not _NUM.match(part) or (i > 0 and part.startswith(("+", "-"))):
                raise EditError("INVALID_REQUEST", f"{what}: bad time string {text!r}")
            total = total * 60 + Fraction(part)
        return total

    @staticmethod
    def _parse_rational(raw: Any, what: str) -> Fraction:
        if isinstance(raw, bool):
            raise EditError("INVALID_REQUEST", f"{what}: bad rational")
        if isinstance(raw, int):
            return Fraction(raw)
        if isinstance(raw, str) and re.match(r"^\d+/\d+$", raw.strip()):
            n, d = raw.strip().split("/")
            if int(d) == 0:
                raise EditError("INVALID_REQUEST", f"{what}: zero denominator")
            return Fraction(int(n), int(d))
        if isinstance(raw, str) and _NUM.match(raw.strip()):
            return Fraction(raw.strip())
        raise EditError("INVALID_REQUEST", f"{what}: rational must look like 'N/D'")

    @classmethod
    def from_float(cls, seconds: float) -> "Time":
        """For measurements (probe durations): rounded to microseconds so identity is stable."""
        return cls(Fraction(round(seconds * 1_000_000), 1_000_000))

    # ---- arithmetic
    def __add__(self, other: "Time") -> "Time":
        return Time(self.value + other.value)

    def __sub__(self, other: "Time") -> "Time":
        return Time(self.value - other.value)

    def scale(self, factor: Fraction) -> "Time":
        return Time(self.value * factor)

    def __lt__(self, other: "Time") -> bool:
        return self.value < other.value

    def __le__(self, other: "Time") -> bool:
        return self.value <= other.value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Time) and self.value == other.value

    def __hash__(self) -> int:
        return hash(self.value)

    def __repr__(self) -> str:
        return f"Time({self.value})"

    # ---- output
    @property
    def seconds(self) -> float:
        return float(self.value)

    def text(self, places: int = 6) -> str:
        """Fixed-point decimal string with `places` digits (exact for values representable that way)."""
        q = Fraction(1, 10 ** places)
        v = self.value
        n = int(round(v / q))
        sign = "-" if n < 0 else ""
        n = abs(n)
        return f"{sign}{n // 10 ** places}.{n % 10 ** places:0{places}d}"

    def tool_arg(self) -> str:
        """The form handed to ffmpeg-skill (which reads it as seconds)."""
        return self.text(6)

    def to_dict(self) -> Dict[str, str]:
        return {"seconds": self.text(6), "rational": f"{self.value.numerator}/{self.value.denominator}"}


def parse_fraction(raw: Any, what: str) -> Fraction:
    """A positive rational factor (speed, fps): number or 'N/D' string."""
    if isinstance(raw, bool):
        raise EditError("INVALID_REQUEST", f"{what}: bad number")
    if isinstance(raw, int):
        return Fraction(raw)
    if isinstance(raw, float):
        if raw != raw or raw in (float("inf"), float("-inf")):
            raise EditError("INVALID_REQUEST", f"{what}: not a finite number")
        return Fraction(repr(raw))
    if isinstance(raw, str):
        return Time._parse_rational(raw, what)
    raise EditError("INVALID_REQUEST", f"{what}: must be a number or 'N/D'")


def fraction_text(f: Fraction) -> str:
    return f"{f.numerator}/{f.denominator}"


TimeLike = Union[int, float, str, Dict[str, Any]]
