"""Capability detection instead of a version window (docs/decisions.md ADR-013).

video-editing-skill must work with whichever ffmpeg-skill is installed. What differs between releases is read from the
installed checkout, never inferred from its version number:
  - CLI flags per tool: `contract --json` (input_schema.properties[].cli), else each tool's `--help`
  - --crf (0.x, 1.x) vs --quality (1.10+; the only spelling from 2.0)
  - probe's `hdr` (wide meaning before 2.0) vs `bt2020_or_hdr` (the same wide meaning from 2.0)
  - fit.py --rotate / --flip (absent from 0.10.0)
Each layout is a fake checkout (fake_engine.py `layout=`) whose tools reject a flag they do not list, so a wrong
spelling fails here exactly as it would against the real release.
"""
import json
import os
import shutil
import tempfile
import unittest

from fake_engine import make_fake_skill
from helpers import cli, make_workspace, request, write_fake_media

from video_editing_skill.compiler import OPERATION_FLAGS
from video_editing_skill.contract import ffmpeg_skill_version_range
from video_editing_skill.executor import _hdr_of
from video_editing_skill.ffmpeg_skill import MIN_VERSION, FfmpegSkill, _flags_from_contract, locate


class LayoutCase(unittest.TestCase):
    layout = "2.x"
    version = "2.10.3"

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix=f"fake-ffmpeg-skill-{cls.layout}-")
        make_fake_skill(cls.root, version=cls.version, layout=cls.layout)
        cls.env = {"VIDEO_EDITING_FFMPEG_SKILL_DIR": cls.root}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.ws = make_workspace()
        for name in ("a.mp4", "b.mp4", "bt2020.mp4", "logo.png"):
            write_fake_media(os.path.join(self.ws, "in", name))

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def run_(self, ops, src="in/a.mp4", options=None, encoding=None):
        out = {"id": "o", "operation": ops[-1]["id"], "path": "out/o.mp4"}
        if encoding:
            out["encoding"] = encoding
        doc = request([{"id": "A", "path": src}, {"id": "B", "path": "in/b.mp4"}, {"id": "logo", "path": "in/logo.png", "kind": "image"}],
                      ops, [out], options)
        rc, res, err = cli(["run", "-", "--json", "--workspace", self.ws], stdin=json.dumps(doc).encode(), env=self.env)
        self.assertIsInstance(res, dict, err)
        return rc, res

    def delivered_header(self):
        with open(os.path.join(self.ws, "out", "o.mp4"), "rb") as fh:
            return json.loads(fh.read()[4:].decode())


class Engine2xTests(LayoutCase):
    """ffmpeg-skill 2.x: --crf is an unknown argument, an existing output needs --overwrite, probe's hdr is narrower."""

    def test_detected_from_contract(self):
        skill = locate(self.root)
        self.assertTrue(skill.version_supported())   # no ceiling
        self.assertEqual(skill.cli_flags("cut") and "--quality" in skill.cli_flags("cut"), True)
        self.assertEqual(skill.flag_source["fit"], "contract")
        self.assertEqual(skill.resolve_flag("cut", "--crf"), "--quality")
        self.assertEqual(skill.resolve_flag("fit", "--rotate"), "--rotate")
        self.assertIsNone(skill.resolve_flag("fit", "--no-such-flag"))

    def test_encoding_crf_is_passed_as_quality(self):
        rc, res = self.run_([{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1, "precision": "frame"}}],
                            encoding={"crf": 23, "preset": "fast"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.delivered_header()["encoding"], {"crf": 23, "preset": "fast"})
        self.assertIn("--quality", self.delivered_header()["args"])
        self.assertNotIn("--crf", self.delivered_header()["args"])

    def test_rerun_needs_no_overwrite(self):
        """Every tool writes to a fresh .partial path the executor removes first, so 2.x's overwrite refusal never
        applies and --overwrite is never passed."""
        ops = [{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1, "precision": "frame"}}]
        for _ in range(2):
            rc, res = self.run_(ops, options={"reuse": False, "overwrite": True})
            self.assertTrue(res["ok"], res)
        self.assertNotIn("--overwrite", self.delivered_header()["args"])

    def test_plan_previews_and_still_writes_nothing(self):
        """1.x+ refuses a dry run into a directory that does not exist; plan creates the work directory for its previews
        and removes it again."""
        doc = request([{"id": "A", "path": "in/a.mp4"}], [{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1}}],
                      [{"id": "o", "operation": "t", "path": "out/o.mp4"}])
        rc, res, err = cli(["plan", "-", "--json", "--workspace", self.ws], stdin=json.dumps(doc).encode(), env=self.env)
        self.assertTrue(res["ok"], res)
        self.assertTrue(all(s["preview"]["ok"] for s in res["plan"]["steps"]), res["plan"]["steps"])
        self.assertFalse(os.path.exists(os.path.join(self.ws, ".video-editing")))
        self.assertFalse(os.path.exists(os.path.join(self.ws, "out")))

    def test_bt2020_sdr_keeps_the_wide_hdr_meaning(self):
        """2.x says hdr: false for BT.2020 SDR but still encodes it as hevc; validation reads bt2020_or_hdr and expects hevc."""
        rc, res = self.run_([{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1, "precision": "frame"}}], src="in/bt2020.mp4")
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.delivered_header()["codec"], "hevc")

    def test_doctor_reports_what_was_detected(self):
        rc, rep, err = cli(["doctor", "--json"], env=self.env)
        self.assertEqual(rc, 0, err)
        self.assertTrue(rep["ok"], rep["problems"])
        self.assertTrue(rep["engine"]["version_supported"])
        comp = rep["engine"]["compatibility"]
        self.assertIsNone(comp["upper_bound"])
        self.assertEqual(comp["version_floor"], ">=" + ".".join(map(str, MIN_VERSION)))
        self.assertEqual(comp["tools"]["cut"]["flags_from"], "contract")
        self.assertEqual(comp["tools"]["cut"]["flag_count"], len(locate(self.root).cli_flags("cut")))
        self.assertEqual(comp["tools"]["cut"]["spellings"], {"--crf": "--quality"})
        rows = {c["check"]: c for c in rep["checks"]}
        self.assertEqual(rows["tool:ffmpeg-skill/fit"]["uses_crf"], "--quality")


    def test_fit_canvas_never_exceeds_the_source(self):
        """fit.py 0.16.10+ bounds an aspect-only canvas by the source: 640x360 at 9:16 is 202x360 (before: 640x1138)."""
        for aspect, frame in (("9:16", [202, 360]), ("21:9", [640, 274]), ("1:1", [360, 360])):
            rc, res = self.run_([{"id": "f", "type": "FIT", "input": "A", "params": {"aspect": aspect}}], options={"overwrite": True})
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["execution"]["operations"][0]["normalized"]["target_frame"], frame)
            self.assertEqual([self.delivered_header()["width"], self.delivered_header()["height"]], frame)


class Engine1xTests(Engine2xTests):
    """1.10+ accepts both spellings: --quality (the non-deprecated one) is chosen; probe still has the wide hdr."""
    layout = "1.x"
    version = "1.26.0"


class Engine0xTests(LayoutCase):
    layout = "0.x"
    version = "0.12.5"

    def test_crf_kept_where_quality_does_not_exist(self):
        skill = locate(self.root)
        self.assertEqual(skill.resolve_flag("cut", "--crf"), "--crf")
        rc, res = self.run_([{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1, "precision": "frame"}}],
                            encoding={"crf": 21})
        self.assertTrue(res["ok"], res)
        self.assertIn("--crf", self.delivered_header()["args"])
        self.assertEqual(self.delivered_header()["encoding"]["crf"], 21)

    def test_bt2020_reads_old_hdr_key(self):
        rc, res = self.run_([{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1, "precision": "frame"}}], src="in/bt2020.mp4")
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.delivered_header()["codec"], "hevc")

    def test_fit_canvas_keeps_the_old_rule(self):
        """fit.py before 0.16.10 kept the source width for a narrower aspect: 640x360 at 9:16 is 640x1138."""
        for aspect, frame in (("9:16", [640, 1138]), ("21:9", [840, 360])):
            rc, res = self.run_([{"id": "f", "type": "FIT", "input": "A", "params": {"aspect": aspect}}], options={"overwrite": True})
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["execution"]["operations"][0]["normalized"]["target_frame"], frame)
            self.assertEqual([self.delivered_header()["width"], self.delivered_header()["height"]], frame)

    def test_rotate_available(self):
        rc, res = self.run_([{"id": "r", "type": "ROTATE", "input": "A", "params": {"degrees": 90}}])
        self.assertTrue(res["ok"], res)


class Engine010Tests(LayoutCase):
    """0.10.0's fit.py has no --rotate / --flip: ROTATE is MISSING in doctor and refused before anything runs; the
    other operations are unaffected."""
    layout = "0.10"
    version = "0.10.0"

    def test_rotate_refused_up_front(self):
        rc, res = self.run_([{"id": "r", "type": "ROTATE", "input": "A", "params": {"degrees": 90}}])
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "TOOL_ERROR")
        self.assertIn("flag:--rotate", res["error"]["details"]["missing"])
        self.assertFalse(res["error"]["retryable"])
        self.assertFalse(os.path.exists(os.path.join(self.ws, "out", "o.mp4")))
        work = os.path.join(self.ws, ".video-editing", "work")
        self.assertEqual([f for _, _, fs in os.walk(work) for f in fs if f.endswith(".mp4")] if os.path.isdir(work) else [], [])

    def test_doctor_marks_only_rotate_missing(self):
        rc, rep, err = cli(["doctor", "--json"], env=self.env)
        rows = {r["type"]: r for r in rep["operations"]}
        self.assertEqual(rows["ROTATE"]["status"], "MISSING")
        self.assertEqual(rows["ROTATE"]["missing"], ["flag:ffmpeg-skill/fit --rotate", "flag:ffmpeg-skill/fit --flip"])
        self.assertEqual(sorted(t for t, r in rows.items() if r["status"] == "AVAILABLE"), sorted(set(OPERATION_FLAGS) - {"ROTATE"}))

    def test_trim_still_runs(self):
        rc, res = self.run_([{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1}}])
        self.assertTrue(res["ok"], res)


class EngineHelpOnlyTests(LayoutCase):
    """A checkout whose contract describes no tools: flags are read from each tool's --help."""
    layout = "help-only"
    version = "2.10.3"

    def test_flags_from_help(self):
        skill = locate(self.root)
        self.assertEqual(skill.resolve_flag("cut", "--crf"), "--quality")
        self.assertEqual({skill.flag_source[t] for t in ("cut", "join", "fit", "overlay")}, {"help"})
        rc, res = self.run_([{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1, "precision": "frame"}}],
                            encoding={"crf": 20})
        self.assertTrue(res["ok"], res)
        self.assertIn("--quality", self.delivered_header()["args"])


class VersionPolicyTests(unittest.TestCase):
    def test_no_upper_bound(self):
        for v in ("0.10.0", "0.12.5", "1.0.0", "1.26.0", "2.0.0", "2.10.3", "3.0.0", "17.4.2", "unknown"):
            self.assertTrue(FfmpegSkill("/x", v, []).version_supported(), v)
        for v in ("0.9.1", "0.1.0"):
            self.assertFalse(FfmpegSkill("/x", v, []).version_supported(), v)
        self.assertEqual(ffmpeg_skill_version_range(), ">=0.10.0")
        self.assertFalse(FfmpegSkill("/x", "0.16.9", []).fit_bounds_by_source())
        for v in ("0.16.10", "1.26.0", "2.10.3", "unknown"):
            self.assertTrue(FfmpegSkill("/x", v, []).fit_bounds_by_source(), v)
        self.assertNotIn("<", ffmpeg_skill_version_range())

    def test_floor_refusal_names_the_reason(self):
        root = tempfile.mkdtemp(prefix="fake-old-")
        ws = make_workspace()
        try:
            make_fake_skill(root, version="0.9.1", layout="0.10")
            write_fake_media(os.path.join(ws, "in", "a.mp4"))
            doc = request([{"id": "A", "path": "in/a.mp4"}], [{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1}}],
                          [{"id": "o", "operation": "t", "path": "out/o.mp4"}])
            rc, out, err = cli(["run", "-", "--json", "--workspace", ws], stdin=json.dumps(doc).encode(), env={"VIDEO_EDITING_FFMPEG_SKILL_DIR": root})
            self.assertEqual(out["error"]["code"], "TOOL_ERROR")
            self.assertEqual(out["error"]["details"]["reason"], "below_floor")
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(ws, ignore_errors=True)

    def test_undetected_flags_pass_through(self):
        skill = FfmpegSkill("/nonexistent", "2.10.3", ["cut"])
        self.assertIsNone(skill.cli_flags("cut"))
        self.assertEqual(skill.resolve_flag("cut", "--crf"), "--crf")
        self.assertEqual(skill.missing_flags("cut", ["--rotate"]), [])

    def test_contract_parsing_ignores_malformed_entries(self):
        doc = {"tools": [None, {"id": "ffmpeg-skill/cut", "input_schema": {"properties": {"start": {"cli": ["--start"]}, "input": {"cli": "positional"},
                                                                                           "x": "junk"}}},
                         {"id": "ffmpeg-skill/join"}, {"name": "fit", "input_schema": {"properties": {"q": {"cli": ["--quality", "-q"]}}}}]}
        self.assertEqual(_flags_from_contract(doc), {"cut": frozenset({"--start"}), "fit": frozenset({"--quality"})})
        self.assertEqual(_flags_from_contract({"ok": True}), {})
        self.assertEqual(_flags_from_contract(None), {})

    def test_hdr_key(self):
        self.assertTrue(_hdr_of({"hdr": True}))                                   # 0.x / 1.x
        self.assertTrue(_hdr_of({"hdr": False, "bt2020_or_hdr": True}))           # 2.x BT.2020 SDR
        self.assertFalse(_hdr_of({"hdr": False, "bt2020_or_hdr": False}))
        self.assertFalse(_hdr_of({}))


if __name__ == "__main__":
    unittest.main()
