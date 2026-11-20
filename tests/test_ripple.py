"""`video-editing ripple` (docs/decisions.md ADR-042, Velisa ADR-032): cut edges placed in their windows, on silence and on frames;
the timeline after the cut; the transcript and subtitles retimed; a CONDENSE request for `run`.

Offline: the conformance cases in tests/fixtures/ripple_conformance/cases.json run against place_cuts / Timeline, and the
command runs on a placeholder file with `fps`, `duration` and `silence: false` (no engine). The real-media run is in
test_integration.py (RippleTests)."""
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from fractions import Fraction

from helpers import make_workspace, write_fake_media

from video_editing_skill import cli
from video_editing_skill.contract import skill_contract
from video_editing_skill.errors import EditError
from video_editing_skill.paths import PathPolicy
from video_editing_skill.project import parse_request
from video_editing_skill.ripple import (MARGIN_DEFAULTS, RIPPLE_SCHEMA, RULE_VERSION, Timeline, check_ripple_doc, parse_spans, place_cuts,
                                        retime_subtitles, ripple)
from video_editing_skill.timebase import frames_at, parse_rate

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = json.load(open(os.path.join(HERE, "fixtures", "ripple_conformance", "cases.json"), encoding="utf-8"))


def selection(spans, sentences=None):
    return {"rule": {"version": "transcript-selection@1"}, "mode": "remove", "transcript_id": "tr", "spans": spans,
            "sentences": sentences or [], "speech_seconds": 0.2}


def transcript():
    words = [("We", 0.5, 0.7), ("measured", 0.8, 1.3), ("um", 1.5, 1.7), ("the", 2.0, 2.1), ("noise.", 2.15, 2.6)]
    seg1 = {"id": "seg_0001", "start": 0.5, "end": 2.6, "text": "We measured um the noise.", "raw_text": "We measured um the noise.",
            "confidence": 0.9, "speaker_id": None, "words": [{"start": s, "end": e, "text": t, "confidence": 0.9, "index": i}
                                                          for i, (t, s, e) in enumerate(words)]}
    seg2 = {"id": "seg_0002", "start": 4.0, "end": 5.5, "text": "[applause]", "raw_text": "[applause]", "confidence": 0.9, "speaker_id": None, "words": None}
    seg3 = {"id": "seg_0003", "start": 6.5, "end": 7.6, "text": "Now results.", "raw_text": "Now results.", "confidence": 0.9, "speaker_id": None,
            "words": [{"start": 6.5, "end": 6.8, "text": "Now", "confidence": 0.9, "index": 5}, {"start": 6.9, "end": 7.6, "text": "results.", "confidence": 0.9, "index": 6}]}
    return {"schema": "transcription-skill/transcript/0.1", "id": "tr", "language": "en", "duration": 10.0, "segments": [seg1, seg2, seg3],
            "source": {"filename": "talk.mp4", "media_duration": 10.0}, "warnings": []}


SPANS = [{"id": "r1", "start": 1.5, "end": 1.7, "in": {"lo": 1.3, "hi": 1.5, "after_kept": True}, "out": {"lo": 1.7, "hi": 2.0, "before_kept": True},
          "text": "um", "speaker_ids": ["SPEAKER_00"], "segment_ids": ["seg_0001"]},
         {"id": "r2", "start": 4.0, "end": 5.5, "in": {"lo": 2.6, "hi": 4.0, "after_kept": True}, "out": {"lo": 5.5, "hi": 6.5, "before_kept": True},
          "text": "[applause]", "segment_ids": ["seg_0002"]}]


class ConformanceTests(unittest.TestCase):
    def test_cases(self):
        self.assertEqual(CASES["rule"], RULE_VERSION)
        for case in CASES["cases"]:
            with self.subTest(case=case["name"]):
                rate = parse_rate(case["fps"])
                total = frames_at(Fraction(case["duration"]), rate, "nearest")
                sil = [(Fraction(str(a)), Fraction(str(b))) for a, b in case["silences"]]
                cuts, dropped = place_cuts(parse_spans(case["spans"]), rate, total, dict(MARGIN_DEFAULTS), sil)
                self.assertEqual([[c["in_frame"], c["out_frame"]] for c in cuts], case["cuts"])
                self.assertEqual([i for d in dropped for i in d["span_ids"]], case["dropped"])
                self.assertEqual(Timeline(cuts, rate, total).record_frames, case["record_frames"])


class TimelineTests(unittest.TestCase):
    def test_positions_are_counted_in_frames(self):
        rate = parse_rate("30000/1001")
        cuts = [{"in_frame": 10 * k + 3, "out_frame": 10 * k + 8} for k in range(200)]
        tl = Timeline(cuts, rate, 2000)
        self.assertEqual(tl.record_frames, 1000)
        self.assertEqual(tl.kept[-1][2], 998)   # record in of the last kept range: 3 + 199 x 5, counted, not summed seconds
        self.assertEqual(tl.map(Fraction(1995) / rate, "start"), Fraction(998) / rate)   # inside the last cut: its join
        self.assertEqual(tl.map(Fraction(1999) / rate, "start"), Fraction(999) / rate)

    def test_map_inside_a_cut(self):
        rate = Fraction(25)
        tl = Timeline([{"in_frame": 25, "out_frame": 50}], rate, 100)
        self.assertEqual(tl.map(Fraction(3, 2), "start"), Fraction(1))   # 1.5 s is cut: it maps to the join at 1.0 s
        self.assertEqual(tl.map(Fraction(3), "end"), Fraction(2))
        self.assertTrue(tl.removed(Fraction(3, 2)))
        self.assertFalse(tl.removed(Fraction(2)))

    def test_subtitles(self):
        tl = Timeline([{"in_frame": 25, "out_frame": 50}], Fraction(25), 250)
        srt = "1\n00:00:00,500 --> 00:00:01,200\nfirst\n\n2\n00:00:01,200 --> 00:00:01,800\ngone\n\n3\n00:00:01,500 --> 00:00:03,000\nlate <i>x</i>\n"
        out, c = retime_subtitles(srt, ".srt", tl)
        self.assertEqual(out, "1\n00:00:00,500 --> 00:00:01,000\nfirst\n\n2\n00:00:01,000 --> 00:00:02,000\nlate <i>x</i>\n")
        self.assertEqual(c, {"cues": 3, "dropped": 1, "shortened": 2})
        vtt = "WEBVTT\n\nNOTE kept\n\nc1\n00:02.000 --> 00:03.000 align:start\nhello\n"
        out, c = retime_subtitles(vtt, ".vtt", tl)
        self.assertEqual(out, "WEBVTT\n\nNOTE kept\n\nc1\n00:00:01.000 --> 00:00:02.000 align:start\nhello\n")
        with self.assertRaises(EditError):
            retime_subtitles("1\n00:00:05,000 --> 00:00:01,000\nbad\n", ".srt", tl)


class RippleCommandTests(unittest.TestCase):
    def setUp(self):
        self.ws = make_workspace(self)
        self.src = write_fake_media(os.path.join(self.ws, "in", "talk.mp4"))
        self.tr = os.path.join(self.ws, "in", "talk.transcript.json")
        with open(self.tr, "w", encoding="utf-8") as fh:
            json.dump(transcript(), fh)
        self.srt = os.path.join(self.ws, "in", "talk.srt")
        with open(self.srt, "w", encoding="utf-8") as fh:
            fh.write("1\n00:00:00,500 --> 00:00:02,600\nWe measured um the noise.\n\n2\n00:00:04,000 --> 00:00:05,500\n[applause]\n\n"
                     "3\n00:00:06,500 --> 00:00:07,600\nNow results.\n")
        self.policy = PathPolicy(self.ws, [os.path.join(self.ws, "in")])

    def req(self, **kw):
        doc = {"schema": RIPPLE_SCHEMA, "source": self.src, "selection": selection(SPANS), "fps": "25", "duration": "10", "silence": False,
               "transcript": self.tr, "subtitles": [self.srt]}
        doc.update(kw)
        return doc

    def test_cuts_edl_retimed_files_and_edit_request(self):
        res = ripple(self.req(), self.policy)
        self.assertFalse(res["applied"])
        self.assertEqual([(c["source_in"]["frame"], c["source_out"]["frame"]) for c in res["cuts"]], [(35, 48), (70, 160)])
        self.assertEqual(res["record"]["frames"], 250 - 13 - 90)
        self.assertEqual(res["cuts"][0]["speaker_ids"], ["SPEAKER_00"])
        # the EDL: events counted in frames, every record_in the previous record_out
        with open(res["edl"], encoding="utf-8") as fh:
            edl = json.load(fh)
        self.assertEqual(edl["schema"], "video-editing/ripple-edl@1")
        self.assertEqual(edl["producer"]["skill_id"], "video-editing")
        self.assertEqual([i["role"] for i in edl["inputs"]], ["source", "transcript", "subtitles"])
        self.assertEqual([(e["source_in"]["frame"], e["source_out"]["frame"], e["record_in"]["frame"]) for e in edl["events"]],
                         [(0, 35, 0), (48, 70, 35), (160, 250, 57)])
        # the transcript: um and the applause removed, indices renumbered, times on the record timeline
        with open(res["transcript"]["path"], encoding="utf-8") as fh:
            t = json.load(fh)
        self.assertEqual([s["id"] for s in t["segments"]], ["seg_0001", "seg_0003"])
        self.assertEqual(t["segments"][0]["text"], "We measured the noise.")
        self.assertEqual([w["index"] for s in t["segments"] for w in s["words"]], [0, 1, 2, 3, 4, 5])
        self.assertAlmostEqual(t["segments"][0]["words"][2]["start"], 2.0 - 13 / 25, places=6)
        self.assertAlmostEqual(t["segments"][1]["words"][0]["start"], 6.5 - 103 / 25, places=6)
        self.assertAlmostEqual(t["duration"], 147 / 25, places=6)
        self.assertEqual(t["source"]["media_duration"], t["duration"])
        self.assertIn("retimed by video-editing-skill ripple", t["warnings"][-1])
        with open(res["subtitles"][0]["path"], encoding="utf-8") as fh:
            srt = fh.read()
        self.assertEqual(srt, "1\n00:00:00,500 --> 00:00:02,080\nWe measured um the noise.\n\n2\n00:00:02,380 --> 00:00:03,480\nNow results.\n")
        # the CONDENSE request is a valid request of this Skill, in frames of the source's rate
        project = parse_request(res["edit_request"], self.policy)
        self.assertEqual([o.type for o in project.operations.values()], ["CONDENSE"])
        segs = res["edit_request"]["project"]["operations"][0]["params"]["segments"]
        self.assertEqual(segs[0], {"start": {"frames": 35, "fps": "25"}, "end": {"frames": 48, "fps": "25"}, "label": "transcript"})
        doc = dict(res, ok=True, schema="video-editing/ripple-result@1")
        self.assertEqual(check_ripple_doc(doc), [])

    def test_plain_spans_and_margins(self):
        plain = {k: v for k, v in self.req(spans=[{"start": 1.5, "end": 1.7}]).items() if k not in ("selection", "transcript", "subtitles")}
        res = ripple(plain, self.policy)
        self.assertEqual([(c["source_in"]["frame"], c["source_out"]["frame"]) for c in res["cuts"]], [(38, 43)])   # 37.5 -> 38, 42.5 -> 43
        res = ripple(self.req(margins={"after_kept": 0.05, "before_kept": 0.05}, outputs={"edl": "ripple/m.json", "transcript": "ripple/m.t.json",
                                                                                           "subtitles": ["ripple/m.srt"]}), self.policy)
        self.assertEqual(res["cuts"][0]["source_in"]["frame"], 34)   # 1.30 + 0.05 = 1.35 -> 33.75 -> 34
        self.assertEqual(res["cuts"][0]["source_out"]["frame"], 49)  # 2.00 - 0.05 = 1.95 -> 48.75 -> 49

    def test_refusals(self):
        bad = [
            (dict(schema="video-editing/ripple@2"), "INVALID_REQUEST"),
            (dict(spans=[{"start": 1, "end": 2}]), "INVALID_REQUEST"),                                   # selection and spans
            (dict(selection={"rule": {"version": "other@1"}, "spans": SPANS}), "INVALID_INPUT"),
            (dict(selection=selection([{"start": 9.5, "end": 10.5}])), "INVALID_INPUT"),                 # past the end
            (dict(selection=selection([{"start": 0, "end": 10}])), "INVALID_INPUT"),                     # nothing kept
            (dict(selection=selection([{"start": 2, "end": 1}])), "INVALID_REQUEST"),
            (dict(selection=selection([{"start": 1, "end": 2}, {"start": 1.5, "end": 3}])), "INVALID_REQUEST"),   # overlap
            (dict(margins={"after_kept": 5}), "INVALID_REQUEST"),
            (dict(silence={"threshold_db": 0}), "INVALID_REQUEST"),
            (dict(fps="abc"), "INVALID_INPUT"),
            (dict(extra=1), "INVALID_REQUEST"),
            (dict(outputs={"edl": "../out.json"}), "PATH_NOT_ALLOWED"),
            (dict(subtitles=[self.tr]), "UNSUPPORTED_FORMAT"),
        ]
        for patch, code in bad:
            with self.subTest(patch=list(patch)):
                with self.assertRaises(EditError) as cm:
                    ripple(self.req(**patch), self.policy)
                self.assertEqual(cm.exception.code, code)
        ripple(self.req(), self.policy)
        with self.assertRaises(EditError) as cm:   # the EDL exists: not replaced without overwrite
            ripple(self.req(), self.policy)
        self.assertEqual(cm.exception.code, "PATH_NOT_ALLOWED")
        ripple(self.req(), self.policy, overwrite=True)
        with self.assertRaises(EditError):   # silence (the default) or a probe needs the engine
            ripple(self.req(silence={"threshold_db": -40}), self.policy, overwrite=True)

    def test_cli_without_engine(self):
        path = os.path.join(self.ws, "in", "req.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.req(), fh)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(["ripple", path, "--json", "--workspace", self.ws, "--allowed-input", os.path.join(self.ws, "in")])
        self.assertEqual(code, 0)
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["command"], "ripple")
        self.assertEqual(doc["schema"], "video-editing/ripple-result@1")
        self.assertEqual(len(doc["cuts"]), 2)

    def test_contract_block_is_additive(self):
        c = skill_contract()
        r = c["ripple"]
        self.assertEqual(r["request_schema"], RIPPLE_SCHEMA)
        self.assertEqual(r["rule"]["version"], RULE_VERSION)
        self.assertEqual(r["invocation"][:2], ["video-editing", "ripple"])
        self.assertNotIn("RIPPLE", c["operations"])
        self.assertEqual(c["contract_version"], "7.0")


if __name__ == "__main__":
    unittest.main()
