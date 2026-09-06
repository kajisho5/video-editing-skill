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


@unittest.skipUnless(READY, REASON)
class OperationE2ETests(unittest.TestCase):
    """Real ffmpeg-skill + real media, one operation at a time: every delivered file exists, is non-empty, hashes as
    reported, has the expected duration, streams and frame, and the timeline says where it came from. Every document
    passes the response self-check. Plus the media-compatibility refusals that protect the engine (an overlay on a
    video without audio would never terminate in ffmpeg-skill 0.9.x; a corrupt image would too)."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ves-e2e-")
        cls.media = build_all(os.path.join(cls.root, "media"))
        na = os.path.join(cls.root, "media", "noaudio.mp4")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", "4",
                        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", na], check=True)
        cls.media["na"] = na
        bad = os.path.join(cls.root, "media", "bad.png")
        with open(bad, "wb") as fh:
            fh.write(b"corrupt png bytes")
        cls.media["bad"] = bad
        cls.env = {"VIDEO_EDITING_FFMPEG_SKILL_DIR": FFMPEG_SKILL.root}

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)

    def sources(self, bad=False):
        m = self.media
        s = [{"id": "A", "path": m["a"]}, {"id": "B", "path": m["b"]}, {"id": "NA", "path": m["na"]}, {"id": "logo", "path": m["logo"], "kind": "image"}]
        return s + ([{"id": "bad", "path": m["bad"], "kind": "image"}] if bad else [])

    def run_cli(self, cmd, doc, expect=0):
        from video_editing_skill.response import check_response
        rc, out, err = cli([cmd, "-", "--json", "--workspace", self.ws, "--allowed-input", os.path.dirname(self.media["a"])],
                           stdin=json.dumps(doc).encode(), env=self.env)
        self.assertIsInstance(out, dict, err)
        self.assertEqual(check_response(out, cmd), [], out)
        self.assertEqual(rc, expect, json.dumps(out.get("error"), indent=1) + err)
        return out

    def one(self, op, expect=0, sources=None):
        doc = request(sources or self.sources(), [dict(op, id="x")], [{"id": "o", "operation": "x", "path": "out/o.mp4"}])
        return self.run_cli("run", doc, expect)

    def facts(self, out, expected_duration, tol=0.35):
        """The delivered-file facts every operation must satisfy; returns (path, ffprobe streams)."""
        o = out["execution"]["outputs"][0]
        path = o["path"]
        self.assertTrue(o["delivered"] and os.path.isfile(path))
        self.assertGreater(os.path.getsize(path), 0)
        self.assertEqual(o["size"], os.path.getsize(path))
        self.assertEqual(o["sha256"], sha256_file(path), "the reported hash is the file's hash")
        self.assertEqual(o["timeline"]["duration_known"], True)
        self.assertAlmostEqual(float(o["timeline"]["duration"]["seconds"]), expected_duration, delta=0.001)
        real = duration(path)
        self.assertAlmostEqual(real, expected_duration, delta=tol, msg=f"duration {real} vs timeline {expected_duration}")
        self.assertAlmostEqual(float(o["observation"]["data"]["duration"]), real, delta=0.05)
        self.assertEqual((o["observation"]["kind"], o["observation"]["provenance"]), ("media.probe", "OBSERVED"))
        self.assertTrue(o["observation"]["source"].startswith("ffmpeg-skill/probe@"))
        rec = next(r for r in out["execution"]["operations"] if r["operation"] == o["operation"])
        self.assertEqual(rec["status"], "completed")
        self.assertTrue(rec["commands"] and all("ffmpeg" in c for c in rec["commands"]), rec["commands"])
        self.assertEqual(rec["output"]["sha256"], sha256_file(rec["output"]["path"]))
        self.assertEqual(rec["tool_versions"]["ffmpeg-skill"], FFMPEG_SKILL.version)
        streams = probe(path)["streams"]
        video = next(s for s in streams if s["codec_type"] == "video")
        self.assertEqual(video["codec_name"], "h264")
        return path, streams

    def has_audio(self, streams):
        return any(s["codec_type"] == "audio" for s in streams)

    def test_cut(self):
        out = self.one({"type": "CUT", "input": "A", "params": {"keep": [{"start": 4, "end": 5}, {"start": 1, "end": 2}]}})
        path, streams = self.facts(out, 2.0)
        self.assertEqual(size(path), (640, 360))
        self.assertTrue(self.has_audio(streams))
        segs = out["execution"]["outputs"][0]["timeline"]["tracks"][0]["segments"]
        self.assertEqual([(s["source_range"]["start"]["rational"], s["timeline_range"]["start"]["rational"]) for s in segs], [("4/1", "0/1"), ("1/1", "1/1")])

    def test_concat(self):
        out = self.one({"type": "CONCAT", "inputs": ["A", "B"], "params": {"width": 640, "height": 360, "fps": 30, "transition": {"type": "fade", "duration": 1}}})
        path, streams = self.facts(out, 10.0, tol=0.5)
        self.assertEqual(size(path), (640, 360))
        self.assertTrue(self.has_audio(streams))
        segs = out["execution"]["outputs"][0]["timeline"]["tracks"][0]["segments"]
        self.assertEqual([s["source"] for s in segs], ["A", "B"])
        self.assertEqual(segs[1]["timeline_range"]["start"]["rational"], "5/1")   # 6 s clip minus the 1 s overlap

    def test_concat_mixed_audio_gets_audio(self):
        out = self.one({"type": "CONCAT", "inputs": ["NA", "A"], "params": {"transition": {"type": "none", "duration": 0.5}}}) if False else \
            self.one({"type": "CONCAT", "inputs": ["NA", "A"], "params": {}})
        path, streams = self.facts(out, 10.0, tol=0.6)
        self.assertTrue(self.has_audio(streams), "ffmpeg-skill join inserts silence for the input without audio")

    def test_speed(self):
        out = self.one({"type": "SPEED", "input": "A", "params": {"factor": 2}})
        path, streams = self.facts(out, 3.0)
        self.assertEqual(size(path), (640, 360))
        self.assertTrue(self.has_audio(streams))
        seg = out["execution"]["outputs"][0]["timeline"]["tracks"][0]["segments"][0]
        self.assertEqual((seg["speed"], seg["source_range"]["end"]["rational"], seg["timeline_range"]["end"]["rational"]), ("2/1", "6/1", "3/1"))

    def test_resize(self):
        out = self.one({"type": "RESIZE", "input": "A", "params": {"width": 320}})
        path, streams = self.facts(out, 6.0)
        self.assertEqual(size(path), (320, 180))
        self.assertTrue(self.has_audio(streams))

    def test_fit(self):
        out = self.one({"type": "FIT", "input": "A", "params": {"aspect": "1:1", "width": 360, "pad_color": "white"}})
        path, streams = self.facts(out, 6.0)
        self.assertEqual(size(path), (360, 360))
        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        out = self.one({"type": "FIT", "input": "B", "params": {"aspect": "9:16"}})
        path, streams = self.facts(out, 5.0)
        w, h = size(path)
        self.assertAlmostEqual(w / h, 9 / 16, delta=0.01, msg="without params.width the frame is ffmpeg-skill's choice; the aspect is what was promised")
        self.assertTrue(self.has_audio(streams))

    def test_fill(self):
        out = self.one({"type": "FILL", "input": "A", "params": {"aspect": "1:1"}})
        path, streams = self.facts(out, 6.0)
        w, h = size(path)
        self.assertEqual(w, h, "1:1 as promised; the frame size without params.width is ffmpeg-skill's choice (it keeps the source width)")
        self.assertTrue(self.has_audio(streams))
        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        out = self.one({"type": "FILL", "input": "A", "params": {"aspect": "1:1", "width": 360}})
        path, streams = self.facts(out, 6.0)
        self.assertEqual(size(path), (360, 360))

    def test_fill_anchor(self):
        # docs/decisions.md ADR-009: anchor -> ffmpeg-skill fit.py --crop-x/--crop-y (0.10.0); testsrc2 has
        # distinguishable content across the frame, so a real anchor change must change the delivered bytes
        left = self.one({"type": "FILL", "input": "A", "params": {"aspect": "1:1", "width": 200, "anchor": {"x": 0, "y": 0.5}}})
        lpath, _ = self.facts(left, 6.0)
        self.assertEqual(size(lpath), (200, 200))
        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        right = self.one({"type": "FILL", "input": "A", "params": {"aspect": "1:1", "width": 200, "anchor": {"x": 1, "y": 0.5}}})
        rpath, _ = self.facts(right, 6.0)
        self.assertEqual(size(rpath), (200, 200))
        self.assertNotEqual(sha256_file(lpath), sha256_file(rpath), "a different anchor must crop a different region")
        self.assertEqual(left["execution"]["operations"][0]["parameters"]["crop_x"], "0.000")
        self.assertEqual(right["execution"]["operations"][0]["parameters"]["crop_x"], "1.000")

    def test_overlay(self):
        out = self.one({"type": "OVERLAY", "input": "A", "params": {"image": "logo", "position": "bottom-left", "start": 1, "end": 3, "fade": 0.25, "opacity": 0.8}})
        path, streams = self.facts(out, 6.0)
        self.assertEqual(size(path), (640, 360))
        self.assertTrue(self.has_audio(streams))
        tracks = out["execution"]["outputs"][0]["timeline"]["tracks"]
        self.assertEqual((tracks[1]["kind"], tracks[1]["segments"][0]["source"], tracks[1]["segments"][0]["timeline_range"]["end"]["rational"]), ("overlay", "logo", "3/1"))
        srcs = {s["id"]: s for s in out["execution"]["sources"]}
        self.assertEqual(srcs["logo"]["observation"]["data"]["video"]["width"], 120)

    def test_overlay_without_audio_is_refused_before_the_engine(self):
        t0 = time.monotonic()
        out = self.one({"type": "OVERLAY", "input": "NA", "params": {"image": "logo"}}, expect=3)
        self.assertEqual((out["error"]["code"], out["error"]["details"]["reason"]), ("INVALID_INPUT", "audio_required"))
        self.assertLess(time.monotonic() - t0, 30, "refused up front, not after a hung ffmpeg")
        self.assertFalse(os.path.exists(os.path.join(self.ws, "out", "o.mp4")))
        work = os.path.join(self.ws, ".video-editing", "work")
        self.assertFalse(os.path.isdir(work) and any(os.scandir(work)), "no tool ran")

    def test_corrupt_image_is_refused_before_the_engine(self):
        out = self.one({"type": "OVERLAY", "input": "A", "params": {"image": "bad"}}, expect=3, sources=self.sources(bad=True))
        self.assertEqual(out["error"]["details"]["reason"], "image_undecodable")

    def test_graph_chain_and_multiple_outputs(self):
        doc = request(self.sources(), [{"id": "cut", "type": "CUT", "input": "A", "params": {"keep": [{"start": 0, "end": 2}]}},
                                       {"id": "fast", "type": "SPEED", "input": "cut", "params": {"factor": 2}},
                                       {"id": "small", "type": "RESIZE", "input": "fast", "params": {"width": 320}},
                                       {"id": "brand", "type": "OVERLAY", "input": "small", "params": {"image": "logo", "position": "top-right"}}],
                      [{"id": "final", "operation": "brand", "path": "out/final.mp4"}, {"id": "mid", "operation": "small", "path": "out/mid.mp4"}])
        plan = self.run_cli("plan", doc)
        self.assertEqual([s["operation"] for s in plan["plan"]["steps"]], ["cut", "fast", "small", "brand"])
        self.assertFalse(os.path.exists(os.path.join(self.ws, "out")))
        out = self.run_cli("run", doc)
        outs = {o["id"]: o for o in out["execution"]["outputs"]}
        for oid in ("final", "mid"):
            self.assertTrue(outs[oid]["delivered"])
            self.assertEqual(outs[oid]["sha256"], sha256_file(outs[oid]["path"]))
            self.assertAlmostEqual(duration(outs[oid]["path"]), 1.0, delta=0.35)
            self.assertEqual(size(outs[oid]["path"]), (320, 180))
        self.assertEqual(len(outs["final"]["timeline"]["tracks"]), 2)
        self.assertEqual(outs["mid"]["timeline"]["tracks"][0]["segments"][0]["speed"], "2/1")
        recs = {r["operation"]: r for r in out["execution"]["operations"]}
        self.assertEqual(recs["fast"]["inputs"][0], {"ref": "cut", "kind": "operation", "operation_id": recs["cut"]["operation_id"], "sha256": recs["cut"]["output"]["sha256"]})
        again = self.run_cli("run", dict(doc, options={"overwrite": True}))
        self.assertEqual((again["status"], again["execution"]["reused"]), ("reused", True))
        self.assertEqual({o["id"]: o["sha256"] for o in again["execution"]["outputs"]}, {k: v["sha256"] for k, v in outs.items()})

    def test_doctor_reports_every_operation_available(self):
        rc, rep, err = cli(["doctor", "--json", "--workspace", self.ws], env=self.env)
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(rep["supported_operations"]), ["CONCAT", "CUT", "FILL", "FIT", "OVERLAY", "RESIZE", "SPEED", "TRIM"])
        self.assertTrue(rep["engine"]["capabilities_reported"])
        self.assertEqual(rep["engine"]["version"], FFMPEG_SKILL.version)


@unittest.skipUnless(READY, REASON)
class MediaMatrixE2ETests(unittest.TestCase):
    """Real-media matrix (contract.media_policy, frame_semantics, encoding): A video-only, B video+audio, C mixed audio,
    D different resolutions, E different frame rates, F short clip, G longer clip, H unicode path, I space-containing
    path, J multi-operation graph, K reuse, L invalid media, M invalid graph, N path traversal, O broken input; plus
    exact frame normalization, rotation metadata, HDR sources and the encoding profile. Every document is self-checked;
    every delivered file is checked for existence, size, hash, duration, streams, frame and provenance."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ves-matrix-")
        media = os.path.join(cls.root, "media")
        cls.media = build_all(media)
        ff = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]

        def run(*args):
            subprocess.run([*ff, *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cls.media["na"] = os.path.join(media, "noaudio.mp4")
        run("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", "4", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", cls.media["na"])
        cls.media["long"] = os.path.join(media, "long.mp4")
        run("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000", "-t", "30",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", cls.media["long"])
        cls.media["audio_only"] = os.path.join(media, "audio_only.mp4")
        run("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "3", "-c:a", "aac", cls.media["audio_only"])
        cls.media["rot90"] = os.path.join(media, "rot90.mp4")
        run("-display_rotation", "90", "-i", cls.media["a"], "-c", "copy", cls.media["rot90"])   # a real display matrix (a `rotate` tag is ignored by ffmpeg >= 5)
        cls.media["hdr"] = os.path.join(media, "hdr.mp4")
        run("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "3",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
            "-c:a", "aac", cls.media["hdr"])
        cls.media["odd"] = os.path.join(media, "odd.mp4")   # 641x361: only mpeg4 stores an odd frame (libx264 yuv420p refuses it)
        run("-f", "lavfi", "-i", "testsrc2=size=642x362:rate=30", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2", "-vf", "scale=641:361",
            "-c:v", "mpeg4", "-q:v", "3", "-c:a", "aac", cls.media["odd"])
        cls.media["broken"] = os.path.join(media, "broken.mp4")
        with open(cls.media["a"], "rb") as src, open(cls.media["broken"], "wb") as dst:
            dst.write(src.read()[: os.path.getsize(cls.media["a"]) * 3 // 5])
        uni = os.path.join(media, "素材 テスト", "sub dir")
        os.makedirs(uni)
        cls.media["unicode"] = os.path.join(uni, "クリップ (1) [final].mp4")
        shutil.copyfile(cls.media["a"], cls.media["unicode"])
        cls.media["unicode_logo"] = os.path.join(uni, "ロゴ é.png")
        shutil.copyfile(cls.media["logo"], cls.media["unicode_logo"])
        cls.media["spaces"] = os.path.join(media, "in dir", "clip with spaces.mp4")
        os.makedirs(os.path.dirname(cls.media["spaces"]))
        shutil.copyfile(cls.media["b"], cls.media["spaces"])
        cls.env = {"VIDEO_EDITING_FFMPEG_SKILL_DIR": FFMPEG_SKILL.root}
        cls.before = {k: sha256_file(v) for k, v in cls.media.items()}

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.media.items():
            assert sha256_file(v) == cls.before[k], f"fixture {k} was modified"

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)

    def src(self, key, sid, kind="video"):
        return {"id": sid, "path": self.media[key], "kind": kind} if kind != "video" else {"id": sid, "path": self.media[key]}

    def run_cli(self, cmd, doc, expect=0, roots=None):
        from video_editing_skill.response import check_response
        roots = roots or [os.path.join(self.root, "media")]
        argv = [cmd, "-", "--json", "--workspace", self.ws]
        for r in roots:
            argv += ["--allowed-input", r]
        rc, out, err = cli(argv, stdin=json.dumps(doc).encode(), env=self.env)
        self.assertIsInstance(out, dict, err)
        self.assertEqual(check_response(out, cmd), [], out)
        self.assertEqual(rc, expect, json.dumps(out.get("error"), indent=1) + err)
        return out

    def facts(self, out, oid, expected_duration, frame=None, audio=None, tol=0.35):
        o = next(x for x in out["execution"]["outputs"] if x["id"] == oid)
        path = o["path"]
        self.assertTrue(o["delivered"] and os.path.isfile(path) and os.path.getsize(path) > 0)
        self.assertEqual((o["size"], o["sha256"]), (os.path.getsize(path), sha256_file(path)))
        self.assertAlmostEqual(float(o["timeline"]["duration"]["seconds"]), expected_duration, delta=0.001)
        self.assertAlmostEqual(duration(path), expected_duration, delta=tol)
        streams = probe(path)["streams"]
        if frame is not None:
            self.assertEqual(size(path), frame)
        if audio is not None:
            self.assertEqual(any(s["codec_type"] == "audio" for s in streams), audio)
        rec = next(r for r in out["execution"]["operations"] if r["operation"] == o["operation"])
        self.assertIn(rec["status"], ("completed", "reused"))
        self.assertEqual(rec["output"]["sha256"], sha256_file(rec["output"]["path"]))
        self.assertIsNotNone(rec["normalized"])
        if frame is not None:
            self.assertEqual(rec["normalized"]["target_frame"], list(frame))
        return path, streams, rec

    # A / B / C ---------------------------------------------------------------
    def test_A_video_only_through_every_single_input_operation(self):
        ops = [{"id": "c", "type": "CUT", "input": "NA", "params": {"keep": [{"start": 0, "end": 1}, {"start": 3, "end": 4}]}},
               {"id": "s", "type": "SPEED", "input": "c", "params": {"factor": 2}},
               {"id": "f", "type": "FILL", "input": "s", "params": {"aspect": "1:1"}},
               {"id": "r", "type": "RESIZE", "input": "f", "params": {"width": 320}}]
        doc = request([self.src("na", "NA")], ops, [{"id": "o", "operation": "r", "path": "out/o.mp4"}, {"id": "mid", "operation": "c", "path": "out/c.mp4"}])
        out = self.run_cli("run", doc)
        self.facts(out, "mid", 2.0, (640, 360), audio=False)
        self.facts(out, "o", 1.0, (320, 320), audio=False)
        self.assertTrue(all(r["normalized"]["audio"] is False for r in out["execution"]["operations"]))

    def test_B_video_and_audio_keep_the_audio(self):
        doc = request([self.src("a", "A")], [{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 1, "end": 3}}], [{"id": "o", "operation": "t", "path": "out/o.mp4"}])
        out = self.run_cli("run", doc)
        path, streams, rec = self.facts(out, "o", 2.0, (640, 360), audio=True)
        self.assertEqual(rec["normalized"]["audio"], True)
        self.assertEqual(rec["normalized"]["video_codec"], "h264")

    def test_C_mixed_audio_presence_in_concat_both_orders(self):
        for order, frame in ((["A", "NA"], (640, 360)), (["NA", "B"], (640, 360))):
            self.setUp()
            doc = request([self.src("a", "A"), self.src("na", "NA"), self.src("b", "B")], [{"id": "c", "type": "CONCAT", "inputs": order, "params": {}}],
                          [{"id": "o", "operation": "c", "path": "out/o.mp4"}])
            out = self.run_cli("run", doc)
            expected = {"A": 6.0, "NA": 4.0, "B": 5.0}
            self.facts(out, "o", sum(expected[x] for x in order), frame, audio=True, tol=0.6)

    # D / E ---------------------------------------------------------------------
    def test_D_different_resolutions_conform_to_the_first_input(self):
        for order, frame in ((["A", "B"], (640, 360)), (["B", "A"], (1280, 720))):
            self.setUp()
            doc = request([self.src("a", "A"), self.src("b", "B")], [{"id": "c", "type": "CONCAT", "inputs": order, "params": {"transition": {"type": "fade", "duration": 0.5}}}],
                          [{"id": "o", "operation": "c", "path": "out/o.mp4"}])
            out = self.run_cli("run", doc)
            self.facts(out, "o", 10.5, frame, audio=True, tol=0.6)

    def test_E_different_frame_rates(self):
        doc = request([self.src("a", "A"), self.src("b", "B")], [{"id": "c", "type": "CONCAT", "inputs": ["A", "B"], "params": {}}], [{"id": "o", "operation": "c", "path": "out/o.mp4"}])
        out = self.run_cli("run", doc)
        path, streams, rec = self.facts(out, "o", 11.0, (640, 360), audio=True, tol=0.6)
        video = next(s for s in streams if s["codec_type"] == "video")
        self.assertEqual(video["r_frame_rate"], "30/1", "the first input's rate")
        self.setUp()
        doc["project"]["operations"][0]["params"] = {"fps": 25}
        out = self.run_cli("run", doc)
        path, streams, rec = self.facts(out, "o", 11.0, (640, 360), audio=True, tol=0.6)
        self.assertEqual(next(s for s in streams if s["codec_type"] == "video")["r_frame_rate"], "25/1")
        self.assertEqual(rec["normalized"]["target_fps"], 25.0)

    # F / G ---------------------------------------------------------------------
    def test_F_short_clip_then_overlay(self):
        doc = request([self.src("a", "A"), self.src("logo", "logo", "image")],
                      [{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 1, "end": 1.5}}, {"id": "o", "type": "OVERLAY", "input": "t", "params": {"image": "logo"}}],
                      [{"id": "x", "operation": "o", "path": "out/x.mp4"}])
        out = self.run_cli("run", doc)
        self.facts(out, "x", 0.5, (640, 360), audio=True, tol=0.2)

    def test_G_longer_clip_with_encoding_profile(self):
        doc = request([self.src("long", "L")], [{"id": "c", "type": "CUT", "input": "L", "params": {"keep": [{"start": 2, "end": 28}]}}],
                      [{"id": "o", "operation": "c", "path": "out/o.mp4", "encoding": {"crf": 24, "preset": "veryfast"}}])
        out = self.run_cli("run", doc)
        path, streams, rec = self.facts(out, "o", 26.0, (640, 360), audio=True)
        self.assertTrue(any("-crf 24" in c and "-preset veryfast" in c for c in rec["commands"]), rec["commands"])
        self.assertEqual((rec["encoding"], rec["normalized"]["encoding"]), ({"crf": 24, "preset": "veryfast"}, {"crf": 24, "preset": "veryfast"}))
        self.assertEqual(next(s for s in streams if s["codec_type"] == "video")["codec_name"], "h264")
        again = self.run_cli("run", dict(doc, options={"overwrite": True}))
        self.assertEqual(again["status"], "reused")
        other = dict(doc, options={"overwrite": True})
        other["project"]["outputs"][0]["encoding"] = {"crf": 28, "preset": "veryfast"}
        third = self.run_cli("run", other)
        self.assertEqual(third["status"], "completed")
        self.assertNotEqual(third["execution"]["operations"][0]["operation_id"], rec["operation_id"])
        self.assertTrue(any("-crf 28" in c for c in third["execution"]["operations"][0]["commands"]))
        self.assertLess(third["execution"]["outputs"][0]["size"], out["execution"]["outputs"][0]["size"], "crf 28 is smaller than crf 24 at the same preset")

    # H / I ---------------------------------------------------------------------
    def test_H_unicode_paths(self):
        doc = request([self.src("unicode", "A"), self.src("unicode_logo", "logo", "image")],
                      [{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1}}, {"id": "o", "type": "OVERLAY", "input": "t", "params": {"image": "logo"}}],
                      [{"id": "x", "operation": "o", "path": "出力/最終 版 (v2).mp4"}])
        out = self.run_cli("run", doc)
        path, streams, rec = self.facts(out, "x", 1.0, (640, 360), audio=True)
        self.assertTrue(path.endswith(os.path.join("出力", "最終 版 (v2).mp4")))
        self.assertTrue(any("クリップ" in c for c in out["execution"]["operations"][0]["commands"]))

    def test_I_space_containing_paths(self):
        doc = request([self.src("spaces", "B")], [{"id": "r", "type": "RESIZE", "input": "B", "params": {"width": 300}}], [{"id": "x", "operation": "r", "path": "out dir/final version.mp4"}])
        out = self.run_cli("run", doc)
        self.facts(out, "x", 5.0, (300, 170), audio=True)

    # J / K ---------------------------------------------------------------------
    def test_J_multi_operation_graph_with_concat_of_two_chains(self):
        doc = request([self.src("a", "A"), self.src("b", "B"), self.src("logo", "logo", "image")],
                      [{"id": "ta", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 2}},
                       {"id": "tb", "type": "TRIM", "input": "B", "params": {"start": 0, "end": 2}},
                       {"id": "fb", "type": "FIT", "input": "tb", "params": {"aspect": "16:9", "width": 640}},
                       {"id": "c", "type": "CONCAT", "inputs": ["ta", "fb"], "params": {"transition": {"type": "fade", "duration": 0.5}}},
                       {"id": "s", "type": "SPEED", "input": "c", "params": {"factor": "1/2"}},
                       {"id": "o", "type": "OVERLAY", "input": "s", "params": {"image": "logo", "position": "bottom-right", "start": 0, "end": 2}}],
                      [{"id": "x", "operation": "o", "path": "out/x.mp4"}, {"id": "m", "operation": "c", "path": "out/c.mp4"}])
        plan = self.run_cli("plan", doc)
        self.assertEqual([s["operation"] for s in plan["plan"]["steps"]], ["ta", "tb", "fb", "c", "s", "o"])
        self.assertEqual(next(s for s in plan["plan"]["steps"] if s["operation"] == "fb")["normalized"]["target_frame"], [640, 360])
        out = self.run_cli("run", doc)
        self.facts(out, "m", 3.5, (640, 360), audio=True, tol=0.5)
        self.facts(out, "x", 7.0, (640, 360), audio=True, tol=0.6)
        recs = {r["operation"]: r for r in out["execution"]["operations"]}
        self.assertEqual(recs["c"]["depends_on"], ["ta", "fb"])
        self.assertEqual({i["ref"]: i["operation_id"] for i in recs["c"]["inputs"]}, {"ta": recs["ta"]["operation_id"], "fb": recs["fb"]["operation_id"]})

    def test_K_reuse_is_exact_and_invalidates_on_change(self):
        doc = request([self.src("a", "A")], [{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 2}}, {"id": "r", "type": "RESIZE", "input": "t", "params": {"width": 320}}],
                      [{"id": "x", "operation": "r", "path": "out/x.mp4"}], {"overwrite": True})
        first = self.run_cli("run", doc)
        second = self.run_cli("run", doc)
        self.assertEqual((second["status"], second["execution"]["reused"]), ("reused", True))
        self.assertEqual([o["sha256"] for o in second["execution"]["outputs"]], [o["sha256"] for o in first["execution"]["outputs"]])
        self.assertEqual([r["operation_id"] for r in second["execution"]["operations"]], [r["operation_id"] for r in first["execution"]["operations"]])
        doc["project"]["operations"][0]["params"]["end"] = 3
        third = self.run_cli("run", doc)
        self.assertEqual([r["status"] for r in third["execution"]["operations"]], ["completed", "completed"], "a changed upstream parameter invalidates the chain")
        self.facts(third, "x", 3.0, (320, 180), audio=True)

    # L / M / N / O -------------------------------------------------------------
    def test_L_invalid_media(self):
        doc = request([self.src("audio_only", "A")], [{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1}}], [{"id": "x", "operation": "t", "path": "out/x.mp4"}])
        out = self.run_cli("run", doc, expect=3)
        self.assertEqual((out["error"]["code"], out["error"]["details"]["reason"]), ("INVALID_INPUT", "no_video_stream"))
        doc = request([self.src("logo", "L")], [{"id": "t", "type": "TRIM", "input": "L", "params": {"start": 0, "end": 1}}], [{"id": "x", "operation": "t", "path": "out/x.mp4"}])
        out = self.run_cli("run", doc, expect=6)
        self.assertEqual(out["error"]["code"], "UNSUPPORTED_FORMAT")

    def test_M_invalid_graph_runs_nothing(self):
        srcs = [self.src("a", "A")]
        cases = [
            ([{"id": "x", "type": "TRIM", "input": "y", "params": {"start": 0, "end": 1}}, {"id": "y", "type": "SPEED", "input": "x", "params": {"factor": 2}}], "cycle"),
            ([{"id": "x", "type": "TRIM", "input": "ghost", "params": {"start": 0, "end": 1}}], "unknown_reference"),
            ([{"id": "x", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1}}, {"id": "y", "type": "SPEED", "input": "A", "params": {"factor": 2}}], "unused_operation"),
        ]
        for ops, reason in cases:
            doc = request(srcs, ops, [{"id": "o", "operation": "x", "path": "out/o.mp4"}])
            out = self.run_cli("run", doc, expect=9)
            self.assertEqual((out["error"]["code"], out["error"]["details"]["reason"]), ("DEPENDENCY_ERROR", reason))
        self.assertFalse(os.path.exists(os.path.join(self.ws, ".video-editing")), "nothing ran, nothing was written")

    def test_N_path_traversal_and_escapes(self):
        secret = os.path.join(self.root, "secret.mp4")
        shutil.copyfile(self.media["a"], secret)
        base = [{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1}}]
        for src_path, reason in ((os.path.join(self.root, "media", "..", "secret.mp4"), "traversal"), (secret, "outside_allowed_roots")):
            out = self.run_cli("run", request([{"id": "A", "path": src_path}], base, [{"id": "o", "operation": "t", "path": "out/o.mp4"}]), expect=4)
            self.assertEqual((out["error"]["code"], out["error"]["details"]["reason"]), ("PATH_NOT_ALLOWED", reason))
        for out_path, reason in (("../escape.mp4", "traversal"), (os.path.join(self.root, "abs.mp4"), "absolute_output"), (self.media["a"], "absolute_output")):
            out = self.run_cli("run", request([self.src("a", "A")], base, [{"id": "o", "operation": "t", "path": out_path}]), expect=4)
            self.assertEqual((out["error"]["code"], out["error"]["details"]["reason"]), ("PATH_NOT_ALLOWED", reason))
        self.assertFalse(os.path.exists(os.path.join(self.root, "escape.mp4")))

    def test_O_broken_input_is_a_structured_failure(self):
        doc = request([self.src("broken", "X")], [{"id": "t", "type": "TRIM", "input": "X", "params": {"start": 0, "end": 5}}], [{"id": "o", "operation": "t", "path": "out/o.mp4"}])
        rc, out, err = cli(["run", "-", "--json", "--workspace", self.ws, "--allowed-input", os.path.join(self.root, "media")], stdin=json.dumps(doc).encode(), env=self.env)
        from video_editing_skill.response import check_response
        self.assertIsInstance(out, dict, err)
        self.assertEqual(check_response(out, "run"), [], out)
        self.assertFalse(out["ok"])
        self.assertIn(out["error"]["code"], ("INVALID_INPUT", "INVALID_TIME_RANGE", "TOOL_ERROR", "VALIDATION_ERROR"))
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.ws, "out", "o.mp4")))
        work = os.path.join(self.ws, ".video-editing", "work")
        self.assertEqual([f for _, _, fs in os.walk(work) for f in fs if ".partial" in f] if os.path.isdir(work) else [], [])

    # frame semantics / rotation / HDR --------------------------------------------
    def test_frame_targets_are_exact(self):
        cases = [({"type": "RESIZE", "input": "A", "params": {"width": 250}}, "a", (250, 142)),
                 ({"type": "RESIZE", "input": "B", "params": {"width": 300}}, "b", (300, 170)),
                 ({"type": "FIT", "input": "A", "params": {"aspect": "21:9"}}, "a", (840, 360)),
                 ({"type": "FILL", "input": "A", "params": {"aspect": "4:3", "width": 334}}, "a", (334, 250)),
                 ({"type": "FIT", "input": "B", "params": {"aspect": "9:16"}}, "b", (1280, 2276)),
                 ({"type": "FILL", "input": "A", "params": {"aspect": "1:1"}}, "a", (640, 640))]
        for op, key, frame in cases:
            self.setUp()
            doc = request([{"id": op["input"], "path": self.media[key]}], [dict(op, id="x")], [{"id": "o", "operation": "x", "path": "out/o.mp4"}])
            out = self.run_cli("run", doc)
            self.facts(out, "o", 6.0 if key == "a" else 5.0, frame, audio=True)

    def test_concat_single_dimension_and_odd_frames(self):
        for params, frame in (({"width": 300}, (300, 168)), ({"height": 250}, (444, 250))):
            self.setUp()
            doc = request([self.src("a", "A"), self.src("b", "B")], [{"id": "c", "type": "CONCAT", "inputs": ["A", "B"], "params": params}], [{"id": "o", "operation": "c", "path": "out/o.mp4"}])
            out = self.run_cli("run", doc)
            self.facts(out, "o", 11.0, frame, audio=True, tol=0.6)
        self.setUp()   # an odd 641x361 source: TRIM is refused before the engine, RESIZE / FIT / CONCAT normalize it to even
        doc = request([self.src("odd", "O")], [{"id": "t", "type": "TRIM", "input": "O", "params": {"start": 0, "end": 1}}], [{"id": "o", "operation": "t", "path": "out/o.mp4"}])
        out = self.run_cli("run", doc, expect=3)
        self.assertEqual((out["error"]["details"]["reason"], out["error"]["details"]["frame"]), ("odd_frame", [641, 361]))
        self.assertFalse(os.path.exists(os.path.join(self.ws, ".video-editing")), "nothing ran")
        for op, frame in (({"type": "RESIZE", "input": "O", "params": {"width": 202}}, (202, 114)), ({"type": "FIT", "input": "O", "params": {"aspect": "1:1"}}, (642, 642)),
                          ({"type": "CONCAT", "inputs": ["O", "A"], "params": {}}, (640, 360))):
            self.setUp()
            doc = request([self.src("odd", "O"), self.src("a", "A")], [dict(op, id="x")], [{"id": "o", "operation": "x", "path": "out/o.mp4"}])
            out = self.run_cli("run", doc)
            self.facts(out, "o", 2.0 if op["type"] != "CONCAT" else 8.0, frame, audio=True, tol=0.6)

    def test_rotation_metadata_is_measured_as_displayed(self):
        doc = request([self.src("rot90", "R")], [{"id": "r", "type": "RESIZE", "input": "R", "params": {"width": 180}}], [{"id": "o", "operation": "r", "path": "out/o.mp4"}])
        out = self.run_cli("run", doc)
        path, streams, rec = self.facts(out, "o", 6.0, (180, 320), audio=True)
        self.assertEqual(rec["normalized"]["source_frame"], [360, 640])

    def test_hdr_source_is_encoded_hevc_and_never_mixed(self):
        doc = request([self.src("hdr", "H")], [{"id": "t", "type": "TRIM", "input": "H", "params": {"start": 0, "end": 2}}], [{"id": "o", "operation": "t", "path": "out/o.mp4"}])
        out = self.run_cli("run", doc)
        path, streams, rec = self.facts(out, "o", 2.0, (640, 360), audio=True)
        self.assertEqual(next(s for s in streams if s["codec_type"] == "video")["codec_name"], "hevc")
        self.assertEqual((rec["normalized"]["hdr"], rec["normalized"]["video_codec"]), (True, "hevc"))
        self.assertTrue(any("HDR" in w for w in out["warnings"]))
        self.setUp()
        mixed = request([self.src("hdr", "H"), self.src("a", "A")], [{"id": "c", "type": "CONCAT", "inputs": ["H", "A"], "params": {}}], [{"id": "o", "operation": "c", "path": "out/o.mp4"}])
        out = self.run_cli("run", mixed, expect=3)
        self.assertEqual(out["error"]["details"]["reason"], "hdr_mismatch")
