"""Real-media integration: needs ffmpeg/ffprobe on PATH and an ffmpeg-skill checkout
(VIDEO_EDITING_FFMPEG_SKILL_DIR or a default location). Skipped, never faked, when they are missing.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import FFMPEG_SKILL, HAVE_FFMPEG, cli, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
from generate import build_all  # noqa: E402

from video_editing_skill.canonical import sha256_file  # noqa: E402

READY = HAVE_FFMPEG and FFMPEG_SKILL is not None and FFMPEG_SKILL.version_supported() and not FFMPEG_SKILL.missing_tools()
REASON = "needs ffmpeg/ffprobe on PATH and a supported ffmpeg-skill checkout (VIDEO_EDITING_FFMPEG_SKILL_DIR)"


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path], stdout=subprocess.PIPE, text=True).stdout
    return json.loads(out)


def duration(path) -> float:
    return float(probe(path)["format"]["duration"])


def size(path):
    v = next(s for s in probe(path)["streams"] if s["codec_type"] == "video")
    return v["width"], v["height"]


@unittest.skipUnless(READY, REASON)
class RealMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ves-int-")
        cls.media = build_all(os.path.join(cls.root, "media"))
        cls.env = {"VIDEO_EDITING_FFMPEG_SKILL_DIR": FFMPEG_SKILL.root}

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        self.before = {k: sha256_file(v) for k, v in self.media.items()}

    def tearDown(self):
        for k, v in self.media.items():
            self.assertEqual(sha256_file(v), self.before[k], f"fixture {k} was modified")

    def sources(self):
        m = self.media
        return [{"id": "A", "path": m["a"]}, {"id": "B", "path": m["b"]}, {"id": "logo", "path": m["logo"], "kind": "image"}]

    def run_cli(self, cmd, doc, expect=0, extra=()):
        rc, out, err = cli([cmd, "-", "--json", "--workspace", self.ws, "--allowed-input", os.path.dirname(self.media["a"]), *extra],
                           stdin=json.dumps(doc).encode(), env=self.env)
        self.assertIsInstance(out, dict, err)
        self.assertEqual(rc, expect, json.dumps(out.get("error"), indent=1) + err)
        return out

    def out(self, name="out/final.mp4"):
        return os.path.join(self.ws, name)

    # ------------------------------------------------------------ operations
    def test_trim_frame_accurate(self):
        doc = request(self.sources(), [{"id": "t", "type": "TRIM", "input": "A", "params": {"start": "1.5", "end": "4"}}],
                      [{"id": "o", "operation": "t", "path": "out/final.mp4"}])
        res = self.run_cli("run", doc)
        self.assertEqual(res["status"], "completed")
        self.assertAlmostEqual(duration(self.out()), 2.5, delta=0.15)
        op = res["execution"]["operations"][0]
        for k in ("skill", "skill_version", "tool", "tool_versions", "operation_id", "type", "inputs", "output", "parameters", "started_at", "finished_at", "status", "commands"):
            self.assertIn(k, op)
        self.assertEqual(op["inputs"][0]["sha256"], self.before["a"])
        self.assertEqual(op["provenance"], "OBSERVED")
        o = res["execution"]["outputs"][0]
        self.assertEqual(o["sha256"], sha256_file(self.out()))
        self.assertEqual(o["observation"]["provenance"], "OBSERVED")
        seg = o["timeline"]["tracks"][0]["segments"][0]
        self.assertEqual(seg["source_range"]["start"]["rational"], "3/2")
        self.assertEqual(seg["timeline_range"]["end"]["rational"], "5/2")

    def test_cut_and_reorder(self):
        doc = request(self.sources(), [{"id": "c", "type": "CUT", "input": "A", "params": {"keep": [{"start": 4, "end": 5}, {"start": 0, "end": 2}]}}],
                      [{"id": "o", "operation": "c", "path": "out/cut.mkv"}])
        self.run_cli("run", doc)
        self.assertAlmostEqual(duration(self.out("out/cut.mkv")), 3.0, delta=0.3)

    def test_concat_reorder_transition(self):
        doc = request(self.sources(), [{"id": "c", "type": "CONCAT", "inputs": ["B", "A"], "params": {"transition": {"type": "fade", "duration": 1}, "width": 640, "height": 360, "fps": 30}}],
                      [{"id": "o", "operation": "c", "path": "out/final.mov"}])
        res = self.run_cli("run", doc)
        self.assertAlmostEqual(duration(self.out("out/final.mov")), 6 + 5 - 1, delta=0.3)
        self.assertEqual(size(self.out("out/final.mov")), (640, 360))
        segs = res["execution"]["outputs"][0]["timeline"]["tracks"][0]["segments"]
        self.assertEqual([s["source"] for s in segs], ["B", "A"])
        self.assertEqual(segs[1]["timeline_range"]["start"]["rational"], "4/1")

    def test_fill_resize_fit(self):
        doc = request(self.sources(), [{"id": "f", "type": "FILL", "input": "B", "params": {"aspect": "1:1", "width": 360}},
                                       {"id": "r", "type": "RESIZE", "input": "f", "params": {"width": 180}},
                                       {"id": "p", "type": "FIT", "input": "r", "params": {"aspect": "16:9", "width": 320, "pad_color": "0x101010"}}],
                      [{"id": "o", "operation": "p", "path": "out/final.mp4"}])
        self.run_cli("run", doc)
        self.assertEqual(size(self.out()), (320, 180))

    def test_speed_and_overlay(self):
        doc = request(self.sources(), [{"id": "s", "type": "SPEED", "input": "A", "params": {"factor": 2}},
                                       {"id": "l", "type": "OVERLAY", "input": "s", "params": {"image": "logo", "position": {"x": -10, "y": 10}, "start": 0, "end": 1, "opacity": 0.8, "scale": 60}}],
                      [{"id": "o", "operation": "l", "path": "out/final.mp4"}])
        self.run_cli("run", doc)
        self.assertAlmostEqual(duration(self.out()), 3.0, delta=0.2)

    def test_pipeline_e2e(self):
        """source A -> trim -> fill/resize -> + source B (trim) -> concat -> output validation."""
        doc = request(self.sources(), [
            {"id": "trimA", "type": "TRIM", "input": "A", "params": {"start": 1, "end": 3.5}},
            {"id": "fillA", "type": "FILL", "input": "trimA", "params": {"aspect": "16:9", "width": 640}},
            {"id": "trimB", "type": "TRIM", "input": "B", "params": {"start": {"frames": 25, "fps": 25}, "end": 4}},
            {"id": "cat", "type": "CONCAT", "inputs": ["fillA", "trimB"], "params": {"width": 640, "height": 360, "fps": 30}},
        ], [{"id": "final", "operation": "cat", "path": "out/final.mp4"}, {"id": "partA", "operation": "fillA", "path": "out/partA.mp4"}])
        plan = self.run_cli("plan", doc)
        self.assertTrue(plan["dry_run"])
        self.assertFalse(os.path.exists(self.out()))
        self.assertEqual(len(plan["plan"]["steps"]), 4)
        self.assertTrue(all(s["preview"]["ok"] for s in plan["plan"]["steps"]), plan["plan"]["steps"])
        self.assertEqual(plan["plan"]["steps"][3]["timeline"]["duration"]["rational"], "11/2")
        res = self.run_cli("run", doc)
        self.assertAlmostEqual(duration(self.out()), 5.5, delta=0.3)
        self.assertEqual(size(self.out()), (640, 360))
        self.assertAlmostEqual(duration(self.out("out/partA.mp4")), 2.5, delta=0.15)
        ids = [o["operation_id"] for o in res["execution"]["operations"]]
        self.assertEqual(ids, [plan_s["operation_id"] for plan_s in plan["plan"]["steps"]])
        # second run: identical request, new output name -> every operation reused, bytes identical
        doc["project"]["outputs"] = [{"id": "again", "operation": "cat", "path": "out/again.mp4"}]
        res2 = self.run_cli("run", doc)
        self.assertEqual(res2["status"], "reused")
        self.assertEqual(sha256_file(self.out("out/again.mp4")), sha256_file(self.out()))
        # a changed parameter invalidates only its own chain
        doc["project"]["operations"][2]["params"]["end"] = 3
        doc["project"]["outputs"] = [{"id": "third", "operation": "cat", "path": "out/third.mp4"}]
        res3 = self.run_cli("run", doc)
        self.assertEqual([o["status"] for o in res3["execution"]["operations"]], ["reused", "reused", "completed", "completed"])

    # ------------------------------------------------------------ failures
    def test_range_beyond_duration(self):
        doc = request(self.sources(), [{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 5, "end": 9}}],
                      [{"id": "o", "operation": "t", "path": "out/final.mp4"}])
        out = self.run_cli("run", doc, expect=8)
        self.assertEqual(out["error"]["code"], "INVALID_TIME_RANGE")
        self.assertFalse(os.path.exists(self.out()))

    def test_transition_too_long(self):
        doc = request(self.sources(), [{"id": "c", "type": "CONCAT", "inputs": ["A", "B"], "params": {"transition": {"type": "fade", "duration": 3}}}],
                      [{"id": "o", "operation": "c", "path": "out/final.mp4"}])
        out = self.run_cli("run", doc, expect=8)
        self.assertEqual(out["error"]["code"], "INVALID_TIME_RANGE")

    def test_corrupt_input_fails_cleanly(self):
        bad = os.path.join(self.ws, "in", "bad.mp4")
        os.makedirs(os.path.dirname(bad))
        with open(bad, "wb") as fh:
            fh.write(b"\0" * 4096)
        doc = request([{"id": "X", "path": "in/bad.mp4"}], [{"id": "t", "type": "TRIM", "input": "X", "params": {"start": 0, "end": 1}}],
                      [{"id": "o", "operation": "t", "path": "out/final.mp4"}])
        rc, out, err = cli(["run", "-", "--json", "--workspace", self.ws], stdin=json.dumps(doc).encode(), env=self.env)
        self.assertFalse(out["ok"])
        self.assertIn(out["error"]["code"], ("INVALID_INPUT", "TOOL_ERROR"))
        self.assertFalse(os.path.exists(self.out()))

    def test_still_image_as_video_source_is_refused(self):
        """PNG bytes under a video extension: refused at probe time (no duration), before any encode can start."""
        img = os.path.join(self.ws, "in", "still.mp4")
        os.makedirs(os.path.dirname(img))
        shutil.copyfile(self.media["logo"], img)  # PNG bytes under a video extension
        doc = request([{"id": "A", "path": self.media["a"]}, {"id": "S", "path": "in/still.mp4"}],
                      [{"id": "c", "type": "CONCAT", "inputs": ["A", "S"], "params": {}}], [{"id": "o", "operation": "c", "path": "out/final.mp4"}])
        rc, out, err = cli(["run", "-", "--json", "--workspace", self.ws, "--allowed-input", os.path.dirname(self.media["a"]), "--allowed-input", self.ws],
                           stdin=json.dumps(doc).encode(), env=self.env)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "INVALID_INPUT")
        self.assertEqual(out["error"]["details"].get("reason"), "no_duration")
        self.assertFalse(os.path.exists(self.out()))
        work = os.path.join(self.ws, ".video-editing", "work")
        partials = [f for _, _, fs in os.walk(work) for f in fs if ".partial" in f]
        self.assertEqual(partials, [])

    def test_timeout_is_cancelled(self):
        doc = request(self.sources(), [{"id": "c", "type": "CONCAT", "inputs": ["A", "B", "A", "B"], "params": {"transition": {"type": "dissolve", "duration": 1}, "width": 1280, "height": 720}}],
                      [{"id": "o", "operation": "c", "path": "out/final.mp4"}], options={"timeout_seconds": 1})
        t0 = time.time()
        out = self.run_cli("run", doc, expect=130)
        self.assertEqual(out["error"]["code"], "CANCELLED")
        self.assertEqual(out["error"]["details"].get("reason"), "timeout")
        self.assertLess(time.time() - t0, 30)
        self.assertFalse(os.path.exists(self.out()))

    def test_doctor(self):
        rc, out, _ = cli(["doctor", "--json", "--workspace", self.ws], env=self.env)
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertFalse(out["secrets_shown"])


if __name__ == "__main__":
    unittest.main()
