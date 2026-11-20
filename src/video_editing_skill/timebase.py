"""Exact time. Every edit point is a rational number of seconds (fractions.Fraction), never a float.

Accepted request forms (all converted losslessly):
  12.5                      number of seconds (decimal digits are read as text, so 0.1 is exactly 1/10)
  "12.5" | "1:30" | "00:01:30.250" | "00:01:30,250"   seconds / mm:ss / hh:mm:ss(.ms)
  {"seconds": "12.5"}       explicit seconds as a string
  {"rational": "25/2"}      numerator/denominator
  {"frames": 300, "timebase": "1/30"} or {"frames": 300, "fps": "30000/1001"}   frame count at a rate

Serialised form (stable, machine-readable): {"seconds": "12.500000", "rational": "25/2"}.
"""
import math
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
                try:   # read as a frame rate (velisa/timebase@1): "29.97" is 30000/1001, an explicit "N/D" is taken as written
                    fps = parse_rate(raw["fps"])
                except RateError as e:
                    raise EditError("INVALID_REQUEST", f"{what}.fps: {e}") from None
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
        """Fixed-point decimal string with `places` digits, the nearest, a half up (exact for values representable that way)."""
        return format_seconds(self.value, places)

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


# ---------------------------------------------------------------------------------------------- frame rates
# The ecosystem's timebase spec (velisa/timebase@1, docs/decisions.md ADR-040). Rates are exact fractions of frames per
# second; a rate written as a rounded decimal (ffmpeg-skill's probe `fps` has 3 decimals) is read by one table of NTSC
# names, never by limit_denominator() (which reads 29.97 as 2997/100 and 59.94 as 2997/50). tests/test_timebase_vectors.py
# runs the spec's conformance vectors (tests/fixtures/velisa/timebase-vectors.json) against these functions.
NTSC_BASES = (24, 30, 48, 60, 120, 240)
NTSC_TOLERANCE = Fraction(5, 1000)
MAX_RATE = Fraction(1000)
DOWN_TOLERANCE = Fraction(1, 1000)
_RATIO = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")
_DECIMAL = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


class RateError(ValueError):
    """A rate or time the timebase spec refuses. `code` is the spec's: INVALID_RATE or INVALID_TIME. Callers turn it into
    an EditError INVALID_REQUEST."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _exact(value: Any, code: str, what: str) -> Fraction:
    if isinstance(value, bool):
        raise RateError(code, f"{what}: a boolean is not a number")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RateError(code, f"{what}: {value!r} is not finite")
        return Fraction(repr(value))
    if isinstance(value, str):
        m = _RATIO.match(value)
        if m:
            if int(m.group(2)) == 0:
                raise RateError(code, f"{what}: {value!r} has a zero denominator")
            return Fraction(int(m.group(1)), int(m.group(2)))
        if _DECIMAL.match(value.strip()):
            return Fraction(value.strip())
    raise RateError(code, f"{what}: {value!r} is not a number")


def round_half_up(x: Fraction) -> int:
    """The one rounding to the nearest integer: a half goes up, toward +infinity (floor(x + 1/2)). Python's round() takes a
    half to the even neighbour and is not used for frames or decimal text."""
    return math.floor(x + Fraction(1, 2))


def parse_rate(value: Any) -> Fraction:
    """A frame rate as an exact fraction. 'N/D' (or a Fraction) is taken as written, an integer is N/1, a decimal that is not
    an integer and lies within 0.005 of base*1000/1001 (base 24, 30, 48, 60, 120, 240) is that NTSC rate ('23.976', '23.98',
    '29.97', '59.94', ...), any other decimal is its exact value ('12.5' is 25/2). A float is read through its shortest
    decimal text (repr). The rate must be in (0, 1000]."""
    text: Any = value.strip() if isinstance(value, str) else value
    try:
        exact = _exact(text, "INVALID_RATE", "frame rate")
    except RateError:
        raise RateError("INVALID_RATE", f"frame rate {value!r} is not N/D, an integer or a decimal") from None
    as_ratio = isinstance(value, Fraction) or (isinstance(text, str) and "/" in text)
    rate = exact
    if not as_ratio and exact.denominator != 1:
        for base in NTSC_BASES:
            ntsc = Fraction(base * 1000, 1001)
            if abs(exact - ntsc) <= NTSC_TOLERANCE:
                rate = ntsc
                break
    if rate <= 0 or rate > MAX_RATE:
        raise RateError("INVALID_RATE", f"frame rate {value!r} is out of range (0, 1000]")
    return rate


def rate_str(rate: Any) -> str:
    """The canonical text of a rate: '25' or '30000/1001'."""
    r = parse_rate(rate)
    return str(r.numerator) if r.denominator == 1 else f"{r.numerator}/{r.denominator}"


def nominal(rate: Any) -> int:
    """Timecode frames per second: the rate rounded half up (30 for 29.97)."""
    return max(1, round_half_up(parse_rate(rate)))


def same_rate(a: Any, b: Any) -> bool:
    """Two rates are the same only when their exact fractions are equal (no float tolerance)."""
    return parse_rate(a) == parse_rate(b)


def probed_rate(rate: Any) -> Fraction:
    """A rate as ffmpeg-skill's probe reports it (`fps`, 3 decimals, a half up) and read back: what a rate measured on a file
    is compared with. 30000/1001 stays 30000/1001; a rate no 3-decimal text names exactly (7/3) becomes 2333/1000, which
    is also what the probe of a 7/3 file reads as, so the two compare equal."""
    return parse_rate(format_seconds(parse_rate(rate), 3))


def frames_at(seconds: Any, rate: Any, mode: str = "nearest", tolerance: Any = DOWN_TOLERANCE) -> int:
    """Frames of a time: `nearest` (floor(t*rate + 1/2), positions) or `down` (floor((t + 1 ms)*rate), joined lengths)."""
    t = seconds.value if isinstance(seconds, Time) else _exact(seconds, "INVALID_TIME", "time")
    r = parse_rate(rate)
    if mode == "nearest":
        return round_half_up(t * r)
    if mode == "down":
        return math.floor((t + _exact(tolerance, "INVALID_TIME", "tolerance")) * r)
    raise RateError("INVALID_TIME", f"unknown rounding mode {mode!r} (nearest | down)")


def seconds_of(frames: int, rate: Any) -> Fraction:
    """The exact start time of frame `frames`: frames / rate."""
    if isinstance(frames, bool) or not isinstance(frames, int):
        raise RateError("INVALID_TIME", f"frame count {frames!r} is not an integer")
    return Fraction(frames) / parse_rate(rate)


def transition_frames(seconds: Any, rate: Any) -> int:
    """A transition's length in whole frames: the nearest, a half up, at least one."""
    return max(1, frames_at(seconds, rate, "nearest"))


def format_seconds(seconds: Any, places: int = 6) -> str:
    """Decimal text of a time to `places` digits, the nearest, a half up."""
    t = seconds.value if isinstance(seconds, Time) else _exact(seconds, "INVALID_TIME", "time")
    n = round_half_up(t * 10 ** places)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if places == 0:
        return f"{sign}{n}"
    return f"{sign}{n // 10 ** places}.{n % 10 ** places:0{places}d}"
