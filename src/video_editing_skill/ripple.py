"""Ripple cuts from a transcript selection (ADR-042, Velisa ADR-032): where exactly each cut goes, and the timeline after it.

`video-editing ripple` takes the spans a person chose to remove (transcription-skill `transcription/select`'s result, or
plain spans), places every cut edge inside the window its span allows, rounds it to a frame of the source, and closes
the gaps. It writes a JSON edit decision list and, when given, the transcript and the subtitles retimed to the cut. It
applies nothing to the media: its `edit_request` (one CONDENSE) is run by `video-editing run`. Rule `ripple@1`:

1. Margins. An in edge (the removal starts) keeps `after_kept` (0.20 s) after the kept speech before it; an out edge (the
   removal ends) keeps `before_kept` (0.10 s) before the kept speech after it. Neither passes the middle of its window,
   so a closed gap leaves a pause of at most 0.30 s. An edge with no kept speech on its side sits at the window's end.
2. Silence. With the engine's silences (ffmpeg-skill silence --list, default -40 dB for 0.05 s), an edge whose window
   holds silence moves to the silent point nearest its margin target. Without silence in the window the target stands.
3. Frames. The edge is rounded to the nearest frame of the source's rate (velisa/timebase@1: the rate read exactly, a half
   up). A frame outside the window is replaced by the nearest frame inside it; a window that holds no frame puts the edge
   on the removed side (a sliver of a removed word may stay, a kept word is never cut). Cuts that meet are merged; a cut
   of no frames is dropped and listed.
4. Positions after the cut are counted in frames, never summed from rounded values (TB-5).
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from fractions import Fraction
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import EditError
from .timebase import RateError, format_seconds, frames_at, parse_rate, rate_str

RIPPLE_SCHEMA = "video-editing/ripple@1"
RESULT_SCHEMA = "video-editing/ripple-result@1"
EDL_SCHEMA = "video-editing/ripple-edl@1"
RULE_VERSION = "ripple@1"
SELECTION_RULE = "transcript-selection@1"
MARGIN_DEFAULTS = {"after_kept": Fraction(1, 5), "before_kept": Fraction(1, 10)}
MARGIN_MAX = Fraction(2)
SILENCE_DEFAULTS = {"threshold_db": -40.0, "min_seconds": 0.05}
LABEL = "transcript"
MAX_SPANS = 5000
MAX_SUBTITLES = 8
MAX_TEXT_BYTES = 64 * 1024 * 1024
NO_SPACE_LANGUAGES = frozenset({"ja", "zh", "yue", "th", "lo", "km", "my", "bo"})
SUBTITLE_EXTENSIONS = (".srt", ".vtt")

RULE: Dict[str, Any] = {
    "version": RULE_VERSION,
    "margins": "in edge: after_kept (default 0.20 s) after the kept speech before it; out edge: before_kept (default 0.10 s) before the kept "
               "speech after it; never past the middle of the window; no kept speech on that side: the window's end",
    "silence": "ffmpeg-skill/silence --list (default -40 dB, 0.05 s): an edge whose window holds silence moves to the silent point nearest "
               "its margin target; false turns it off",
    "frames": "nearest frame of the source's rate (velisa/timebase@1, a half up); outside the window: the nearest frame inside it; a window "
              "with no frame: the frame on the removed side (a sliver of a removed word may stay, a kept word is never cut)",
    "merge": "cuts that meet or overlap are one cut; a cut of no frames is dropped and listed under dropped",
    "positions": "record positions counted in frames (TB-5)",
    "retime": "a word (or a segment without words) whose middle lies in a cut is removed; every other time t maps to its record time, "
              "a start inside a cut to the frame after it, an end to the frame before it; a cue that loses all its time is dropped, one "
              "that loses part is shortened (its text is kept as written)",
}


# ------------------------------------------------------------------ small helpers
def _frac(v: Any, what: str) -> Fraction:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise EditError("INVALID_REQUEST", f"{what}: must be a number of seconds")
    try:
        f = Fraction(repr(v)) if isinstance(v, float) else Fraction(str(v))
    except (ValueError, ZeroDivisionError):
        raise EditError("INVALID_REQUEST", f"{what}: must be a number of seconds") from None
    if f < 0:
        raise EditError("INVALID_REQUEST", f"{what}: must not be negative")
    return f


def _keys(d: Any, what: str, allowed: Sequence[str], required: Sequence[str] = ()) -> None:
    if not isinstance(d, dict):
        raise EditError("INVALID_REQUEST", f"{what}: must be an object")
    extra = sorted(set(d) - set(allowed))
    if extra:
        raise EditError("INVALID_REQUEST", f"{what}: unknown keys {extra}")
    missing = [k for k in required if k not in d]
    if missing:
        raise EditError("INVALID_REQUEST", f"{what}: missing {missing}")


def _sec(f: Fraction) -> str:
    return format_seconds(f, 6)


def _ms(f: Fraction) -> int:
    return math.floor(f * 1000 + Fraction(1, 2))


# ------------------------------------------------------------------ spans in
class Span:
    __slots__ = ("id", "start", "end", "in_lo", "in_hi", "after_kept", "out_lo", "out_hi", "before_kept", "text", "speakers", "segments", "plain")

    def __init__(self, sid: str, start: Fraction, end: Fraction, in_lo: Fraction, in_hi: Fraction, after_kept: bool, out_lo: Fraction,
                 out_hi: Fraction, before_kept: bool, text: str = "", speakers: Optional[List[str]] = None, segments: Optional[List[str]] = None):
        self.id, self.start, self.end = sid, start, end
        self.in_lo, self.in_hi, self.after_kept = in_lo, in_hi, after_kept
        self.out_lo, self.out_hi, self.before_kept = out_lo, out_hi, before_kept
        self.text, self.speakers, self.segments = text, speakers or [], segments or []
        self.plain = False   # given as {start, end} only: each edge is the nearest frame of the time given


def parse_spans(raw: Any, what: str = "spans") -> List[Span]:
    """Spans as transcription/select writes them ({id, start, end, in: {lo, hi, after_kept}, out: {lo, hi, before_kept}}), or plain
    {start, end} (no window, no margin: each edge is the nearest frame of the time given)."""
    if not isinstance(raw, list) or not raw or len(raw) > MAX_SPANS:
        raise EditError("INVALID_REQUEST", f"{what}: must be a list of 1..{MAX_SPANS} spans")
    out: List[Span] = []
    for i, sp in enumerate(raw):
        w = f"{what}[{i}]"
        if not isinstance(sp, dict):
            raise EditError("INVALID_REQUEST", f"{w}: must be an object")
        s, e = _frac(sp.get("start"), w + ".start"), _frac(sp.get("end"), w + ".end")
        if e < s:
            raise EditError("INVALID_REQUEST", f"{w}: end is before start")
        win_in = sp.get("in") if sp.get("in") is not None else {"lo": sp.get("start"), "hi": sp.get("start"), "after_kept": False}
        win_out = sp.get("out") if sp.get("out") is not None else {"lo": sp.get("end"), "hi": sp.get("end"), "before_kept": False}
        if not isinstance(win_in, dict) or not isinstance(win_out, dict):
            raise EditError("INVALID_REQUEST", f"{w}: in / out must be {{lo, hi, after_kept | before_kept}}")
        il, ih = _frac(win_in.get("lo"), w + ".in.lo"), _frac(win_in.get("hi"), w + ".in.hi")
        ol, oh = _frac(win_out.get("lo"), w + ".out.lo"), _frac(win_out.get("hi"), w + ".out.hi")
        if not (il <= ih <= s + Fraction(1, 1000) and e - Fraction(1, 1000) <= ol <= oh):
            raise EditError("INVALID_REQUEST", f"{w}: windows must hold in.lo <= in.hi <= start and end <= out.lo <= out.hi")
        raw_id = sp.get("id")
        sid = raw_id if isinstance(raw_id, str) and re.match(r"^[A-Za-z0-9_.-]{1,32}$", raw_id) else f"r{i + 1}"
        span = Span(sid, s, e, il, ih, bool(win_in.get("after_kept")), ol, oh, bool(win_out.get("before_kept")),
                    str(sp.get("text") or "")[:2000], [str(x) for x in (sp.get("speaker_ids") or [])][:20],
                    [str(x) for x in (sp.get("segment_ids") or [])][:200])
        span.plain = sp.get("in") is None and sp.get("out") is None
        out.append(span)
    out.sort(key=lambda x: (x.start, x.end))
    for a, b in zip(out, out[1:]):
        if b.in_lo < a.out_lo - Fraction(1, 1000) or b.start < a.end:
            raise EditError("INVALID_REQUEST", f"{what}: spans {a.id} and {b.id} overlap")
    return out


# ------------------------------------------------------------------ placing the edges
def _nearest_in(target: Fraction, lo: Fraction, hi: Fraction, silences: Sequence[Tuple[Fraction, Fraction]]) -> Tuple[Fraction, bool]:
    """The silent point of [lo, hi] nearest `target`, or `target` itself when the window holds no silence."""
    best: Optional[Fraction] = None
    for a, b in silences:
        s, e = max(a, lo), min(b, hi)
        if e < s:
            continue
        p = min(max(target, s), e)
        if best is None or abs(p - target) < abs(best - target):
            best = p
    return (best, True) if best is not None else (target, False)


def _to_frame(t: Fraction, lo: Fraction, hi: Fraction, rate: Fraction, removed_side: str) -> Tuple[int, str]:
    """Frame for an edge at `t` inside window [lo, hi]: the nearest (a half up), else the nearest inside the window, else the frame on
    the removed side ("after": the first frame past lo, for an in edge; "before": the last frame before hi, for an out edge)."""
    f = frames_at(t, rate, "nearest")
    lo_f, hi_f = math.ceil(lo * rate), math.floor(hi * rate)
    if lo_f <= f <= hi_f:
        return f, "nearest"
    if lo_f <= hi_f:
        return min(max(f, lo_f), hi_f), "clamped_to_window"
    return (lo_f if removed_side == "after" else hi_f), "no_frame_in_window"


def place_cuts(spans: Sequence[Span], rate: Fraction, total_frames: int, margins: Dict[str, Fraction],
               silences: Sequence[Tuple[Fraction, Fraction]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(cuts, dropped): each cut {span_ids, in_frame, out_frame, in, out} in frames of `rate`, merged where they meet."""
    raw: List[Dict[str, Any]] = []
    for sp in spans:
        if sp.after_kept:
            target_in = min(sp.in_lo + margins["after_kept"], (sp.in_lo + sp.in_hi) / 2)
            p_in, sil_in = _nearest_in(target_in, sp.in_lo, sp.in_hi, silences)
        else:
            target_in, p_in, sil_in = sp.in_lo, sp.in_lo, False
        if sp.before_kept:
            target_out = max(sp.out_hi - margins["before_kept"], (sp.out_lo + sp.out_hi) / 2)
            p_out, sil_out = _nearest_in(target_out, sp.out_lo, sp.out_hi, silences)
        else:
            target_out, p_out, sil_out = sp.out_hi, sp.out_hi, False
        if sp.plain:
            fi, how_in, fo, how_out = frames_at(p_in, rate, "nearest"), "nearest", frames_at(p_out, rate, "nearest"), "nearest"
        else:
            fi, how_in = _to_frame(p_in, sp.in_lo, sp.in_hi, rate, "after")
            fo, how_out = _to_frame(p_out, sp.out_lo, sp.out_hi, rate, "before")
        fi, fo = max(0, min(fi, total_frames)), max(0, min(fo, total_frames))
        edge = {"in": {"target": _sec(target_in), "silence": sil_in, "placed": _sec(p_in), "frame": fi, "rule": how_in,
                       "window": [_sec(sp.in_lo), _sec(sp.in_hi)]},
                "out": {"target": _sec(target_out), "silence": sil_out, "placed": _sec(p_out), "frame": fo, "rule": how_out,
                        "window": [_sec(sp.out_lo), _sec(sp.out_hi)]}}
        raw.append({"span_ids": [sp.id], "in_frame": fi, "out_frame": fo, "edges": [edge], "spans": [sp]})
    cuts: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for c in raw:
        if c["out_frame"] <= c["in_frame"]:
            dropped.append({"span_ids": c["span_ids"], "reason": "no whole frame between the edges", "edges": c["edges"]})
            continue
        if cuts and c["in_frame"] <= cuts[-1]["out_frame"]:
            last = cuts[-1]
            last["out_frame"] = max(last["out_frame"], c["out_frame"])
            last["span_ids"] += c["span_ids"]
            last["edges"] += c["edges"]
            last["spans"] += c["spans"]
            continue
        cuts.append(c)
    return cuts, dropped


# ------------------------------------------------------------------ the timeline after the cut
class Timeline:
    """Kept frame ranges of the source and their record positions, counted in frames."""

    def __init__(self, cuts: Sequence[Dict[str, Any]], rate: Fraction, total_frames: int):
        self.rate, self.total = rate, total_frames
        self.cuts = [(c["in_frame"], c["out_frame"]) for c in cuts]
        self.kept: List[Tuple[int, int, int]] = []   # (source in, source out, record in), frames
        pos, rec = 0, 0
        for a, b in self.cuts:
            if a > pos:
                self.kept.append((pos, a, rec))
                rec += a - pos
            pos = b
        if self.total > pos:
            self.kept.append((pos, self.total, rec))
            rec += self.total - pos
        self.record_frames = rec

    def removed(self, t: Fraction) -> bool:
        f = t * self.rate
        return any(a <= f < b for a, b in self.cuts)

    def map(self, t: Fraction, side: str) -> Fraction:
        """Record time of source time t. Inside a cut: a start maps to the frame after it, an end to the frame before it (the same
        record point)."""
        f = t * self.rate
        for a, b, rec in self.kept:
            if a <= f <= b:
                return (rec + (f - a)) / self.rate
        for a, b in self.cuts:
            if a <= f < b:
                before = [k for k in self.kept if k[1] <= a]
                if before:
                    k = before[-1]
                    return Fraction(k[2] + (k[1] - k[0])) / self.rate
                return Fraction(0)
        return Fraction(self.record_frames) / self.rate


# ------------------------------------------------------------------ retiming a transcript
def _joiner(language: Any) -> str:
    return "" if isinstance(language, str) and language in NO_SPACE_LANGUAGES else " "


def retime_transcript(doc: Dict[str, Any], tl: Timeline) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """A copy of a transcription-skill Transcript on the record timeline: removed words and segments dropped, the rest mapped,
    word indices renumbered, duration the record duration. Fields it does not know are copied unchanged."""
    if not isinstance(doc, dict) or not isinstance(doc.get("segments"), list):
        raise EditError("INVALID_INPUT", "transcript: not a transcription-skill Transcript (no segments)", {"reason": "not_a_transcript"})
    out = json.loads(json.dumps(doc))
    joiner = _joiner(doc.get("language"))
    segs: List[Dict[str, Any]] = []
    counts = {"words_removed": 0, "segments_removed": 0, "segments_shortened": 0}
    index = 0
    for i, seg in enumerate(out["segments"]):
        try:
            s, e = _frac(seg["start"], f"segments[{i}].start"), _frac(seg["end"], f"segments[{i}].end")
        except (KeyError, EditError):
            raise EditError("INVALID_INPUT", f"transcript: segments[{i}] has no valid start / end", {"reason": "not_a_transcript"}) from None
        words = seg.get("words")
        if words:
            kept_words = []
            for j, w in enumerate(words):
                ws, we = _frac(w.get("start"), f"segments[{i}].words[{j}].start"), _frac(w.get("end"), f"segments[{i}].words[{j}].end")
                if tl.removed((ws + we) / 2):
                    counts["words_removed"] += 1
                    continue
                w = dict(w, start=float(_sec(tl.map(ws, "start"))), end=float(_sec(tl.map(we, "end"))))
                if "index" in w:
                    w["index"] = index
                index += 1
                kept_words.append(w)
            if not kept_words:
                counts["segments_removed"] += 1
                continue
            if len(kept_words) != len(words):
                counts["segments_shortened"] += 1
                text = ("".join(str(w["text"]) for w in kept_words).strip() if any(str(w["text"])[:1].isspace() for w in kept_words)
                        else joiner.join(str(w["text"]).strip() for w in kept_words))
                seg["text"] = text
                seg["raw_text"] = text
            seg["words"] = kept_words
            seg["start"], seg["end"] = kept_words[0]["start"], kept_words[-1]["end"]
        else:
            if tl.removed((s + e) / 2):
                counts["segments_removed"] += 1
                continue
            seg["start"], seg["end"] = float(_sec(tl.map(s, "start"))), float(_sec(tl.map(e, "end")))
        if seg["end"] <= seg["start"]:
            seg["end"] = float(_sec(Fraction(str(seg["start"])) + 1 / tl.rate))
        segs.append(seg)
    out["segments"] = segs
    dur = float(_sec(Fraction(tl.record_frames) / tl.rate))
    out["duration"] = dur
    if isinstance(out.get("source"), dict) and "media_duration" in out["source"]:
        out["source"]["media_duration"] = dur
    out["id"] = f"{doc.get('id') or 'transcript'}.rippled"
    warn = (f"retimed by video-editing-skill ripple ({RULE_VERSION}) to the cut programme: {counts['words_removed']} word(s) and "
            f"{counts['segments_removed']} segment(s) removed; source identity fields describe the uncut source")
    out["warnings"] = list(out.get("warnings") or []) + [warn]
    return out, counts


# ------------------------------------------------------------------ retiming subtitles (SRT / VTT)
_TS = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})\s*$")
_CUE = re.compile(r"^\s*(\S+)\s+-->\s+(\S+)(.*)$")


def _parse_ts(text: str) -> Optional[Fraction]:
    m = _TS.match(text)
    if not m:
        return None
    h, mi, s, ms = (int(x) if x else 0 for x in m.groups())
    return Fraction(((h * 60 + mi) * 60 + s) * 1000 + ms, 1000)


def _fmt_ts(t: Fraction, sep: str) -> str:
    ms = _ms(t)
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def retime_subtitles(text: str, fmt: str, tl: Timeline) -> Tuple[str, Dict[str, int]]:
    """SRT or WebVTT text with every cue moved to the record timeline. Cue text, settings, notes and styles are kept."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    sep = "," if fmt == ".srt" else "."
    blocks: List[List[str]] = []
    cur: List[str] = []
    for ln in lines:
        if ln.strip() == "":
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(ln)
    if cur:
        blocks.append(cur)
    out_blocks: List[str] = []
    counts = {"cues": 0, "dropped": 0, "shortened": 0}
    n = 0
    for b in blocks:
        at = next((k for k, ln in enumerate(b) if "-->" in ln), None)
        if at is None:
            out_blocks.append("\n".join(b))   # WEBVTT header, NOTE, STYLE, REGION: kept as written
            continue
        m = _CUE.match(b[at])
        s = _parse_ts(m.group(1)) if m else None
        e = _parse_ts(m.group(2)) if m else None
        if m is None or s is None or e is None or e < s:
            raise EditError("INVALID_INPUT", f"subtitles: a cue time is not valid: {b[at][:80]!r}", {"reason": "subtitle_unreadable"})
        counts["cues"] += 1
        ns, ne = tl.map(s, "start"), tl.map(e, "end")
        if ne <= ns or _ms(ne) <= _ms(ns):
            counts["dropped"] += 1
            continue
        if (ne - ns) * 1000 < (e - s) * 1000 - 1:
            counts["shortened"] += 1
        n += 1
        timing = f"{_fmt_ts(ns, sep)} --> {_fmt_ts(ne, sep)}{m.group(3)}"
        if fmt == ".srt":
            body = b[at + 1:]
            out_blocks.append("\n".join([str(n), timing] + body))
        else:
            out_blocks.append("\n".join(b[:at] + [timing] + b[at + 1:]))
    return "\n\n".join(out_blocks) + "\n", counts


# ------------------------------------------------------------------ the command
def _read_json(path: str, what: str) -> Any:
    if os.path.getsize(path) > MAX_TEXT_BYTES:
        raise EditError("INVALID_INPUT", f"{what}: larger than {MAX_TEXT_BYTES} bytes", {"reason": "oversized"})
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, UnicodeDecodeError) as exc:
        raise EditError("INVALID_INPUT", f"{what}: not UTF-8 JSON: {exc}", {"reason": "not_json"}) from exc


def _write(path: str, data: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.partial.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)
    os.replace(tmp, path)


def ripple(doc: Any, policy: Any, skill: Any = None, overwrite: bool = False) -> Dict[str, Any]:
    """`video-editing ripple`: read a ripple request, place the cuts, write the EDL and the retimed files, return the result."""
    from .canonical import sha256_file
    from .paths import VIDEO_EXTENSIONS
    _keys(doc, "request", ("schema", "source", "selection", "spans", "fps", "duration", "margins", "silence", "transcript", "subtitles",
                           "outputs"), ("schema", "source"))
    if doc["schema"] != RIPPLE_SCHEMA:
        raise EditError("INVALID_REQUEST", f"request.schema must be {RIPPLE_SCHEMA!r}", {"got": doc["schema"]})
    if ("selection" in doc) == ("spans" in doc):
        raise EditError("INVALID_REQUEST", "request: give exactly one of selection (transcription/select's result or its file) or spans")
    src = policy.resolve_input(doc["source"], "source", VIDEO_EXTENSIONS)
    inputs: List[Dict[str, Any]] = [{"role": "source", "file": os.path.basename(src), "path": src, "sha256": sha256_file(src)}]
    selection_meta: Optional[Dict[str, Any]] = None
    if "selection" in doc:
        sel = doc["selection"]
        if isinstance(sel, str):
            path = policy.resolve_input(sel, "selection", (".json",))
            inputs.append({"role": "selection", "file": os.path.basename(path), "path": path, "sha256": sha256_file(path)})
            sel = _read_json(path, "selection")
        if isinstance(sel, dict) and isinstance(sel.get("result"), dict):   # a `run -` response of transcription-skill
            sel = sel["result"]
        if not isinstance(sel, dict) or (sel.get("rule") or {}).get("version") != SELECTION_RULE:
            raise EditError("INVALID_INPUT", f"selection: not a transcription/select result (rule {SELECTION_RULE})", {"reason": "not_a_selection"})
        spans = parse_spans(sel.get("spans"), "selection.spans")
        selection_meta = {"rule": SELECTION_RULE, "mode": sel.get("mode"), "transcript_id": sel.get("transcript_id"),
                          "sentences": sel.get("sentences") or [], "speech_seconds": sel.get("speech_seconds")}
    else:
        spans = parse_spans(doc["spans"])
    margins = dict(MARGIN_DEFAULTS)
    if "margins" in doc:
        _keys(doc["margins"], "margins", ("after_kept", "before_kept"))
        for k, v in doc["margins"].items():
            f = _frac(v, f"margins.{k}")
            if f > MARGIN_MAX:
                raise EditError("INVALID_REQUEST", f"margins.{k}: at most {MARGIN_MAX} s")
            margins[k] = f
    cfg: Dict[str, float] = dict(SILENCE_DEFAULTS)
    if "silence" in doc and doc["silence"] is not False:
        _keys(doc["silence"], "silence", ("threshold_db", "min_seconds"))
        for k, lo, hi in (("threshold_db", -90, -5), ("min_seconds", 0.02, 10)):
            if k in doc["silence"]:
                v = doc["silence"][k]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                    raise EditError("INVALID_REQUEST", f"silence.{k}: must be a number between {lo} and {hi}")
                cfg[k] = float(v)
    sil_cfg: Optional[Dict[str, float]] = None if doc.get("silence") is False else cfg
    # the source's rate and duration: given, or measured by ffmpeg-skill's probe
    # the source's rate and frame count: given (fps, duration), or measured by ffmpeg-skill's probe (r_frame_rate, the video stream's
    # nb_frames, else its duration): the picture's own frames, not the container's duration, which the sound can make longer
    rate_raw, dur_raw = doc.get("fps"), doc.get("duration")
    nb_frames: Optional[int] = None
    if rate_raw is None or dur_raw is None:
        if skill is None:
            raise EditError("TOOL_ERROR", "ripple needs ffmpeg-skill to read the source's rate and duration (or give fps and duration)", retryable=False)
        from .ffmpeg_skill import probe
        pdoc = probe(skill, src)
        video = pdoc.get("video") or {}
        if not video:
            raise EditError("INVALID_INPUT", "source: no video stream", {"reason": "no_video"})
        if rate_raw is None:
            rate_raw = video.get("r_frame_rate") if isinstance(video.get("r_frame_rate"), str) and video.get("r_frame_rate") != "0/0" else video.get("fps")
        if dur_raw is None:
            dur_raw = video.get("duration") or pdoc.get("duration")
            n = video.get("nb_frames")
            nb_frames = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else None
    try:
        rate = parse_rate(rate_raw)
    except RateError as e:
        raise EditError("INVALID_INPUT", f"source: frame rate {rate_raw!r}: {e}", {"reason": "no_frame_rate"}) from None
    duration = _frac(dur_raw, "duration")
    total = nb_frames if nb_frames is not None else frames_at(duration, rate, "nearest")
    duration = max(duration, Fraction(total) / rate)
    if total <= 0:
        raise EditError("INVALID_INPUT", "source: no frames", {"reason": "no_frames"})
    late = [sp.id for sp in spans if sp.end > duration + Fraction(1, 1000)]
    if late:
        raise EditError("INVALID_INPUT", f"spans {', '.join(late)} end after the source ({_sec(duration)} s); a span is never clamped",
                        {"reason": "span_past_end", "spans": late})
    for sp in spans:   # a window's open end at the transcript's duration is the source's end
        sp.out_hi = min(sp.out_hi, duration)
        sp.in_hi = min(sp.in_hi, duration)
        sp.in_lo, sp.out_lo = min(sp.in_lo, sp.in_hi), min(sp.out_lo, sp.out_hi)
    silences: List[Tuple[Fraction, Fraction]] = []
    measured: Optional[Dict[str, Any]] = None
    if sil_cfg is not None:
        if skill is None:
            raise EditError("TOOL_ERROR", "ripple silence needs ffmpeg-skill (or silence: false)", retryable=False)
        from .proposals import _engine_json, _secs
        argv = [src, "--list", f"--threshold={sil_cfg['threshold_db']:g}", "--min-silence", f"{sil_cfg['min_seconds']:g}"]
        sdoc = _engine_json(skill, "silence", argv, "ripple silence")
        silences = [(_secs(a), _secs(b)) for a, b in (sdoc.get("silences") or []) if float(b) > float(a)]
        measured = {"threshold_db": sil_cfg["threshold_db"], "min_seconds": sil_cfg["min_seconds"], "silences": len(silences),
                    "engine": f"ffmpeg-skill@{skill.version}"}
    cuts, dropped = place_cuts(spans, rate, total, margins, silences)
    if not cuts:
        raise EditError("INVALID_INPUT", "no span keeps a whole frame to cut once placed", {"reason": "nothing_to_cut", "dropped": dropped})
    tl = Timeline(cuts, rate, total)
    if tl.record_frames <= 0:
        raise EditError("INVALID_INPUT", "the cuts remove the whole source; nothing would be kept", {"reason": "nothing_kept"})
    rs = rate_str(rate)

    def fr(n: int) -> Dict[str, Any]:
        return {"frame": n, "seconds": _sec(Fraction(n) / rate)}

    cut_rows = []
    for i, c in enumerate(cuts, 1):
        n = c["out_frame"] - c["in_frame"]
        texts = [sp.text for sp in c["spans"] if sp.text]
        speakers: List[str] = []
        for sp in c["spans"]:
            speakers += [x for x in sp.speakers if x not in speakers]
        cut_rows.append({"id": f"c{i}", "span_ids": c["span_ids"], "source_in": fr(c["in_frame"]), "source_out": fr(c["out_frame"]),
                         "frames": n, "seconds": _sec(Fraction(n) / rate), "text": " ".join(texts), "speaker_ids": speakers,
                         "segment_ids": [s for sp in c["spans"] for s in sp.segments], "edges": c["edges"]})
    events = [{"event": i, "source_in": fr(a), "source_out": fr(b), "record_in": fr(rec), "record_out": fr(rec + (b - a))}
              for i, (a, b, rec) in enumerate(tl.kept, 1)]
    removed_frames = sum(c["frames"] for c in cut_rows)
    stem = os.path.splitext(os.path.basename(src))[0]
    outs = doc.get("outputs") or {}
    _keys(outs, "outputs", ("edl", "transcript", "subtitles", "video"))
    pending: List[Tuple[str, str]] = []   # (path, text): written only once every input is read and every output path is allowed
    # retimed files
    transcript_out = None
    tcounts = None
    if doc.get("transcript") is not None:
        tpath = policy.resolve_input(doc["transcript"], "transcript", (".json",))
        inputs.append({"role": "transcript", "file": os.path.basename(tpath), "path": tpath, "sha256": sha256_file(tpath)})
        retimed, tcounts = retime_transcript(_read_json(tpath, "transcript"), tl)
        tstem = os.path.splitext(os.path.basename(tpath))[0]
        transcript_out = policy.resolve_output(outs.get("transcript") or f"ripple/{tstem}.rippled.json", "outputs.transcript",
                                               [i["path"] for i in inputs], overwrite=overwrite, extensions=(".json",))
        pending.append((transcript_out, json.dumps(retimed, ensure_ascii=False, indent=2) + "\n"))
    subs = doc.get("subtitles") or []
    if not isinstance(subs, list) or len(subs) > MAX_SUBTITLES or not all(isinstance(x, str) for x in subs):
        raise EditError("INVALID_REQUEST", f"subtitles: a list of at most {MAX_SUBTITLES} .srt / .vtt paths")
    sub_outs = outs.get("subtitles")
    if sub_outs is not None and (not isinstance(sub_outs, list) or len(sub_outs) != len(subs)):
        raise EditError("INVALID_REQUEST", "outputs.subtitles: one path per subtitles entry")
    sub_rows = []
    for k, sp_ in enumerate(subs):
        spath = policy.resolve_input(sp_, f"subtitles[{k}]", SUBTITLE_EXTENSIONS)
        inputs.append({"role": "subtitles", "file": os.path.basename(spath), "path": spath, "sha256": sha256_file(spath)})
        ext = os.path.splitext(spath)[1].lower()
        if os.path.getsize(spath) > MAX_TEXT_BYTES:
            raise EditError("INVALID_INPUT", f"subtitles[{k}]: too large", {"reason": "oversized"})
        try:
            with open(spath, encoding="utf-8-sig") as fh:
                text = fh.read()
        except UnicodeDecodeError as exc:
            raise EditError("INVALID_INPUT", f"subtitles[{k}]: not UTF-8: {exc}", {"reason": "subtitle_unreadable"}) from exc
        new, counts = retime_subtitles(text, ext, tl)
        sstem = os.path.splitext(os.path.basename(spath))[0]
        target = (sub_outs[k] if sub_outs is not None else f"ripple/{sstem}.rippled{ext}")
        opath = policy.resolve_output(target, f"outputs.subtitles[{k}]", [i["path"] for i in inputs], overwrite=overwrite, extensions=(ext,))
        pending.append((opath, new))
        sub_rows.append({"input": os.path.basename(spath), "path": opath, **counts})
    video_out = outs.get("video") or f"ripple/{stem}.rippled{os.path.splitext(src)[1].lower() if os.path.splitext(src)[1].lower() in ('.mp4', '.mov', '.mkv') else '.mp4'}"
    edit_request = {"schema": "video-editing/request@1",
                    "project": {"id": f"ripple-{stem}"[:64],
                                "sources": [{"id": "source", "path": src}],
                                "operations": [{"id": "ripple", "type": "CONDENSE", "input": "source",
                                                "params": {"segments": [{"start": {"frames": c["source_in"]["frame"], "fps": rs},
                                                                         "end": {"frames": c["source_out"]["frame"], "fps": rs}, "label": LABEL}
                                                                        for c in cut_rows],
                                                           "rules": {LABEL: "cut"}, "default": "keep"}}],
                                "outputs": [{"id": "rippled", "operation": "ripple", "path": video_out}]}}
    edl_path = policy.resolve_output(outs.get("edl") or f"ripple/{stem}.ripple-edl.json", "outputs.edl", [i["path"] for i in inputs],
                                     overwrite=overwrite, extensions=(".json",))
    pub_inputs = [{"sha256": i["sha256"], "file": i["file"], "role": i["role"]} for i in inputs]
    edl = {"schema": EDL_SCHEMA, "producer": {"skill_id": "video-editing", "version": _version()},
           "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "inputs": pub_inputs, "rule": RULE_VERSION,
           "rate": rs, "source": {"file": os.path.basename(src), "frames": total, "seconds": _sec(Fraction(total) / rate)},
           "events": events, "removed": [{k: c[k] for k in ("id", "span_ids", "source_in", "source_out", "frames", "seconds")} for c in cut_rows],
           "record": {"frames": tl.record_frames, "seconds": _sec(Fraction(tl.record_frames) / rate)}}
    pending.append((edl_path, json.dumps(edl, ensure_ascii=False, indent=2) + "\n"))
    if len({os.path.normcase(p_) for p_, _ in pending}) != len(pending):
        raise EditError("PATH_NOT_ALLOWED", "outputs: two outputs have the same path", {"reason": "duplicate_output"})
    for p_, text_ in pending:
        _write(p_, text_)
    warnings = []
    if any(e["rule"] == "no_frame_in_window" for c in cut_rows for ed in c["edges"] for e in (ed["in"], ed["out"])):
        warnings.append("an edge had no frame inside its window and was put on the removed side: a sliver of a removed word may remain")
    return {"status": "planned", "applied": False, "rule": dict(RULE), "rate": rs,
            "margins": {k: _sec(v) for k, v in margins.items()}, "silence": measured,
            "source": {"file": os.path.basename(src), "frames": total, "seconds": _sec(Fraction(total) / rate)},
            "selection": selection_meta, "cuts": cut_rows, "dropped": dropped,
            "removed": {"frames": removed_frames, "seconds": _sec(Fraction(removed_frames) / rate)},
            "record": {"frames": tl.record_frames, "seconds": _sec(Fraction(tl.record_frames) / rate)},
            "events": events, "edl": edl_path, "transcript": ({"path": transcript_out, **(tcounts or {})} if transcript_out else None),
            "subtitles": sub_rows, "edit_request": edit_request, "inputs": pub_inputs, "warnings": warnings,
            "next": "show the cuts (text, speakers, seconds) and get a yes; then `video-editing run` with edit_request cuts the media"}


def _version() -> str:
    from . import __version__
    return __version__


def check_ripple_doc(doc: Any) -> List[str]:
    """Self-check of a ripple response: the shape callers key on."""
    p: List[str] = []
    if not isinstance(doc, dict):
        return ["not an object"]
    if doc.get("schema") != RESULT_SCHEMA:
        p.append("schema")
    if doc.get("applied") is not False:
        p.append("applied must be false")
    for k in ("cuts", "events", "edit_request", "edl", "removed", "record"):
        if k not in doc:
            p.append(f"missing {k}")
    last = -1
    for c in doc.get("cuts") or []:
        a, b = c["source_in"]["frame"], c["source_out"]["frame"]
        if not (last <= a < b):
            p.append(f"cut {c.get('id')} out of order")
        last = b
    rec = 0
    for e in doc.get("events") or []:
        if e["record_in"]["frame"] != rec:
            p.append(f"event {e.get('event')} record_in")
        rec = e["record_out"]["frame"]
    if doc.get("record") and rec != doc["record"]["frames"]:
        p.append("record frames")
    return p
