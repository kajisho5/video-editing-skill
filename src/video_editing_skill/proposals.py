"""Cut proposals: measured or rule-based candidates for removal, each with its reason. Nothing here edits anything.

A proposal is data for the caller (the agent or a person) to approve or reject. Only approved proposals reach an edit, as
CONDENSE segments (operations.py). The rules are deterministic and stated in the contract (`proposals`); there is no
model and no learned score (ADR-024).

Retakes (`detect_retakes`) work on a transcript with word timings, {"words": [{"word" | "text", "start", "end"}, ...]}:
  repeat    the same 1-4 words said twice in a row ("the the", "we went we went"): the first saying is proposed
  restart   a phrase of 2+ words broken off and started again within a few words ("I went to the, I went to the store")
  fragment  a cut-off word ("th-", "b—") or a short false start that the next word begins with ("st store")
  filler    a hesitation word of the profile (en: um, uh, erm, ...; ja: えー, えっと, あのー, ...)
Each carries a confidence from its rule (not a probability); `min_confidence` (default 0.5) decides what is proposed.
"""
import re
import unicodedata
from fractions import Fraction
from typing import Any, Dict, List, Optional, Tuple

from .errors import EditError
from .timebase import Time

# ---- language profiles. English is the default; others are regional profiles selected by the caller.
PROFILES: Dict[str, Dict[str, Any]] = {
    "en": {"fillers": {"um": 0.75, "umm": 0.75, "uh": 0.75, "uhh": 0.75, "erm": 0.75, "er": 0.6, "ah": 0.55, "hmm": 0.6, "mm": 0.55},
           # words people double on purpose: a repeat of one of these alone is proposed only below the default threshold
           "emphatic": {"very", "really", "so", "no", "yes", "yeah", "bye", "ha", "go", "more", "many", "much", "long", "far", "had", "that"},
           "fragment_max_chars": 3},
    "ja": {"fillers": {"えー": 0.75, "ええと": 0.75, "えっと": 0.75, "えーと": 0.75, "えーっと": 0.75, "あのー": 0.7, "あの": 0.45,
                       "うーん": 0.7, "んー": 0.65, "まあ": 0.4, "そのー": 0.6},
           "emphatic": {"はい", "うん", "ね", "そう", "どうぞ", "もう"},
           "fragment_max_chars": 2},
}
DEFAULT_PROFILE = "en"
DEFAULT_MIN_CONFIDENCE = 0.5
RESTART_WINDOW = 8          # words between a broken-off phrase and its restart
RESTART_MAX_SECONDS = 4.0   # and at most this long
MAX_WORDS = 200_000

CONFIDENCE = {"repeat_phrase": 0.9, "repeat_word": 0.7, "repeat_emphatic": 0.3, "restart": 0.85, "fragment_cutoff": 0.85, "fragment_prefix": 0.75}

_PUNCT = re.compile(r"[\s\.,!?;:\"'“”‘’()\[\]「」『』、。！？・…]+")


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower()
    return _PUNCT.sub("", t)


def load_words(doc: Any, what: str = "transcript") -> List[Dict[str, Any]]:
    """Validated words [{text, norm, start, end, cutoff}] from a transcript document (or a bare list of words)."""
    words = doc.get("words") if isinstance(doc, dict) else doc
    if not isinstance(words, list):
        raise EditError("INVALID_REQUEST", f"{what}: must be {{\"words\": [{{word, start, end}}, ...]}}")
    if len(words) > MAX_WORDS:
        raise EditError("INVALID_REQUEST", f"{what}: at most {MAX_WORDS} words")
    out: List[Dict[str, Any]] = []
    for i, w in enumerate(words):
        if not isinstance(w, dict):
            raise EditError("INVALID_REQUEST", f"{what}.words[{i}]: must be an object")
        text = w.get("word", w.get("text"))
        if not isinstance(text, str):
            raise EditError("INVALID_REQUEST", f"{what}.words[{i}]: needs word (or text)")
        s, e = Time.parse(w.get("start"), f"{what}.words[{i}].start"), Time.parse(w.get("end"), f"{what}.words[{i}].end")
        if e.value < s.value:
            raise EditError("INVALID_TIME_RANGE", f"{what}.words[{i}]: end before start")
        stripped = text.strip()
        out.append({"index": i, "text": stripped, "norm": _norm(stripped), "start": s, "end": e,
                    "cutoff": stripped.endswith(("-", "—", "–", "‐", "~", "〜"))})
    if any(out[i]["start"].value < out[i - 1]["start"].value for i in range(1, len(out))):
        raise EditError("INVALID_REQUEST", f"{what}: words must be in time order")
    return out


def _proposal(kind: str, rule: str, words: List[Dict[str, Any]], first: int, last: int, confidence: float, reason: str) -> Dict[str, Any]:
    return {"kind": kind, "rule": rule, "start": words[first]["start"], "end": words[last]["end"], "word_range": [words[first]["index"], words[last]["index"]],
            "text": " ".join(w["text"] for w in words[first:last + 1]), "confidence": confidence, "reason": reason}


def detect_retakes(words: List[Dict[str, Any]], profile: str = DEFAULT_PROFILE, min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> Dict[str, Any]:
    if profile not in PROFILES:
        raise EditError("INVALID_REQUEST", f"profile: must be one of {sorted(PROFILES)}")
    prof = PROFILES[profile]
    cands: List[Dict[str, Any]] = []
    n = len(words)
    norm = [w["norm"] for w in words]
    for i, w in enumerate(words):
        if not norm[i]:
            continue
        # filler words
        if norm[i] in prof["fillers"]:
            cands.append(_proposal("filler", "filler", words, i, i, prof["fillers"][norm[i]], f"hesitation word {w['text']!r} ({profile} profile)"))
            continue
        # a cut-off word ("th-") or a short false start the next word begins with ("st store")
        if w["cutoff"] and i + 1 < n:
            cands.append(_proposal("fragment", "fragment_cutoff", words, i, i, CONFIDENCE["fragment_cutoff"], f"{w['text']!r} is broken off before {words[i + 1]['text']!r}"))
            continue
        if (i + 1 < n and norm[i + 1] and len(norm[i]) <= prof["fragment_max_chars"] and len(norm[i]) < len(norm[i + 1])
                and norm[i + 1].startswith(norm[i]) and norm[i] not in prof["emphatic"]):
            cands.append(_proposal("fragment", "fragment_prefix", words, i, i, CONFIDENCE["fragment_prefix"],
                                   f"{w['text']!r} is the start of the next word {words[i + 1]['text']!r} (a false start)"))
            continue
    # repeats and restarts: words[i:i+k] said again starting at j, the first saying proposed
    i = 0
    while i < n:
        best: Optional[Tuple[int, int, str]] = None
        for k in (4, 3, 2, 1):
            if i + k > n or not all(norm[i:i + k]):
                continue
            for j in range(i + k, min(n - k, i + k + RESTART_WINDOW) + 1):
                if norm[j:j + k] != norm[i:i + k]:
                    continue
                if (words[j]["start"] - words[i]["start"]).seconds > RESTART_MAX_SECONDS:
                    break
                between = [b for b in range(i + k, j) if norm[b]]
                hesitant = all(norm[b] in prof["fillers"] or words[b]["cutoff"] for b in between)
                if j == i + k:
                    rule = "repeat_phrase" if k >= 2 else ("repeat_emphatic" if norm[i] in prof["emphatic"] else "repeat_word")
                elif (k >= 2 and hesitant) or (k >= 3 and len(between) <= 3):
                    # broken off and started again: only hesitations in between, or a long opening said again after a few
                    # words ("in the morning and in the evening" repeats two words on purpose and is not a restart)
                    rule = "restart"
                else:
                    continue
                best = (k, j, rule)
                break
            if best:
                break
        if best:
            k, j, rule = best
            kind = "restart" if rule == "restart" else "repeat"
            said = " ".join(w["text"] for w in words[i:i + k])
            reason = (f"{said!r} is said again at {words[j]['start'].text(2)}s" if kind == "repeat"
                      else f"the phrase {said!r} breaks off and starts again at {words[j]['start'].text(2)}s")
            p = _proposal(kind, rule, words, i, j - 1, CONFIDENCE[rule], reason)
            p["end"] = words[j]["start"]   # up to where the kept saying begins
            cands.append(p)
            i = j
            continue
        i += 1
    return _finish(cands, min_confidence)


def _finish(cands: List[Dict[str, Any]], min_confidence: float) -> Dict[str, Any]:
    """Sort, drop what is below the threshold, merge overlaps (the higher confidence names the merged proposal)."""
    cands.sort(key=lambda c: (c["start"].value, -c["confidence"]))
    kept = [c for c in cands if c["confidence"] >= min_confidence]
    merged: List[Dict[str, Any]] = []
    for c in kept:
        if merged and c["start"].value < merged[-1]["end"].value:
            m = merged[-1]
            if c["end"].value > m["end"].value:
                m["end"] = c["end"]
            m.setdefault("also", []).append({"kind": c["kind"], "rule": c["rule"], "reason": c["reason"], "confidence": c["confidence"]})
            m["word_range"] = [min(m["word_range"][0], c["word_range"][0]), max(m["word_range"][1], c["word_range"][1])]
            continue
        merged.append(dict(c))
    out = []
    for i, m in enumerate(merged, 1):
        d = dict(m, id=f"p{i}", label="retake", action="cut", start=m["start"].to_dict(), end=m["end"].to_dict())
        out.append(d)
    return {"proposals": out, "below_threshold": sum(1 for c in cands if c["confidence"] < min_confidence), "min_confidence": min_confidence}


def to_segments(proposals: List[Dict[str, Any]], approved: List[str]) -> List[Dict[str, Any]]:
    """The approved proposals as CONDENSE segments ({start, end, label}); an unknown id is refused, never skipped."""
    by_id = {p["id"]: p for p in proposals}
    missing = [a for a in approved if a not in by_id]
    if missing:
        raise EditError("INVALID_REQUEST", f"approved: unknown proposal ids {missing}")
    return [{"start": by_id[a]["start"], "end": by_id[a]["end"], "label": by_id[a]["label"]} for a in approved]


def as_fraction(v: float) -> Fraction:
    return Fraction(repr(float(v))).limit_denominator(1000)


PROPOSE_SCHEMA = "video-editing/propose@1"
PROPOSALS_SCHEMA = "video-editing/proposals@1"


def _keys(d: Dict[str, Any], what: str, allowed: tuple, required: tuple = ()) -> None:
    if not isinstance(d, dict):
        raise EditError("INVALID_REQUEST", f"{what}: must be an object")
    extra = sorted(set(d) - set(allowed))
    if extra:
        raise EditError("INVALID_REQUEST", f"{what}: unknown keys {extra}", {"allowed": list(allowed)})
    missing = sorted(set(required) - set(d))
    if missing:
        raise EditError("INVALID_REQUEST", f"{what}: missing keys {missing}")


def _confidence(v: Any, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 <= v <= 1):
        raise EditError("INVALID_REQUEST", f"{what}: must be a number between 0 and 1")
    return float(v)


# ---- spans measured on the media (ADR-025): silence (ffmpeg-skill silence --list) and no motion (ffmpeg-skill scenes --shots)
SILENCE_DEFAULTS = {"threshold_db": -35.0, "min_seconds": 0.6}


def _spans_union(spans: List[Tuple[Fraction, Fraction]]) -> List[Tuple[Fraction, Fraction]]:
    out: List[Tuple[Fraction, Fraction]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _spans_intersection(x: List[Tuple[Fraction, Fraction]], y: List[Tuple[Fraction, Fraction]]) -> List[Tuple[Fraction, Fraction]]:
    out = []
    for a, b in x:
        for c, d in y:
            lo, hi = max(a, c), min(b, d)
            if lo < hi:
                out.append((lo, hi))
    return _spans_union(out)


def _engine_json(skill: Any, tool: str, argv: List[str], what: str) -> Dict[str, Any]:
    from .ffmpeg_skill import run_tool, tool_error
    if tool not in skill.tools:
        raise EditError("TOOL_ERROR", f"{what} needs ffmpeg-skill/{tool}, which this checkout does not provide", {"missing": [f"tool:ffmpeg-skill/{tool}"]}, retryable=False)
    missing = skill.missing_flags(tool, [a.split("=", 1)[0] for a in argv if a.startswith("--")])
    if missing:
        raise EditError("TOOL_ERROR", f"{what} needs ffmpeg-skill/{tool} {' '.join(missing)}, which this checkout does not offer",
                        {"missing": [f"flag:ffmpeg-skill/{tool} {m}" for m in missing]}, retryable=False)
    run = run_tool(skill, tool, argv, timeout=3600)
    if not run.ok or not isinstance(run.document, dict):
        raise tool_error(run, f"{what} (ffmpeg-skill/{tool})")
    return run.document


def _secs(v: Any) -> Fraction:
    return Fraction(repr(float(v))).limit_denominator(100000)


def measure_spans(skill: Any, path: str, silence: Optional[Dict[str, Any]], static: Optional[Dict[str, Any]], fps: Optional[float]) -> Dict[str, Any]:
    """Silent and motionless spans of one video, as the engine measures them, snapped to its frame grid."""
    def snap(t: Fraction) -> Fraction:
        if not fps:
            return t
        f = Fraction(fps).limit_denominator(1001)
        return Fraction(round(t * f)) / f
    out: Dict[str, Any] = {}
    if silence is not None:
        argv = [path, "--list", f"--threshold={silence['threshold_db']:g}", "--min-silence", f"{silence['min_seconds']:g}"]
        doc = _engine_json(skill, "silence", argv, "silence")
        out["silence"] = _spans_union([(snap(_secs(a)), snap(_secs(b))) for a, b in (doc.get("silences") or []) if float(b) > float(a)])
    if static is not None:
        doc = _engine_json(skill, "scenes", [path, "--shots"], "static")
        shots = doc.get("shots")
        if not isinstance(shots, list):
            raise EditError("TOOL_ERROR", "static: ffmpeg-skill/scenes --shots reported no shots", retryable=False)
        out["static"] = _spans_union([(snap(_secs(s["start"])), snap(_secs(s["end"]))) for s in shots
                                      if isinstance(s, dict) and s.get("label") == "static" and float(s["end"]) > float(s["start"])])
        out["static_shots"] = [{"start": s.get("start"), "end": s.get("end"), "label": s.get("label"), "flow_magnitude": s.get("flow_magnitude")}
                               for s in shots if isinstance(s, dict)]
    return out


def combine_spans(measured: Dict[str, Any], mode: str, min_seconds: Fraction) -> List[Dict[str, Any]]:
    """`and`: only where every asked measurement holds (silent and static); `or`: wherever any holds, labelled with all that do."""
    kinds = [k for k in ("silence", "static") if k in measured]
    if mode == "and":
        spans = measured[kinds[0]]
        for k in kinds[1:]:
            spans = _spans_intersection(spans, measured[k])
        labelled = [(a, b, kinds) for a, b in spans]
    else:
        labelled = []
        for a, b in _spans_union([s for k in kinds for s in measured[k]]):
            labels = [k for k in kinds if any(c < b and a < d for c, d in measured[k])]
            labelled.append((a, b, labels))
    out = []
    for a, b, labels in labelled:
        if b - a < min_seconds:
            continue
        label = "+".join(labels) if mode == "and" else ("+".join(labels) if len(labels) > 1 else labels[0])
        reason = " and ".join({"silence": "no sound above the threshold", "static": "no motion in the picture (a static shot)"}[k] for k in labels)
        out.append({"kind": "span", "label": label, "labels": list(labels), "action": "cut", "start": Time(a).to_dict(), "end": Time(b).to_dict(),
                    "confidence": 1.0, "reason": f"{reason}, {float(b - a):.3f}s", "rule": f"measured ({mode})"})
    return out


def propose(doc: Any, policy: Any, skill: Any = None) -> Dict[str, Any]:
    """`video-editing propose`: read a propose request, return proposals. Writes nothing, applies nothing."""
    import json
    from .canonical import sha256_file
    _keys(doc, "request", ("schema", "retakes", "source", "silence", "static", "combine"), ("schema",))
    if doc["schema"] != PROPOSE_SCHEMA:
        raise EditError("INVALID_REQUEST", f"request.schema must be {PROPOSE_SCHEMA!r}", {"got": doc["schema"]})
    inputs: List[Dict[str, Any]] = []
    proposals: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {}
    if "retakes" in doc:
        r = doc["retakes"]
        _keys(r, "retakes", ("transcript", "profile", "min_confidence"), ("transcript",))
        path = policy.resolve_input(r["transcript"], "retakes.transcript", (".json",))
        try:
            with open(path, encoding="utf-8") as fh:
                words = load_words(json.load(fh), "retakes.transcript")
        except ValueError as exc:
            raise EditError("INVALID_INPUT", f"retakes.transcript: not JSON: {exc}") from exc
        inputs.append({"role": "transcript", "path": path, "sha256": sha256_file(path)})
        profile = r.get("profile", DEFAULT_PROFILE)
        if not isinstance(profile, str):
            raise EditError("INVALID_REQUEST", "retakes.profile: must be a string")
        res = detect_retakes(words, profile, _confidence(r.get("min_confidence", DEFAULT_MIN_CONFIDENCE), "retakes.min_confidence"))
        proposals += res["proposals"]
        summary["retakes"] = {"profile": profile, "words": len(words), "proposed": len(res["proposals"]), "below_threshold": res["below_threshold"],
                              "min_confidence": res["min_confidence"]}
    if "silence" in doc or "static" in doc:
        from .ffmpeg_skill import probe
        from .paths import VIDEO_EXTENSIONS
        if "source" not in doc or not isinstance(doc["source"], str):
            raise EditError("INVALID_REQUEST", "source: silence / static measure a video: give its path")
        if skill is None:
            raise EditError("TOOL_ERROR", "silence / static need ffmpeg-skill", retryable=False)
        path = policy.resolve_input(doc["source"], "source", VIDEO_EXTENSIONS)
        sil: Optional[Dict[str, Any]] = None
        st: Optional[Dict[str, Any]] = None
        if "silence" in doc:
            _keys(doc["silence"], "silence", ("threshold_db", "min_seconds"))
            sil = dict(SILENCE_DEFAULTS)
            for k, lo, hi in (("threshold_db", -90, -5), ("min_seconds", 0.05, 3600)):
                if k in doc["silence"]:
                    v = doc["silence"][k]
                    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi):
                        raise EditError("INVALID_REQUEST", f"silence.{k}: must be a number between {lo} and {hi}")
                    sil[k] = float(v)
        if "static" in doc:
            _keys(doc["static"], "static", ())
            st = {}
        comb = doc.get("combine", {})
        _keys(comb, "combine", ("mode", "min_seconds"))
        mode = comb.get("mode", "or")
        if mode not in ("and", "or"):
            raise EditError("INVALID_REQUEST", "combine.mode: must be and | or")
        min_s = Time.parse(comb.get("min_seconds", 0), "combine.min_seconds").value
        pdoc = probe(skill, path)
        fps = ((pdoc.get("video") or {}).get("fps")) or None
        measured = measure_spans(skill, path, sil, st, fps)
        spans = combine_spans(measured, mode, min_s)
        base = len(proposals)
        for i, sp in enumerate(spans, base + 1):
            sp["id"] = f"p{i}"
        proposals += spans
        inputs.append({"role": "source", "path": path, "sha256": sha256_file(path)})
        summary["spans"] = {"mode": mode, "fps": fps, "silence": [[Time(a).to_dict(), Time(b).to_dict()] for a, b in measured.get("silence", [])] if sil is not None else None,
                            "static": [[Time(a).to_dict(), Time(b).to_dict()] for a, b in measured.get("static", [])] if st is not None else None,
                            "static_shots": measured.get("static_shots"), "proposed": len(spans), "engine": f"ffmpeg-skill@{skill.version}"}
    if not inputs:
        raise EditError("INVALID_REQUEST", "request: ask for at least one detection (retakes, silence, static)")
    return {"status": "proposed", "applied": False, "inputs": inputs, "proposals": proposals, "summary": summary,
            "next": "approve proposals by id; pass the approved ones to CONDENSE as segments (label retake -> rule cut). Nothing is cut until then."}


def check_proposals_doc(doc: Any) -> List[str]:
    p: List[str] = []
    if not isinstance(doc, dict) or doc.get("schema") != PROPOSALS_SCHEMA or doc.get("applied") is not False:
        return ["not a proposals document (schema, applied: false)"]
    for i, pr in enumerate(doc.get("proposals") or []):
        for k in ("id", "kind", "label", "action", "start", "end", "reason", "confidence"):
            if k not in pr:
                p.append(f"proposals[{i}]: missing {k}")
    return p
