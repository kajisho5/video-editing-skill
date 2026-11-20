"""The Velisa timebase spec's conformance vectors (velisa/timebase@1, ADR-040), run against this Skill's own functions.

tests/fixtures/velisa/timebase-vectors.json is copied unchanged from the Velisa repository at the commit recorded in
tests/fixtures/velisa/provenance.json. Every vector whose `fn` this Skill offers must give the stated result or error
code; the timecode, seek, join and parse_seconds functions are not offered here (this Skill writes no timecode, and its
joins and times are covered by their own tests), so their vectors are counted and skipped, never weakened.
"""
import hashlib
import json
import os
import unittest
from fractions import Fraction

from video_editing_skill import timebase
from video_editing_skill.proposals import measure_spans
from video_editing_skill.timebase import RateError, Time

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "velisa")
OFFERED = {
    "parse_rate": timebase.parse_rate, "rate_str": timebase.rate_str, "nominal": timebase.nominal,
    "same_rate": timebase.same_rate, "frames_at": timebase.frames_at, "seconds_of": timebase.seconds_of,
    "format_seconds": timebase.format_seconds, "transition_frames": timebase.transition_frames,
}
NOT_OFFERED = {"parse_seconds", "seek_seconds", "join_positions", "timecode_from_frames", "frames_from_timecode",
               "timecode_to_seconds", "seconds_to_timecode"}


def _out(value):
    if isinstance(value, Fraction):
        return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"
    return value


def _load():
    with open(os.path.join(FIXTURES, "timebase-vectors.json"), "rb") as fh:
        raw = fh.read()
    return raw, json.loads(raw.decode("utf-8"))


class VectorFileTests(unittest.TestCase):
    def test_the_copy_is_the_recorded_velisa_file(self):
        raw, doc = _load()
        with open(os.path.join(FIXTURES, "provenance.json"), encoding="utf-8") as fh:
            rec = json.load(fh)["files"]["timebase-vectors.json"]
        self.assertEqual(rec["repository"], "kajisho5/Velisa")
        self.assertRegex(rec["commit"], r"^[0-9a-f]{40}$")
        # a Windows checkout may turn the line ends into CRLF; the recorded hash is of the file as Velisa has it (LF)
        self.assertEqual(hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(), rec["sha256"], "the vectors were edited after they were copied")
        self.assertEqual(doc["spec"], rec["spec"])

    def test_every_function_is_either_offered_or_listed_as_not_offered(self):
        _raw, doc = _load()
        fns = {v["fn"] for v in doc["vectors"]}
        self.assertEqual(fns - set(OFFERED) - NOT_OFFERED, set(), "a new function in the spec: offer it or list it")


class ConformanceTests(unittest.TestCase):
    def test_vectors(self):
        _raw, doc = _load()
        ran = 0
        for vec in doc["vectors"]:
            if vec["fn"] not in OFFERED:
                continue
            with self.subTest(vec["id"]):
                fn = OFFERED[vec["fn"]]
                if vec.get("optional") and vec["fn"] == "parse_rate" and str(vec["args"]["value"]).strip().lower()[-1:] in ("p", "i"):
                    with self.assertRaises(RateError):   # the optional p / i suffix is not offered: refused, never misread
                        fn(**vec["args"])
                    continue
                ran += 1
                if "error" in vec:
                    with self.assertRaises(RateError) as cm:
                        fn(**vec["args"])
                    self.assertEqual(cm.exception.code, vec["error"])
                else:
                    self.assertEqual(_out(fn(**vec["args"])), vec["expect"])
        self.assertGreaterEqual(ran, 70)


class SkillReadsRatesBySpecTests(unittest.TestCase):
    """video-editing-skill#59: the places the Skill read a rate with limit_denominator(1001) or rounded with round()."""

    def test_probe_fps_is_ntsc_not_2997_over_100(self):
        self.assertEqual(timebase.parse_rate(29.97), Fraction(30000, 1001))
        self.assertEqual(timebase.parse_rate(59.94), Fraction(60000, 1001))
        self.assertNotEqual(timebase.parse_rate(29.97), Fraction(29.97).limit_denominator(1001))

    def test_rates_compare_exactly(self):
        self.assertTrue(timebase.same_rate(23.976, "23.98"))
        self.assertFalse(timebase.same_rate(29.97, 30))
        self.assertFalse(timebase.same_rate(29.98, "30000/1001"), "0.01 off was the same rate under the 0.02 tolerance")

    def test_probed_rate_reads_a_rate_at_the_probe_precision(self):
        self.assertEqual(timebase.probed_rate("30000/1001"), Fraction(30000, 1001))
        self.assertEqual(timebase.probed_rate(Fraction(7, 3)), timebase.parse_rate(2.333))

    def test_time_text_rounds_a_half_up(self):
        self.assertEqual(Time(Fraction(1, 2_000_000)).text(6), "0.000001")
        self.assertEqual(Time(Fraction(5, 2_000_000)).text(6), "0.000003", "round() gave the even 0.000002")

    def test_frames_with_an_ntsc_decimal_rate(self):
        self.assertEqual(Time.parse({"frames": 300, "fps": "29.97"}).value, Fraction(1001, 100))

    def test_a_huge_exponent_is_refused_before_it_is_built(self):
        for text in ("1e1000000000", "1" * 65):
            with self.assertRaises(RateError):
                timebase.parse_rate(text)
        self.assertEqual(timebase.parse_rate("2.5e1"), Fraction(25))

    def test_a_span_snapped_to_one_instant_is_dropped(self):
        import video_editing_skill.proposals as proposals
        orig = proposals._engine_json
        proposals._engine_json = lambda skill, tool, argv, what: {"silences": [[0.05, 0.10], [0.2, 0.5]]}
        try:
            out = measure_spans(object(), "x.mp4", {"threshold_db": -40, "min_seconds": 0.1}, None, 10.0)
            self.assertEqual(out["silence"], [(Fraction(1, 5), Fraction(1, 2))], "[0.05, 0.10] at 10 fps snaps to [0.1, 0.1]")
        finally:
            proposals._engine_json = orig

    def test_proposal_spans_snap_a_half_up_to_the_ntsc_grid(self):
        class Skill:
            pass
        import video_editing_skill.proposals as proposals

        def fake(skill, tool, argv, what):
            # 0.35 s at 30 fps is frame 10.5: a half, up to 11 (round() took it to the even 10)
            return {"silences": [[0.35, 1.0]]}
        orig = proposals._engine_json
        proposals._engine_json = fake
        try:
            out = measure_spans(Skill(), "x.mp4", {"threshold_db": -40, "min_seconds": 0.1}, None, 30.0)
            self.assertEqual(out["silence"][0][0], Fraction(11, 30))
            out = measure_spans(Skill(), "x.mp4", {"threshold_db": -40, "min_seconds": 0.1}, None, 29.97)
            self.assertEqual(out["silence"][0][1], Fraction(30 * 1001, 30000), "1.0 s is frame 30 of 30000/1001 (29.97)")
        finally:
            proposals._engine_json = orig


if __name__ == "__main__":
    unittest.main()
