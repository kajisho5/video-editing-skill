"""EditTimeline: where every piece of an output comes from.

Each operation yields a Clip: its duration (exact when every upstream duration is known) and a list of
segments mapping a source time range onto a timeline time range. FIT / FILL / RESIZE / ROTATE / OVERLAY
keep the mapping; SPEED scales timeline ranges; CONCAT offsets them (a transition overlaps neighbours by
its duration). OVERLAY adds a second track holding the image for its visible range. FREEZE splits the mapping
at its time and inserts a held segment (speed 0: one source instant over the hold); REVERSE mirrors it: each
segment plays its source range backwards and is reported with `reversed: true` (source_range stays ascending, speed
stays non-negative). In a segment, source time = source_range.start + (t - timeline_range.start) * speed, or, when
reversed, source_range.end - (t - timeline_range.start) * speed. Internally a reversed segment carries a negative
speed and its source_start is the source time at its timeline start, so the same formula maps through every
operation downstream. IMAGE_INSERT starts a clip from a still: one segment of speed 0 whose source is the image source
(source_range 0..0) over params.duration.

Durations of untrimmed sources come from a probe when one is available; otherwise the clip is marked
duration_known: false and downstream timeline ranges are omitted (never guessed).
"""
from fractions import Fraction
from typing import Any, Dict, List, Optional

from .project import EditOperation, EditProject
from .timebase import Time

ZERO = Time(Fraction(0))


class Segment:
    __slots__ = ("source", "source_start", "source_end", "timeline_start", "timeline_end", "speed")

    def __init__(self, source: str, ss: Time, se: Time, ts: Optional[Time], te: Optional[Time], speed: Fraction = Fraction(1)):
        self.source, self.source_start, self.source_end = source, ss, se
        self.timeline_start, self.timeline_end, self.speed = ts, te, speed

    def to_dict(self) -> Dict[str, Any]:
        lo, hi, speed = self.source_start, self.source_end, self.speed
        if speed < 0:   # played backwards (REVERSE): reported ascending, with the direction as a flag
            lo, hi, speed = hi, lo, -speed
        d: Dict[str, Any] = {"source": self.source, "source_range": {"start": lo.to_dict(), "end": hi.to_dict()},
                             "speed": f"{speed.numerator}/{speed.denominator}"}
        if self.speed < 0:
            d["reversed"] = True
        if self.timeline_start is not None and self.timeline_end is not None:
            d["timeline_range"] = {"start": self.timeline_start.to_dict(), "end": self.timeline_end.to_dict()}
        return d


class Clip:
    def __init__(self, duration: Optional[Time], segments: List[Segment], overlays: Optional[List[Dict[str, Any]]] = None):
        self.duration = duration
        self.segments = segments
        self.overlays = overlays or []

    def to_dict(self) -> Dict[str, Any]:
        tracks: List[Dict[str, Any]] = [{"id": "V1", "kind": "video", "segments": [s.to_dict() for s in self.segments]}]
        if self.overlays:
            tracks.append({"id": "V2", "kind": "overlay", "segments": list(self.overlays)})
        return {"duration_known": self.duration is not None, "duration": self.duration.to_dict() if self.duration is not None else None,
                "tracks": tracks}


def _shift(segs: List[Segment], offset: Time) -> List[Segment]:
    out = []
    for s in segs:
        ts = s.timeline_start + offset if s.timeline_start is not None else None
        te = s.timeline_end + offset if s.timeline_end is not None else None
        out.append(Segment(s.source, s.source_start, s.source_end, ts, te, s.speed))
    return out


def build_timelines(project: EditProject, durations: Dict[str, Optional[Time]]) -> Dict[str, Clip]:
    """Clip per operation ref. `durations` maps source ref -> probed duration (or None when unknown)."""
    clips: Dict[str, Clip] = {}

    def clip_of(ref: str) -> Clip:
        if ref in clips:
            return clips[ref]
        d = durations.get(ref)
        seg = Segment(ref, ZERO, d if d is not None else ZERO, ZERO if d is not None else None, d)
        if d is None:
            seg.source_end = ZERO  # unknown; marked by timeline_range absence
        return Clip(d, [seg])

    for ref in project.order:
        op = project.operations[ref]
        if op.type == "IMAGE_INSERT":   # a still held for params.duration: one segment of speed 0 from the image source
            d = op.params["duration"]
            clips[ref] = Clip(d, [Segment(op.inputs[0], ZERO, ZERO, ZERO, d, Fraction(0))])
            continue
        clips[ref] = _apply(op, [clip_of(r) for r in op.inputs[: (len(op.inputs) - 1 if op.type == "OVERLAY" else None)]], durations)
    return clips


def _apply(op: EditOperation, inputs: List[Clip], durations: Dict[str, Optional[Time]]) -> Clip:
    p = op.params
    if op.type == "TRIM":
        return _cut(inputs[0], [(p["start"], p["end"])])
    if op.type == "CUT":
        return _cut(inputs[0], [(r["start"], r["end"]) for r in p["keep"]])
    if op.type == "SPEED":
        f: Fraction = p["factor"]
        src = inputs[0]
        segs = []
        for s in src.segments:
            ts = s.timeline_start.scale(1 / f) if s.timeline_start is not None else None
            te = s.timeline_end.scale(1 / f) if s.timeline_end is not None else None
            segs.append(Segment(s.source, s.source_start, s.source_end, ts, te, s.speed * f))
        dur = src.duration.scale(1 / f) if src.duration is not None else None
        return Clip(dur, segs, list(src.overlays))
    if op.type in ("FIT", "FILL", "RESIZE", "ROTATE", "CROP"):
        src = inputs[0]
        return Clip(src.duration, list(src.segments), list(src.overlays))
    if op.type == "OVERLAY":
        src = inputs[0]
        start = p.get("start", ZERO)
        end = p.get("end", src.duration)
        ov = {"source": p["image"], "kind": "image", "timeline_range": {"start": start.to_dict(), "end": end.to_dict() if end is not None else None}}
        return Clip(src.duration, list(src.segments), list(src.overlays) + [ov])
    if op.type == "FREEZE":
        return _freeze(inputs[0], p.get("at"), p["hold"])
    if op.type == "REVERSE":
        return _reverse(inputs[0])
    if op.type == "CONCAT":
        tr = p.get("transition")
        d = tr["duration"] if tr else ZERO
        segs = []
        overlays = []
        offset: Optional[Time] = ZERO
        for i, c in enumerate(inputs):
            if offset is not None:
                segs.extend(_shift(c.segments, offset))
            else:
                segs.extend(Segment(s.source, s.source_start, s.source_end, None, None, s.speed) for s in c.segments)
            overlays.extend(c.overlays)
            if c.duration is None or offset is None:
                offset = None
            else:
                offset = offset + c.duration - (d if i < len(inputs) - 1 else ZERO)
        return Clip(offset, segs, overlays)
    raise AssertionError(op.type)


def _cut(src: Clip, ranges: List[tuple]) -> Clip:
    """Keep ranges of an input clip (in the input's own time), placed back to back."""
    segs: List[Segment] = []
    total = ZERO
    for (a, b) in ranges:
        length = b - a
        # map [a, b) of the input clip through its segments
        for s in src.segments:
            if s.timeline_start is None or s.timeline_end is None:
                segs.append(Segment(s.source, a, b, total, total + length, s.speed))  # unknown upstream: assume single source
                continue
            lo = a if a > s.timeline_start else s.timeline_start
            hi = b if b < s.timeline_end else s.timeline_end
            if not lo < hi:
                continue
            # source time = source_start + (t - timeline_start) * speed
            ss = s.source_start + (lo - s.timeline_start).scale(s.speed)
            se = s.source_start + (hi - s.timeline_start).scale(s.speed)
            segs.append(Segment(s.source, ss, se, total + (lo - a), total + (hi - a), s.speed))
        total = total + length
    return Clip(total, segs, [])


def _known(src: Clip) -> bool:
    return src.duration is not None and all(s.timeline_start is not None and s.timeline_end is not None for s in src.segments)


def _source_at(s: Segment, t: Time) -> Time:
    assert s.timeline_start is not None
    return s.source_start + (t - s.timeline_start).scale(s.speed)


def _freeze(src: Clip, at: Optional[Time], hold: Time) -> Clip:
    """Hold one frame for `hold`: at `at` (everything from `at` on plays `hold` later) or, without `at`, the last frame at the
    end. The held instant is a segment of speed 0. Overlay ranges are not carried (as for TRIM / CUT): they cannot be mapped
    exactly through the hold."""
    if not _known(src) or not src.segments:
        return Clip(None, list(src.segments), [])
    assert src.duration is not None
    t = src.duration if at is None else at
    before: List[Segment] = []
    after: List[Segment] = []
    held: Optional[Segment] = None
    for s in src.segments:
        ts, te = s.timeline_start, s.timeline_end
        assert ts is not None and te is not None
        if te <= t:
            before.append(s)
        elif t <= ts:
            after.append(Segment(s.source, s.source_start, s.source_end, ts + hold, te + hold, s.speed))
        else:   # t falls inside: split the segment at the source instant shown at t
            mid = _source_at(s, t)
            before.append(Segment(s.source, s.source_start, mid, ts, t, s.speed))
            after.append(Segment(s.source, mid, s.source_end, t + hold, te + hold, s.speed))
        if held is None and ts <= t and t < te:
            held = Segment(s.source, _source_at(s, t), _source_at(s, t), t, t + hold, Fraction(0))
    if held is None:   # t is the end of the clip: the last frame
        last = src.segments[-1]
        assert last.timeline_end is not None
        held = Segment(last.source, _source_at(last, last.timeline_end), _source_at(last, last.timeline_end), t, t + hold, Fraction(0))
    return Clip(src.duration + hold, before + [held] + after, [])


def _reverse(src: Clip) -> Clip:
    """Play the clip backwards: the segments in reverse order, each mirrored on the timeline with its speed negated.
    Overlay ranges are not carried (as for TRIM / CUT)."""
    if not _known(src):
        return Clip(src.duration, [Segment(s.source, s.source_end, s.source_start, None, None, -s.speed) for s in reversed(src.segments)], [])
    assert src.duration is not None
    d = src.duration
    segs = []
    for s in reversed(src.segments):
        assert s.timeline_start is not None and s.timeline_end is not None
        segs.append(Segment(s.source, _source_at(s, s.timeline_end), s.source_start, d - s.timeline_end, d - s.timeline_start, -s.speed))
    return Clip(d, segs, [])
