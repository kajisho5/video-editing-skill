"""Error contract at the engine boundary, with a fake ffmpeg-skill (see fake_engine.py): every failure class
maps to one error code, one retryable flag, one exit code, and never to a delivered output.

TOOL_ERROR       the engine failed (ffmpeg error, missing ffmpeg, could not start)
OUTPUT_ERROR     the engine reported success but wrote nothing
VALIDATION_ERROR the engine wrote a file that is not what was requested (no video stream, wrong duration)
CANCELLED        timeout / interrupt
"""
import json
import os
import shutil
import tempfile
import time
import unittest

from fake_engine import make_fake_skill, set_mode
from helpers import cli, make_workspace, request, write_fake_media

from video_editing_skill.errors import EXIT_CODES
from video_editing_skill.ffmpeg_skill import ToolRun, _candidate, locate, run_tool, tool_error


class ToolErrorMappingTests(unittest.TestCase):
    def run_(self, **kw):
        base = dict(tool="cut", argv=[], exit_code=1, document=None, stderr_tail="", seconds=0.1)
        base.update(kw)
        return ToolRun(**base)

    def test_mapping(self):
        e = tool_error(self.run_(document={"status": "failed", "error": {"kind": "ffmpeg", "message": "boom"}}), "x")
        self.assertEqual((e.code, e.retryable), ("TOOL_ERROR", True))
        e = tool_error(self.run_(exit_code=127, document={"status": "failed", "error": {"kind": "missing_tool", "message": "m"}}), "x")
        self.assertEqual((e.code, e.retryable), ("TOOL_ERROR", False))
        e = tool_error(self.run_(document={"status": "failed", "error": {"kind": "input", "message": "bad"}}), "x")
        self.assertEqual((e.code, e.retryable), ("INVALID_INPUT", False))
        e = tool_error(self.run_(timed_out=True), "x")
        self.assertEqual((e.code, e.retryable, e.details["reason"]), ("CANCELLED", True, "timeout"))
        e = tool_error(self.run_(interrupted=True), "x")
        self.assertEqual(e.code, "CANCELLED")
        e = tool_error(self.run_(exit_code=3, stderr_tail="segfault"), "x")
        self.assertEqual((e.code, e.retryable), ("TOOL_ERROR", True))
        self.assertIn("segfault", e.message)


class FakeEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="fake-ffmpeg-skill-")
        make_fake_skill(cls.root)
        cls.env = {"VIDEO_EDITING_FFMPEG_SKILL_DIR": cls.root}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        set_mode(self.root, "ok")
        self.ws = make_workspace()
        write_fake_media(os.path.join(self.ws, "in", "a.mp4"))
        write_fake_media(os.path.join(self.ws, "in", "b.mp4"))
        write_fake_media(os.path.join(self.ws, "in", "logo.png"))

    def doc(self, ops=None, outputs=None, options=None):
        return request([{"id": "A", "path": "in/a.mp4"}, {"id": "B", "path": "in/b.mp4"}, {"id": "logo", "path": "in/logo.png", "kind": "image"}],
                       ops or [{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1}}],
                       outputs or [{"id": "o", "operation": "t", "path": "out/o.mp4"}], options)

    def run_(self, doc, cmd="run"):
        rc, out, err = cli([cmd, "-", "--json", "--workspace", self.ws], stdin=json.dumps(doc).encode(), env=self.env)
        self.assertIsInstance(out, dict, err)
        return rc, out

    def assert_failed(self, doc, code, retryable, reason=None):
        rc, out = self.run_(doc)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], code, out["error"])
        self.assertEqual(out["error"]["retryable"], retryable)
        self.assertEqual(rc, EXIT_CODES[code])
        if reason:
            self.assertEqual(out["error"]["details"].get("reason"), reason)
        self.assertFalse(os.path.exists(os.path.join(self.ws, "out", "o.mp4")), "a failed run must not deliver")
        work = os.path.join(self.ws, ".video-editing", "work")
        leftovers = [f for _, _, fs in os.walk(work) for f in fs if ".partial" in f]
        self.assertEqual(leftovers, [], "no partial file may survive a failure")
        return out

    def test_ok_delivers_with_provenance(self):
        rc, out = self.run_(self.doc())
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["status"], "completed")
        rec = out["execution"]["operations"][0]
        self.assertEqual(rec["status"], "completed")
        self.assertEqual(rec["tool_versions"], {"ffmpeg-skill": "0.9.0", "ffmpeg": "fake-6.0", "ffprobe": "fake-6.0"})
        self.assertTrue(os.path.isfile(out["execution"]["outputs"][0]["path"]))
        self.assertTrue(out["execution"]["outputs"][0]["delivered"])
        # the engine flags are the compiled ones, nothing more
        self.assertEqual(rec["parameters"], {"start": "0.000000", "end": "1.000000", "accurate": True})

    def test_ffmpeg_failure_is_tool_error(self):
        set_mode(self.root, "ffmpeg_fail")
        out = self.assert_failed(self.doc(), "TOOL_ERROR", True)
        self.assertEqual(out["execution"]["operations"][0]["status"], "failed")
        self.assertIn("ffmpeg", out["error"]["message"])

    def test_missing_ffmpeg_is_tool_error_not_retryable(self):
        set_mode(self.root, "missing_tool")
        self.assert_failed(self.doc(), "TOOL_ERROR", False)

    def test_engine_doctor_not_ready_is_tool_error(self):
        set_mode(self.root, "no_ffmpeg")
        out = self.assert_failed(self.doc(), "TOOL_ERROR", False)
        self.assertEqual(out["error"]["details"].get("ffmpeg_skill_error"), "missing_tool")
        rc, rep, _ = cli(["doctor", "--json", "--workspace", self.ws], env=self.env)
        self.assertEqual(rc, 1)
        self.assertFalse(rep["ok"])

    def test_success_without_file_is_output_error(self):
        set_mode(self.root, "no_output")
        self.assert_failed(self.doc(), "OUTPUT_ERROR", False)

    def test_file_without_video_stream_is_validation_error(self):
        set_mode(self.root, "bad_probe")
        self.assert_failed(self.doc(), "VALIDATION_ERROR", False)

    def test_wrong_duration_is_validation_error(self):
        set_mode(self.root, "short")
        out = self.assert_failed(self.doc(), "VALIDATION_ERROR", False)
        self.assertIn("expected", out["error"]["message"])

    def test_timeout_is_cancelled(self):
        set_mode(self.root, "hang")
        t0 = time.time()
        self.assert_failed(self.doc(options={"timeout_seconds": 1}), "CANCELLED", True, reason="timeout")
        self.assertLess(time.time() - t0, 20)

    def test_noisy_stdout_still_parsed(self):
        set_mode(self.root, "noisy")
        rc, out = self.run_(self.doc())
        self.assertEqual(rc, 0, out)

    def test_plan_writes_nothing(self):
        rc, out = self.run_(self.doc(), cmd="plan")
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["dry_run"])
        self.assertFalse(os.path.exists(os.path.join(self.ws, "out")))
        self.assertFalse(os.path.exists(os.path.join(self.ws, ".video-editing")), "plan must not even create the work dir")
        self.assertEqual(out["plan"]["steps"][0]["preview"]["ok"], True)

    def test_reuse_and_invalidation(self):
        doc = self.doc(ops=[{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1}},
                            {"id": "c", "type": "CONCAT", "inputs": ["t", "B"], "params": {}}],
                       outputs=[{"id": "o", "operation": "c", "path": "out/o.mp4"}])
        rc, out1 = self.run_(doc)
        self.assertEqual(rc, 0, out1)
        doc["project"]["outputs"][0]["path"] = "out/o2.mp4"
        rc, out2 = self.run_(doc)
        self.assertEqual(out2["status"], "reused")
        self.assertEqual(out1["execution"]["outputs"][0]["sha256"], out2["execution"]["outputs"][0]["sha256"])
        doc["project"]["operations"][0]["params"]["end"] = 1.5
        doc["project"]["outputs"][0]["path"] = "out/o3.mp4"
        rc, out3 = self.run_(doc)
        self.assertEqual([r["status"] for r in out3["execution"]["operations"]], ["completed", "completed"])
        doc["options"] = {"reuse": False}
        doc["project"]["outputs"][0]["path"] = "out/o4.mp4"
        rc, out4 = self.run_(doc)
        self.assertEqual([r["status"] for r in out4["execution"]["operations"]], ["completed", "completed"])

    def test_tampered_intermediate_is_not_reused(self):
        doc = self.doc()
        rc, out1 = self.run_(doc)
        inter = out1["execution"]["operations"][0]["output"]["path"]
        with open(inter, "ab") as fh:
            fh.write(b"x")
        doc["project"]["outputs"][0]["path"] = "out/o2.mp4"
        rc, out2 = self.run_(doc)
        self.assertEqual(out2["execution"]["operations"][0]["status"], "completed")

    def test_unsupported_engine_version(self):
        other = tempfile.mkdtemp(prefix="fake-old-")
        try:
            make_fake_skill(other, version="0.8.4")
            rc, out, _ = cli(["run", "-", "--json", "--workspace", self.ws], stdin=json.dumps(self.doc()).encode(),
                             env={"VIDEO_EDITING_FFMPEG_SKILL_DIR": other})
            self.assertEqual(out["error"]["code"], "TOOL_ERROR")
            self.assertFalse(out["error"]["retryable"])
            self.assertEqual(rc, EXIT_CODES["TOOL_ERROR"])
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_locate_needs_probe_script(self):
        empty = tempfile.mkdtemp(prefix="not-a-skill-")
        try:
            self.assertIsNone(_candidate(empty))  # a directory without scripts/probe.py is not an engine
        finally:
            shutil.rmtree(empty, ignore_errors=True)
        skill = locate(self.root)
        self.assertEqual(skill.missing_tools(), [])
        run = run_tool(skill, "probe", [os.path.join(self.ws, "in", "a.mp4")], timeout=30)
        self.assertTrue(run.ok)


if __name__ == "__main__":
    unittest.main()
