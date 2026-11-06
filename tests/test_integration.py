"""Real-media integration: needs ffmpeg/ffprobe on PATH and an ffmpeg-skill checkout
(VIDEO_EDITING_FFMPEG_SKILL_DIR or a default location). Skipped, never faked, when they are missing.
"""
import json
import os
import re
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
# Detected, not assumed from the version (ADR-013): ffmpeg-skill 0.10.0's fit.py has no --rotate / --flip (added in
# 0.11.0). Against such a checkout ROTATE must be refused up front and reported MISSING by doctor; that is what is tested.
HAS_ROTATE = READY and FFMPEG_SKILL.missing_flags("fit", ["--rotate", "--flip"]) == []
# Optional engine tools (ADR-014), detected the same way: ffmpeg-skill 0.11.0 (the pinned CI checkout) has reverse.py but no
# freeze.py (from 0.16.0). Against a checkout without one, that type must be refused up front and reported MISSING by
# doctor as a note, not a problem.
HAS_FREEZE = READY and "freeze" in FFMPEG_SKILL.tools
HAS_REVERSE = READY and "reverse" in FFMPEG_SKILL.tools
HAS_INSERT = READY and "insert" in FFMPEG_SKILL.tools   # IMAGE_INSERT (ADR-015): insert.py, in 0.11.0 and later
HAS_CROP = READY and "crop" in FFMPEG_SKILL.tools       # CROP (ADR-016): crop.py, in 0.11.0 and later
ALL_OPERATIONS = ["CONCAT", "CONDENSE", "CROP", "CUT", "FILL", "FIT", "FREEZE", "IMAGE_INSERT", "OVERLAY", "PUNCH_IN", "RESIZE", "REVERSE", "ROTATE", "SPEED", "TRIM"]
AVAILABLE_OPERATIONS = [t for t in ALL_OPERATIONS if not ((t == "ROTATE" and not HAS_ROTATE) or (t == "FREEZE" and not HAS_FREEZE)
                                                          or (t == "REVERSE" and not HAS_REVERSE) or (t == "IMAGE_INSERT" and not HAS_INSERT)
                                                          or (t in ("CROP", "PUNCH_IN") and not HAS_CROP))]
# stinger transition (ADR-023): overlay.py --video --place, and broll.py / audio.py --replace for its sound (2.x and later)
HAS_STINGER = READY and all(t in FFMPEG_SKILL.tools for t in ("broll", "audio")) and FFMPEG_SKILL.missing_flags("overlay", ["--video", "--place"]) == []
HAS_SHOTS = READY and "scenes" in FFMPEG_SKILL.tools and FFMPEG_SKILL.missing_flags("scenes", ["--shots"]) == []   # static spans (ADR-025)
HAS_JOIN_COPY = READY and FFMPEG_SKILL.missing_flags("join", ["--copy"]) == []   # join.py --copy, from 2.24.0 (segment cache, ADR-018)
# fit.py's aspect-only canvas (no width) changed in ffmpeg-skill 0.16.10 from "keep the source width / height" to "never
# exceed the source": the exact frame each rule must deliver, written out (old, bounded).
BOUNDED = READY and FFMPEG_SKILL.fit_bounds_by_source()


def fit_frame(old, bounded):
    return bounded if BOUNDED else old


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
        self.assertEqual(rc, 0 if HAS_ROTATE else 1)
        self.assertEqual(out["ok"], HAS_ROTATE)
        if not HAS_ROTATE:
            self.assertEqual(out["problems"], ["operation ROTATE unavailable: flag:ffmpeg-skill/fit --rotate, flag:ffmpeg-skill/fit --flip"])
        missing_optional = [t for t, has in (("CROP", HAS_CROP), ("FREEZE", HAS_FREEZE), ("IMAGE_INSERT", HAS_INSERT), ("REVERSE", HAS_REVERSE)) if not has]
        self.assertEqual(sorted(n.split()[1] for n in out["notes"]), missing_optional)
        # roadmap step 1: whether the engine ships a tool is read from the checkout, never declared; CROP is an operation (ADR-016)
        self.assertEqual({u["type"]: u["engine_tool_status"] for u in out["unsupported"]}, {"POSITION": None})
        self.assertFalse(out["secrets_shown"])
        self.assertIsNone(out["engine"]["compatibility"]["upper_bound"])
        self.assertEqual({t["flags_from"] for t in out["engine"]["compatibility"]["tools"].values()} - {"contract", "help"}, set())


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(READY, REASON)
class OperationE2ETests(unittest.TestCase):
    """Real ffmpeg-skill + real media, one operation at a time: every delivered file exists, is non-empty, hashes as
    reported, has the expected duration, streams and frame, and the timeline says where it came from. Every document
    passes the response self-check. Plus the media-compatibility refusals that protect the engine (a corrupt image
    would never terminate)."""

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

    def test_speed_smooth(self):
        # kajisho5/video-editing-skill#13, docs/decisions.md ADR-012: SPEED.smooth -> fit.py --smooth,
        # which only takes effect on the slow-down leg (factor < 1) -- testsrc2 has real motion, so a
        # real difference in the frame-blend/interpolation filter must change the delivered bytes.
        none_out = self.one({"type": "SPEED", "input": "A", "params": {"factor": 0.5}})
        none_path, none_streams = self.facts(none_out, 12.0)
        self.assertEqual(size(none_path), (640, 360))
        self.assertTrue(self.has_audio(none_streams))

        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        blend_out = self.one({"type": "SPEED", "input": "A", "params": {"factor": 0.5, "smooth": "blend"}})
        blend_path, blend_streams = self.facts(blend_out, 12.0)
        self.assertEqual(size(blend_path), (640, 360))
        self.assertTrue(self.has_audio(blend_streams))

        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        interp_out = self.one({"type": "SPEED", "input": "A", "params": {"factor": 0.5, "smooth": "interpolate"}})
        interp_path, interp_streams = self.facts(interp_out, 12.0)
        self.assertEqual(size(interp_path), (640, 360))
        self.assertTrue(self.has_audio(interp_streams))

        none_hash, blend_hash, interp_hash = sha256_file(none_path), sha256_file(blend_path), sha256_file(interp_path)
        self.assertNotEqual(none_hash, blend_hash, "blend must change the delivered bytes versus no smoothing")
        self.assertNotEqual(none_hash, interp_hash, "interpolate must change the delivered bytes versus no smoothing")
        self.assertNotEqual(blend_hash, interp_hash, "blend and interpolate are different filters, different bytes")

        # a speed-up (factor > 1) accepts smooth without complaint, exactly as fit.py itself does, but
        # fit.py's own factor < 1.0 guard makes it a no-op there -- verified against real ffmpeg-skill,
        # not assumed: this must not fail and must produce the same duration/frame as the plain speed-up.
        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        speedup_out = self.one({"type": "SPEED", "input": "A", "params": {"factor": 2, "smooth": "blend"}})
        speedup_path, _ = self.facts(speedup_out, 3.0)
        self.assertEqual(size(speedup_path), (640, 360))

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

    def test_rotate(self):
        # kajisho5/video-editing-skill#11 item 1: ROTATE -> fit.py --rotate / --flip
        if not HAS_ROTATE:   # an engine without the flags: refused before anything runs, nothing delivered
            out = self.one({"type": "ROTATE", "input": "A", "params": {"degrees": 90}}, expect=10)
            self.assertEqual(out["error"]["code"], "TOOL_ERROR")
            self.assertIn("flag:--rotate", out["error"]["details"]["missing"])
            self.assertFalse(os.path.exists(os.path.join(self.ws, "out", "o.mp4")))
            return
        out = self.one({"type": "ROTATE", "input": "A", "params": {"degrees": 90}})
        path90, streams = self.facts(out, 6.0)
        self.assertEqual(size(path90), (360, 640), "a 90 degree turn swaps width and height")
        self.assertTrue(self.has_audio(streams))
        self.assertEqual(out["execution"]["operations"][0]["parameters"], {"rotate": 90})
        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        out = self.one({"type": "ROTATE", "input": "A", "params": {"degrees": 180}})
        path180, streams = self.facts(out, 6.0)
        self.assertEqual(size(path180), (640, 360), "180 degrees keeps the frame")
        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        out = self.one({"type": "ROTATE", "input": "A", "params": {"flip": "h"}})
        pathflip, streams = self.facts(out, 6.0)
        self.assertEqual(size(pathflip), (640, 360), "a flip alone keeps the frame size")
        self.assertNotEqual(sha256_file(path180), sha256_file(pathflip), "a 180 degree turn and a horizontal flip are different transforms")
        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        out = self.one({"type": "ROTATE", "input": "A", "params": {"degrees": 270, "flip": "v"}})
        path270, streams = self.facts(out, 6.0)
        self.assertEqual(size(path270), (360, 640), "270 also swaps width and height")
        self.assertEqual(out["execution"]["operations"][0]["parameters"], {"rotate": 270, "flip": "v"})
        self.assertNotEqual(sha256_file(path90), sha256_file(path270), "different rotate/flip combinations are different transforms")

    @classmethod
    def ramp(cls):
        """A 6 s, 30 fps clip with audio whose picture brightness rises with time (luma = 16 + 35 * t): a frame's mean luma
        says which source instant it shows, robustly through a re-encode."""
        path = os.path.join(cls.root, "media", "ramp.mp4")
        if not os.path.exists(path):
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=30:d=6,format=gray,geq=lum='16+T*35'",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "6", "-c:v", "libx264", "-preset", "veryfast",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", path], check=True)
        return path

    def luma(self, path, n):
        """Mean luma (signalstats YAVG) of decoded frame number n."""
        out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", f"select=eq(n\\,{n}),signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
                              "-frames:v", "1", "-f", "null", "-"], stdout=subprocess.PIPE, text=True, check=True).stdout
        m = re.search(r"YAVG=([0-9.]+)", out)
        self.assertIsNotNone(m, out)
        return float(m.group(1))

    def test_freeze(self):
        # ECOSYSTEM_ROADMAP step 2, docs/decisions.md ADR-014: FREEZE -> ffmpeg-skill freeze.py
        if not HAS_FREEZE:   # ffmpeg-skill 0.11.0 has no freeze.py: refused before anything runs, nothing delivered
            out = self.one({"type": "FREEZE", "input": "A", "params": {"hold": 1}}, expect=10)
            self.assertEqual((out["error"]["code"], out["error"]["details"]["missing"]), ("TOOL_ERROR", ["tool:ffmpeg-skill/freeze"]))
            self.assertFalse(os.path.exists(os.path.join(self.ws, "out", "o.mp4")))
            return
        ramp = [{"id": "R", "path": self.ramp()}]
        # inside the clip: the frame at 2 s is held for 1 s, the rest plays on after it (6 s + 1 s)
        out = self.one({"type": "FREEZE", "input": "R", "params": {"at": 2, "hold": 1}}, sources=ramp)
        path, streams = self.facts(out, 7.0)
        self.assertEqual(size(path), (320, 240))
        self.assertTrue(self.has_audio(streams), "the audio stream is kept (silent under the hold)")
        self.assertEqual(out["execution"]["operations"][0]["parameters"], {"at": "2.000000", "hold": "1.000000", "mode": "insert"})
        segs = out["execution"]["outputs"][0]["timeline"]["tracks"][0]["segments"]
        self.assertEqual([(s["source_range"]["start"]["rational"], s["timeline_range"]["start"]["rational"], s["speed"]) for s in segs],
                         [("0/1", "0/1", "1/1"), ("2/1", "2/1", "0/1"), ("2/1", "3/1", "1/1")])
        src = self.ramp()
        held = [self.luma(path, n) for n in (62, 75, 88)]          # 2.07 s .. 2.93 s: inside the hold
        self.assertLess(max(held) - min(held), 1.5, f"the picture does not change during the hold: {held}")
        # ffmpeg-skill 3.1.6 (#411) holds the frame shown at 2 s (frame 60 at 30 fps), the source instant this skill's
        # timeline reports for the hold; through 3.1.5 it held the frame shown just before 2 s (frame 59)
        held_at = 60 if FFMPEG_SKILL.freeze_holds_frame_at_t() else 59
        self.assertAlmostEqual(held[0], self.luma(src, held_at), delta=1.5,
                               msg=f"the held frame is source frame {held_at} (ffmpeg-skill {FFMPEG_SKILL.version})")
        self.assertGreater(abs(self.luma(src, 60) - self.luma(src, 59)), 1.5, "the ramp tells frames 59 and 60 apart")
        self.assertAlmostEqual(self.luma(path, 120), self.luma(src, 90), delta=1.5, msg="after the hold the clip resumes where it stopped, 1 s later")
        self.assertGreater(self.luma(path, 40) - self.luma(path, 20), 15, "outside the hold the picture moves")
        # without `at`: the last frame is held past the end (6 s + 1.5 s)
        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        out = self.one({"type": "FREEZE", "input": "R", "params": {"hold": 1.5}}, sources=ramp)
        path, streams = self.facts(out, 7.5)
        self.assertTrue(self.has_audio(streams))
        self.assertEqual(out["execution"]["operations"][0]["parameters"], {"hold": "1.500000", "mode": "extend"})
        tail = [self.luma(path, n) for n in (179, 195, 215)]
        self.assertLess(max(tail) - min(tail), 1.5, f"the last frame is held: {tail}")
        self.assertAlmostEqual(tail[0], self.luma(src, 179), delta=1.5)
        # `at` at the end is refused up front (the last frame is held by omitting it)
        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        out = self.one({"type": "FREEZE", "input": "R", "params": {"at": 6, "hold": 1}}, expect=8, sources=ramp)
        self.assertEqual(out["error"]["code"], "INVALID_TIME_RANGE")

    def test_reverse(self):
        # ECOSYSTEM_ROADMAP step 3, docs/decisions.md ADR-014: REVERSE -> ffmpeg-skill reverse.py
        if not HAS_REVERSE:
            out = self.one({"type": "REVERSE", "input": "A"}, expect=10)
            self.assertEqual((out["error"]["code"], out["error"]["details"]["missing"]), ("TOOL_ERROR", ["tool:ffmpeg-skill/reverse"]))
            return
        src = self.ramp()
        out = self.one({"type": "REVERSE", "input": "R"}, sources=[{"id": "R", "path": src}])
        path, streams = self.facts(out, 6.0)
        self.assertEqual(size(path), (320, 240))
        self.assertTrue(self.has_audio(streams), "the audio is reversed with the picture, not dropped")
        self.assertEqual(out["execution"]["operations"][0]["parameters"], {})
        seg = out["execution"]["outputs"][0]["timeline"]["tracks"][0]["segments"][0]
        self.assertEqual((seg["source_range"]["start"]["rational"], seg["source_range"]["end"]["rational"], seg["speed"], seg["reversed"]), ("0/1", "6/1", "1/1", True))
        # frame n of the output is frame 179 - n of the source: the picture plays backwards
        for n in (0, 30, 90, 150, 179):
            self.assertAlmostEqual(self.luma(path, n), self.luma(src, 179 - n), delta=1.5, msg=f"output frame {n} vs source frame {179 - n}")
        self.assertGreater(self.luma(path, 0) - self.luma(path, 179), 150, "brightness falls: the ramp runs backwards")

    def test_crop(self):
        # ECOSYSTEM_ROADMAP step 5, docs/decisions.md ADR-016: CROP -> ffmpeg-skill crop.py, exactly the given rectangle
        if not HAS_CROP:
            out = self.one({"type": "CROP", "input": "A", "params": {"x": 0, "y": 0, "width": 320, "height": 180}}, expect=10)
            self.assertEqual((out["error"]["code"], out["error"]["details"]["missing"]), ("TOOL_ERROR", ["tool:ffmpeg-skill/crop"]))
            self.assertFalse(os.path.exists(os.path.join(self.ws, "out", "o.mp4")))
            return
        # a 320x240 clip with audio: the left half white, the right half black
        halves = os.path.join(self.root, "media", "halves.mp4")
        if not os.path.exists(halves):
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=white:s=160x240:r=30:d=3",
                            "-f", "lavfi", "-i", "color=c=black:s=160x240:r=30:d=3", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                            "-filter_complex", "[0:v][1:v]hstack=inputs=2[v]", "-map", "[v]", "-map", "2:a", "-t", "3",
                            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", halves], check=True)
        src = [{"id": "H", "path": halves}]
        out = self.one({"type": "CROP", "input": "H", "params": {"x": 176, "y": 40, "width": 120, "height": 160}}, sources=src)
        path, streams = self.facts(out, 3.0)
        self.assertEqual(size(path), (120, 160), "exactly the rectangle's size")
        self.assertTrue(self.has_audio(streams), "the audio is kept")
        self.assertEqual(out["execution"]["operations"][0]["parameters"], {"x": 176, "y": 40, "width": 120, "height": 160})
        self.assertEqual(out["execution"]["operations"][0]["normalized"]["target_frame"], [120, 160])
        self.assertLess(self.luma(path, 30), 30, "a rectangle inside the black half is black")
        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        out = self.one({"type": "CROP", "input": "H", "params": {"x": 0, "y": 0, "width": 160, "height": 240}}, sources=src)
        path, _ = self.facts(out, 3.0)
        self.assertEqual(size(path), (160, 240))
        self.assertGreater(self.luma(path, 30), 220, "the left half is white")
        # a rectangle outside the frame is refused before anything runs
        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        out = self.one({"type": "CROP", "input": "H", "params": {"x": 200, "y": 0, "width": 160, "height": 240}}, expect=2, sources=src)
        self.assertEqual((out["error"]["code"], out["error"]["details"]["reason"]), ("INVALID_REQUEST", "crop_outside_frame"))
        self.assertFalse(os.path.exists(os.path.join(self.ws, "out", "o.mp4")))

    def test_image_insert(self):
        # ECOSYSTEM_ROADMAP step 4, docs/decisions.md ADR-015: IMAGE_INSERT -> ffmpeg-skill insert.py; a still joins CONCAT
        if not HAS_INSERT:
            out = self.one({"type": "IMAGE_INSERT", "input": "logo", "params": {"duration": 1}}, expect=10)
            self.assertEqual((out["error"]["code"], out["error"]["details"]["missing"]), ("TOOL_ERROR", ["tool:ffmpeg-skill/insert"]))
            self.assertFalse(os.path.exists(os.path.join(self.ws, "out", "o.mp4")))
            return
        # alone: a silent clip of exactly the asked length; height only -> width from the 120x40 image's aspect (3:1)
        out = self.one({"type": "IMAGE_INSERT", "input": "logo", "params": {"duration": 2, "height": 120}})
        path, streams = self.facts(out, 2.0)
        self.assertEqual(size(path), (360, 120))
        self.assertFalse(self.has_audio(streams), "insert.py writes a silent clip")
        v = next(x for x in streams if x["codec_type"] == "video")
        self.assertEqual(v["codec_name"], "h264")
        self.assertEqual(v["r_frame_rate"], "30/1", "fps is always passed (default 30)")
        rec = out["execution"]["operations"][0]
        self.assertEqual((rec["tool"], rec["parameters"]), ("ffmpeg-skill/insert", {"duration": "2.000000", "height": 120, "fps": "30"}))
        self.assertEqual(rec["inputs"][0]["sha256"], sha256_file(self.media["logo"]))
        self.assertLess(abs(self.luma(path, 0) - self.luma(path, 59)), 1.0, "a still: the picture does not change")
        # the point of the type: a title card in front of footage with audio, through CONCAT (silence is inserted under the still)
        self.ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        doc = request(self.sources(), [{"id": "card", "type": "IMAGE_INSERT", "input": "logo", "params": {"duration": 1.5, "width": 640, "height": 360}},
                                       {"id": "x", "type": "CONCAT", "inputs": ["card", "A"], "params": {"fps": 30}}],
                      [{"id": "o", "operation": "x", "path": "out/o.mp4"}])
        out = self.run_cli("run", doc)
        path, streams = self.facts(out, 7.5)
        self.assertEqual(size(path), (640, 360))
        self.assertTrue(self.has_audio(streams), "CONCAT keeps the footage's audio and puts silence under the still")
        segs = out["execution"]["outputs"][0]["timeline"]["tracks"][0]["segments"]
        self.assertEqual([(s["source"], s["speed"], s["timeline_range"]["start"]["rational"]) for s in segs], [("logo", "0/1", "0/1"), ("A", "1/1", "3/2")])
        recs = {r["operation"]: r for r in out["execution"]["operations"]}
        self.assertEqual(recs["x"]["inputs"][0], {"ref": "card", "kind": "operation", "operation_id": recs["card"]["operation_id"], "sha256": recs["card"]["output"]["sha256"]})
        self.assertGreater(abs(self.luma(path, 10) - self.luma(path, 100)), 5, "the card, then the footage")

    def test_overlay(self):
        out = self.one({"type": "OVERLAY", "input": "A", "params": {"image": "logo", "position": "bottom-left", "start": 1, "end": 3, "fade": 0.25, "opacity": 0.8}})
        path, streams = self.facts(out, 6.0)
        self.assertEqual(size(path), (640, 360))
        self.assertTrue(self.has_audio(streams))
        tracks = out["execution"]["outputs"][0]["timeline"]["tracks"]
        self.assertEqual((tracks[1]["kind"], tracks[1]["segments"][0]["source"], tracks[1]["segments"][0]["timeline_range"]["end"]["rational"]), ("overlay", "logo", "3/1"))
        srcs = {s["id"]: s for s in out["execution"]["sources"]}
        self.assertEqual(srcs["logo"]["observation"]["data"]["video"]["width"], 120)

    def test_overlay_on_audio_less_input_terminates_and_is_bounded(self):
        """ffmpeg-skill >=0.10.0's overlay.py bounds the looped-image composite with an explicit -t instead of
        -shortest, so this must complete promptly and land at the source's own duration (ADR-009)."""
        t0 = time.monotonic()
        out = self.one({"type": "OVERLAY", "input": "NA", "params": {"image": "logo", "position": "bottom-left", "start": 1, "end": 3, "fade": 0.25}})
        self.assertLess(time.monotonic() - t0, 30, "an explicit -t bounds it; this must not run anywhere near a hang-shaped timeout")
        path, streams = self.facts(out, 4.0)
        self.assertFalse(self.has_audio(streams), "NA has no audio stream; OVERLAY keeps the input's audio profile (none)")

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
        self.assertEqual(rc, 0 if HAS_ROTATE else 1, err)
        self.assertEqual(sorted(rep["supported_operations"]), AVAILABLE_OPERATIONS)
        self.assertTrue(rep["engine"]["capabilities_reported"])
        self.assertEqual(rep["engine"]["version"], FFMPEG_SKILL.version)


def framemd5(path):
    """Decoded-frame hashes of every stream (picture and sound), timestamps included."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path, "-map", "0", "-f", "framemd5", "-"],
                         stdout=subprocess.PIPE, check=True, text=True).stdout
    return [ln for ln in out.splitlines() if ln and not ln.startswith("#")]


@unittest.skipUnless(READY, REASON)
class SegmentCacheLongFormTests(unittest.TestCase):
    """ECOSYSTEM_ROADMAP step 8 (ADR-018): a 1-hour source, three cuts; after one cut changes, only that segment is encoded
    again, and the result equals a render of the changed request from an empty cache (framemd5)."""
    KEEP = [{"start": 600, "end": 603}, {"start": 1800, "end": 1802}, {"start": 3500, "end": 3504}]

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ves-long-")
        cls.env = {"VIDEO_EDITING_FFMPEG_SKILL_DIR": FFMPEG_SKILL.root}
        cls.media = os.path.join(cls.root, "media")
        os.makedirs(cls.media)
        cls.long = os.path.join(cls.media, "long.mp4")
        if HAS_JOIN_COPY:   # small picture, low rate: an hour of media in a few seconds of encoding and ~16 MB
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "3600", "-c:v", "libx264", "-preset", "veryfast",
                            "-crf", "35", "-g", "50", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "24k", "-ac", "1", cls.long], check=True)
        else:   # the refusal needs no media beyond a short clip
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                            "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", cls.long], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def run_cli(self, ws, keep, expect=0):
        doc = request([{"id": "L", "path": self.long}], [{"id": "c", "type": "CUT", "input": "L", "params": {"keep": keep}}],
                      [{"id": "o", "operation": "c", "path": "out/cut.mp4"}], options={"segment_cache": True, "overwrite": True})
        rc, out, err = cli(["run", "-", "--json", "--workspace", ws, "--allowed-input", self.media], stdin=json.dumps(doc).encode(), env=self.env)
        self.assertIsInstance(out, dict, err)
        self.assertEqual(rc, expect, json.dumps(out.get("error"), indent=1) + err)
        return out

    @unittest.skipIf(HAS_JOIN_COPY, "this ffmpeg-skill has join.py --copy")
    def test_refused_without_join_copy(self):
        from video_editing_skill.errors import EXIT_CODES
        out = self.run_cli(tempfile.mkdtemp(prefix="ws-", dir=self.root), [{"start": 0, "end": 1}, {"start": 2, "end": 3}], expect=EXIT_CODES["TOOL_ERROR"])
        self.assertEqual(out["error"]["code"], "TOOL_ERROR")
        self.assertIn("flag:ffmpeg-skill/join --copy", out["error"]["details"]["missing"])

    @unittest.skipUnless(HAS_JOIN_COPY, "needs ffmpeg-skill join.py --copy (2.24.0+); the refusal is tested instead")
    def test_one_changed_cut_re_encodes_only_that_segment(self):
        ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        first = self.run_cli(ws, self.KEEP)["execution"]["operations"][0]["segments"]
        self.assertEqual((first["encoded"], first["reused"], first["join"]), (3, 0, "stream_copy"))
        changed = [dict(r) for r in self.KEEP]
        changed[1] = {"start": 1800, "end": 1803}
        out = self.run_cli(ws, changed)
        seg = out["execution"]["operations"][0]["segments"]
        self.assertEqual([p["status"] for p in seg["parts"]], ["reused", "encoded", "reused"])
        self.assertEqual((seg["encoded"], seg["reused"], seg["join"]), (1, 2, "stream_copy"))
        partial = os.path.join(ws, "out", "cut.mp4")
        self.assertAlmostEqual(duration(partial), 10.0, delta=0.15)
        # the same changed request from an empty cache: every segment encoded, and the same frames
        fresh = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        full = self.run_cli(fresh, changed)["execution"]["operations"][0]["segments"]
        self.assertEqual(full["encoded"], 3)
        self.assertEqual(framemd5(partial), framemd5(os.path.join(fresh, "out", "cut.mp4")))
        # and the same request a third time writes nothing new
        again = self.run_cli(ws, changed)
        self.assertEqual(again["status"], "reused")


def packet_md5s(path):
    """md5 of every video packet as stored (stream copy, no decode): equal lists mean byte-for-byte the same packets."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path, "-map", "0:v", "-c", "copy", "-f", "framemd5", "-"],
                         stdout=subprocess.PIPE, check=True, text=True).stdout
    return [ln.rsplit(",", 1)[1].strip() for ln in out.splitlines() if ln and not ln.startswith("#")]


@unittest.skipUnless(READY, REASON)
class KeyframeAwareCutTests(unittest.TestCase):
    """ECOSYSTEM_ROADMAP step 9 (ADR-019): precision auto copies a cut whose in point is on a keyframe (the packets are the
    source's, byte for byte) and re-encodes one that is not, and the record says which per cut."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ves-kf-")
        cls.env = {"VIDEO_EDITING_FFMPEG_SKILL_DIR": FFMPEG_SKILL.root}
        cls.media = os.path.join(cls.root, "media")
        os.makedirs(cls.media)
        cls.src = os.path.join(cls.media, "gop30.mp4")   # a keyframe every second exactly (30 fps, closed GOP of 30)
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "12", "-c:v", "libx264", "-preset", "veryfast",
                        "-g", "30", "-keyint_min", "30", "-sc_threshold", "0", "-pix_fmt", "yuv420p", "-c:a", "aac", cls.src], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def trim(self, start, end):
        ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        doc = request([{"id": "S", "path": self.src}], [{"id": "t", "type": "TRIM", "input": "S", "params": {"start": start, "end": end, "precision": "auto"}}],
                      [{"id": "o", "operation": "t", "path": "out/t.mp4"}])
        rc, out, err = cli(["run", "-", "--json", "--workspace", ws, "--allowed-input", self.media], stdin=json.dumps(doc).encode(), env=self.env)
        self.assertEqual(rc, 0, json.dumps(out.get("error") if isinstance(out, dict) else out) + err)
        return os.path.join(ws, "out", "t.mp4"), out["execution"]["operations"][0]["cuts"]

    def test_cut_on_keyframes_is_a_stream_copy(self):
        path, cuts = self.trim(2, 5)
        self.assertEqual(cuts["requested_precision"], "auto")
        if cuts["mode"] is not None:   # reported from ffmpeg-skill 2.x; 0.11.0 does not name it
            self.assertEqual(cuts["mode"], "copy")
        got, src = packet_md5s(path), packet_md5s(self.src)
        self.assertGreaterEqual(len(got), 89)
        start = src.index(got[0])
        self.assertEqual(got, src[start:start + len(got)], "every video packet is the source's, byte for byte, in order")
        self.assertAlmostEqual(duration(path), 3.0, delta=0.15)

    def test_cut_between_keyframes_is_reported_as_what_it_is(self):
        """An in point half a GOP past a keyframe. The engine judges a copy by how far its length lands from the asked one, not
        by where the keyframes are (that measurement is Velisa's proposal 1 to ffmpeg-skill): 0.11.0 / 2.x snap the start to
        the keyframe 0.5 s early and re-encode; 3.x keeps the MP4 edit list (the picture starts at 2.5) and may copy. Either
        way the record must say what happened, and the packets must agree with it."""
        path, cuts = self.trim(2.5, 5)
        got, src = packet_md5s(path), packet_md5s(self.src)
        copied = got[0] in src and got == src[src.index(got[0]):src.index(got[0]) + len(got)]
        if cuts["mode"] is not None:
            self.assertEqual(cuts["mode"] == "copy", copied, cuts)
            self.assertEqual(bool(cuts["reencoded"]), not copied, cuts)
        if not copied:
            self.assertAlmostEqual(duration(path), 2.5, delta=0.1)


def rgb_frame(path, t, w, h):
    """One decoded frame at time t as rows of (r, g, b)."""
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE, check=True).stdout
    return [[tuple(raw[(y * w + x) * 3:(y * w + x) * 3 + 3]) for x in range(w)] for y in range(h)]


def red_box(frame):
    """Bounding box (x0, y0, x1, y1) of clearly red pixels, or None."""
    pts = [(x, y) for y, row in enumerate(frame) for x, (r, g, b) in enumerate(row) if r > 150 and g < 90 and b < 90]
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


@unittest.skipUnless(READY and HAS_CROP, REASON + " with crop.py")
class PunchInTests(unittest.TestCase):
    """ECOSYSTEM_ROADMAP step 10 (ADR-020): a 'face' (a red 40x40 box) left of centre; a 2x punch-in on it from 1 s to 3 s
    keeps the whole box inside the zoomed frame for the whole range, and the frame outside the range is untouched."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ves-pin-")
        cls.env = {"VIDEO_EDITING_FFMPEG_SKILL_DIR": FFMPEG_SKILL.root}
        cls.media = os.path.join(cls.root, "media")
        os.makedirs(cls.media)
        cls.src = os.path.join(cls.media, "face.mp4")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=320x180:r=30",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "4",
                        "-vf", "drawbox=x=40:y=60:w=40:h=40:color=red:t=fill", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", cls.src], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_face_left_of_centre_stays_in_the_zoomed_frame(self):
        ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        doc = request([{"id": "S", "path": self.src}],
                      [{"id": "p", "type": "PUNCH_IN", "input": "S", "params": {"start": 1, "end": 3, "scale": 2, "subject": {"x": 40, "y": 60, "width": 40, "height": 40}}}],
                      [{"id": "o", "operation": "p", "path": "out/p.mp4"}])
        rc, out, err = cli(["run", "-", "--json", "--workspace", ws, "--allowed-input", self.media], stdin=json.dumps(doc).encode(), env=self.env)
        self.assertEqual(rc, 0, json.dumps(out.get("error") if isinstance(out, dict) else out) + err)
        self.assertEqual(out["execution"]["operations"][0]["punch_in"]["window"], {"x": 0, "y": 34, "width": 160, "height": 90})
        path = os.path.join(ws, "out", "p.mp4")
        self.assertEqual(size(path), (320, 180))
        self.assertAlmostEqual(duration(path), 4.0, delta=0.1)
        for t in (1.05, 1.5, 2.0, 2.5, 2.95):   # the whole range: the box doubled to 80x80, fully inside, away from every edge
            box = red_box(rgb_frame(path, t, 320, 180))
            self.assertIsNotNone(box, t)
            x0, y0, x1, y1 = box
            self.assertTrue(0 < x0 and x1 < 319 and 0 < y0 and y1 < 179, (t, box))
            self.assertAlmostEqual(x1 - x0 + 1, 80, delta=4)
            self.assertAlmostEqual(y1 - y0 + 1, 80, delta=4)
            self.assertAlmostEqual(x0, 80, delta=4)   # (40 - 0) * 2
            self.assertAlmostEqual(y0, 52, delta=4)   # (60 - 34) * 2
        for t in (0.5, 3.5):   # outside the range: the source picture
            x0, y0, x1, y1 = red_box(rgb_frame(path, t, 320, 180))
            self.assertAlmostEqual(x0, 40, delta=2)
            self.assertAlmostEqual(x1 - x0 + 1, 40, delta=3)


def audio_md5s(path):
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path, "-map", "0:a", "-f", "framemd5", "-"],
                         stdout=subprocess.PIPE, check=True, text=True).stdout
    return [ln for ln in out.splitlines() if ln and not ln.startswith("#")]


@unittest.skipUnless(READY and HAS_CROP, REASON + " with crop.py")
class MultiAspectTests(unittest.TestCase):
    """ECOSYSTEM_ROADMAP step 11 (ADR-021): one cut list, three deliveries (16:9, 9:16, 1:1) in one request; each shot of the
    vertical and square versions cropped with its own window (CROP), the shots shared, the outputs of equal duration."""
    SHOTS = [(0, 2), (2.5, 4), (4.5, 6)]
    VERTICAL = [10, 115, 220]   # x of each shot's 90x160 window (from a subject path, in a real edit)
    SQUARE = [0, 70, 140]       # x of each shot's 180x180 window

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ves-aspect-")
        cls.env = {"VIDEO_EDITING_FFMPEG_SKILL_DIR": FFMPEG_SKILL.root}
        cls.media = os.path.join(cls.root, "media")
        os.makedirs(cls.media)
        cls.src = os.path.join(cls.media, "talk.mp4")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-ac", "2", cls.src], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def doc(self, vertical_x):
        ops = [{"id": f"s{i}", "type": "TRIM", "input": "S", "params": {"start": a, "end": b}} for i, (a, b) in enumerate(self.SHOTS)]
        ops.append({"id": "wide", "type": "CONCAT", "inputs": ["s0", "s1", "s2"], "params": {"width": 320, "height": 180}})
        for i, x in enumerate(vertical_x):
            ops.append({"id": f"v{i}", "type": "CROP", "input": f"s{i}", "params": {"x": x, "y": 10, "width": 90, "height": 160}})
        ops.append({"id": "vertical", "type": "CONCAT", "inputs": ["v0", "v1", "v2"], "params": {"width": 180, "height": 320}})
        for i, x in enumerate(self.SQUARE):
            ops.append({"id": f"q{i}", "type": "CROP", "input": f"s{i}", "params": {"x": x, "y": 0, "width": 180, "height": 180}})
        ops.append({"id": "square", "type": "CONCAT", "inputs": ["q0", "q1", "q2"], "params": {"width": 180, "height": 180}})
        return request([{"id": "S", "path": self.src}], ops,
                       [{"id": "w", "operation": "wide", "path": "out/p_16x9.mp4"}, {"id": "v", "operation": "vertical", "path": "out/p_9x16.mp4"},
                        {"id": "q", "operation": "square", "path": "out/p_1x1.mp4"}])

    def run_cli(self, doc, ws):
        rc, out, err = cli(["run", "-", "--json", "--workspace", ws, "--allowed-input", self.media], stdin=json.dumps(doc).encode(), env=self.env)
        self.assertIsInstance(out, dict, err)
        return rc, out

    def test_three_aspects_from_one_edit(self):
        ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        rc, out = self.run_cli(self.doc(self.VERTICAL), ws)
        self.assertEqual(rc, 0, json.dumps(out.get("error")))
        paths = {k: os.path.join(ws, "out", f"p_{k}.mp4") for k in ("16x9", "9x16", "1x1")}
        self.assertEqual((size(paths["16x9"]), size(paths["9x16"]), size(paths["1x1"])), ((320, 180), (180, 320), (180, 180)))
        durs = [duration(p) for p in paths.values()]
        self.assertAlmostEqual(durs[0], 5.0, delta=0.35)   # the contract's frame tolerance (0.11.0's join.py lands 5.106)
        self.assertLess(max(durs) - min(durs), 1 / 30 + 0.025, durs)   # equal to within a frame (container rounding)
        trims = [r for r in out["execution"]["operations"] if r["type"] == "TRIM"]
        self.assertEqual(len(trims), 3, "each shot is cut once and shared by the three versions")
        for r in out["execution"]["operations"]:
            if r["type"] == "CROP":   # every window inside its shot's frame, as measured
                fw, fh = r["normalized"]["source_frame"]
                p = r["params"]
                self.assertTrue(p["x"] + p["width"] <= fw and p["y"] + p["height"] <= fh, r["params"])
        # the sound comes from the same shots: every version has it, and none is longer or shorter than the others
        lens = [len(audio_md5s(p)) for p in paths.values()]
        self.assertLessEqual(max(lens) - min(lens), 1, lens)

    def test_a_window_outside_the_source_is_refused_before_anything_runs(self):
        ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        rc, out = self.run_cli(self.doc([10, 115, 240]), ws)   # 240 + 90 > 320
        self.assertEqual(out["error"]["code"], "INVALID_REQUEST")
        self.assertEqual(out["error"]["details"]["reason"], "crop_outside_frame")
        self.assertFalse(os.path.exists(os.path.join(ws, "out")))


@unittest.skipUnless(READY, REASON)
class GeneratedClipTests(unittest.TestCase):
    """ECOSYSTEM_ROADMAP step 12 (ADR-022): a generated clip (24 fps, no colour tags, no sound, non-square pixels) joins a
    29.97 fps edit with sound: conformed by CONCAT without a duration drift, and marked AI_GENERATED with the generator and
    licence the caller declared."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ves-gen-")
        cls.env = {"VIDEO_EDITING_FFMPEG_SKILL_DIR": FFMPEG_SKILL.root}
        cls.media = os.path.join(cls.root, "media")
        os.makedirs(cls.media)
        cls.edit = os.path.join(cls.media, "edit.mp4")
        cls.gen = os.path.join(cls.media, "generated.mp4")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30000/1001",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-c:a", "aac", cls.edit], check=True)
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "mandelbrot=size=480x360:rate=24",
                        "-t", "3", "-vf", "setsar=4/3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", cls.gen], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_generated_clip_joins_without_drift_and_carries_the_mark(self):
        ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        doc = request([{"id": "E", "path": self.edit},
                       {"id": "G", "path": self.gen, "ai_generated": {"generator": "example-video-model 2.1", "licence": "CC-BY-4.0"}}],
                      [{"id": "c", "type": "CONCAT", "inputs": ["E", "G", "E"], "params": {"width": 640, "height": 360, "fps": "30000/1001"}}],
                      [{"id": "o", "operation": "c", "path": "out/edit.mp4"}])
        rc, out, err = cli(["run", "-", "--json", "--workspace", ws, "--allowed-input", self.media], stdin=json.dumps(doc).encode(), env=self.env)
        self.assertEqual(rc, 0, json.dumps(out.get("error") if isinstance(out, dict) else out) + err)
        path = os.path.join(ws, "out", "edit.mp4")
        info = probe(path)
        v = next(st for st in info["streams"] if st["codec_type"] == "video")
        self.assertEqual((v["width"], v["height"]), (640, 360))
        # join.py writes its rate with fps={fps:g}, so 30000/1001 comes out as 2997/100 (an engine finding, ADR-022); both are 29.97
        self.assertIn(v["r_frame_rate"], ("30000/1001", "2997/100"))
        self.assertIn(v.get("sample_aspect_ratio", "1:1"), ("1:1", "0:1", None))   # the 4:3 pixels of the generated clip are conformed
        self.assertEqual((v.get("color_primaries"), v.get("color_transfer"), v.get("color_space")), ("bt709", "bt709", "bt709"))
        self.assertTrue(any(st["codec_type"] == "audio" for st in info["streams"]), "a silent track was added for the generated clip")
        # no duration drift: the output lasts the sum of the measured inputs, to within one frame of 29.97 fps, and so does the
        # timeline this Skill reports
        expected = 2 * duration(self.edit) + duration(self.gen)
        frame = 1001 / 30000
        self.assertAlmostEqual(float(info["format"]["duration"]), expected, delta=frame + 0.03)
        self.assertAlmostEqual(int(v["nb_frames"]) * frame, expected, delta=frame + 0.001)
        segs = out["execution"]["outputs"][0]["timeline"]["tracks"][0]["segments"]
        self.assertAlmostEqual(float(segs[2]["timeline_range"]["start"]["seconds"]), duration(self.edit) + duration(self.gen), delta=0.001)
        o = out["execution"]["outputs"][0]
        self.assertEqual(o["marks"], ["AI_GENERATED"])
        self.assertEqual(o["ai_generated"], [{"source": "G", "generator": "example-video-model 2.1", "licence": "CC-BY-4.0", "provenance": "DECLARED"}])
        self.assertEqual(out["execution"]["operations"][0]["marks"], ["AI_GENERATED"])
        src = {s["id"]: s for s in out["execution"]["sources"]}
        self.assertEqual(src["G"]["ai_generated"]["provenance"], "DECLARED")
        self.assertNotIn("ai_generated", src["E"])


def frame_px(path, n, points, w=320):
    """RGB of a few pixels of decoded frame n."""
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path, "-vf", f"select=eq(n\\,{n})", "-frames:v", "1", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE, check=True).stdout
    return [tuple(raw[(y * w + x) * 3:(y * w + x) * 3 + 3]) for x, y in points]


def colour(px):
    r, g, b = px
    return "red" if r > 150 and g < 90 and b < 90 else "blue" if b > 150 and r < 90 and g < 90 else "green" if g > 80 and r < 60 and b < 60 else "other"


@unittest.skipUnless(READY, REASON)
class StingerTests(unittest.TestCase):
    """ECOSYSTEM_ROADMAP step 13 (ADR-023): a red and a blue clip (2 s each, 30 fps) joined with a 1.2 s stinger cut at 0.5 s.
    The stinger covers the left half throughout and the whole frame only at its frame 15 (0.5 s): the frame before the cut
    shows red on the right, the cut frame is all stinger, the frame after shows blue. So the switch is at exactly frame 60."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ves-stinger-")
        cls.env = {"VIDEO_EDITING_FFMPEG_SKILL_DIR": FFMPEG_SKILL.root}
        cls.media = os.path.join(cls.root, "media")
        os.makedirs(cls.media)
        ff = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        cls.a, cls.b, cls.st = (os.path.join(cls.media, n) for n in ("a.mp4", "b.mp4", "stinger.mov"))
        for path, col, freq in ((cls.a, "red", 440), (cls.b, "blue", 660)):
            subprocess.run(ff + ["-f", "lavfi", "-i", f"color=c={col}:s=320x180:r=30", "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000",
                                 "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path], check=True)
        subprocess.run(ff + ["-f", "lavfi", "-i", "color=c=black@0.0:s=320x180:r=30:d=1.2,format=rgba,"
                             "drawbox=x=0:y=0:w=160:h=180:color=green@1:t=fill:replace=1,"
                             "drawbox=x=0:y=0:w=320:h=180:color=green@1:t=fill:replace=1:enable='eq(n,15)'",
                             "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=1.2",
                             "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", "-c:a", "pcm_s16le", "-shortest", cls.st], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def doc(self, cut_point=0.5, cover=None):
        tr = {"type": "stinger", "stinger": "S", "cut_point": cut_point, "cover": cover or {"start": 0.5, "end": {"frames": 16, "fps": 30}}}
        return request([{"id": "A", "path": self.a}, {"id": "B", "path": self.b}, {"id": "S", "path": self.st}],
                       [{"id": "c", "type": "CONCAT", "inputs": ["A", "B"], "params": {"transition": tr}}],
                       [{"id": "o", "operation": "c", "path": "out/stinger.mp4"}])

    def run_cli(self, doc):
        ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        rc, out, err = cli(["run", "-", "--json", "--workspace", ws, "--allowed-input", self.media], stdin=json.dumps(doc).encode(), env=self.env)
        self.assertIsInstance(out, dict, err)
        return rc, out, os.path.join(ws, "out", "stinger.mp4")

    @unittest.skipIf(HAS_STINGER, "this ffmpeg-skill has overlay --place, broll and audio --replace")
    def test_refused_without_the_engine_tools(self):
        rc, out, _ = self.run_cli(self.doc())
        self.assertEqual(out["error"]["code"], "TOOL_ERROR", out)

    @unittest.skipUnless(HAS_STINGER, "needs overlay.py --place, broll.py and audio.py --replace (ffmpeg-skill 2.x+)")
    def test_switch_at_the_cut_point_frame(self):
        rc, out, path = self.run_cli(self.doc())
        self.assertEqual(rc, 0, json.dumps(out.get("error")))
        self.assertAlmostEqual(duration(path), 4.0, delta=0.05)
        left, right = (40, 90), (280, 90)
        seen = {n: [colour(p) for p in frame_px(path, n, [left, right])] for n in (44, 45, 59, 60, 61, 80, 81, 100)}
        self.assertEqual(seen[44], ["red", "red"], "before the stinger: clip A untouched")
        self.assertEqual(seen[45], ["green", "red"], "stinger frame 0 at 1.5 s = 2.0 - 0.5")
        self.assertEqual(seen[59], ["green", "red"], "the last frame of A, under the stinger's bar")
        self.assertEqual(seen[60], ["green", "green"], "the cut frame: fully covered")
        self.assertEqual(seen[61], ["green", "blue"], "the first frame after the cut shows B")
        self.assertEqual(seen[80], ["green", "blue"], "stinger frame 35, its last")
        self.assertEqual(seen[81], ["blue", "blue"], "after the stinger: clip B untouched")
        rec = out["execution"]["operations"][0]
        self.assertEqual(rec["stinger"]["joins"][0]["cut_at"]["rational"], "2/1")
        self.assertEqual(rec["stinger"]["joins"][0]["stinger_start"]["rational"], "3/2")
        ov = out["execution"]["outputs"][0]["timeline"]["tracks"][1]["segments"][0]
        self.assertEqual((ov["kind"], ov["cut_at"]["rational"]), ("stinger", "2/1"))
        # the stinger's sound is mixed in under the join: louder there than in the clip alone
        def mean_db(ss):
            err = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(ss), "-t", "0.6", "-i", path, "-vn", "-af", "volumedetect", "-f", "null", "-"],
                                 capture_output=True, text=True).stderr
            return float(re.search(r"mean_volume: (-?[\d.]+) dB", err).group(1))
        self.assertGreater(mean_db(1.7), mean_db(0.3) + 1.0)

    def test_cut_point_outside_the_covered_window_is_refused(self):
        rc, out, path = self.run_cli(self.doc(cut_point=0.4))
        self.assertEqual((out["error"]["code"], out["error"]["details"].get("reason")), ("INVALID_REQUEST", "stinger_not_covering"))
        self.assertFalse(os.path.exists(path))


@unittest.skipUnless(READY, REASON)
class MotionSpanTests(unittest.TestCase):
    """ECOSYSTEM_ROADMAP step 15 (ADR-025): 2 s of motion, 3 s of a still picture, 2 s of motion; the sound is silent from
    2.5 s to 4.5 s. The still span is found to within one frame, and combined with the silence by and / or."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ves-motion-")
        cls.env = {"VIDEO_EDITING_FFMPEG_SKILL_DIR": FFMPEG_SKILL.root}
        cls.media = os.path.join(cls.root, "media")
        os.makedirs(cls.media)
        cls.src = os.path.join(cls.media, "fixed_camera.mp4")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2", "-f", "lavfi", "-i", "color=gray:s=320x180:r=30:d=3",
                        "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2",
                        "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000:d=2.5", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=2",
                        "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000:d=2.5",
                        "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0[v];[3][4][5]concat=n=3:v=0:a=1[a]", "-map", "[v]", "-map", "[a]",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", cls.src], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def propose(self, body):
        ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        doc = dict({"schema": "video-editing/propose@1", "source": self.src}, **body)
        rc, out, err = cli(["propose", "-", "--json", "--workspace", ws, "--allowed-input", self.media], stdin=json.dumps(doc).encode(), env=self.env)
        self.assertIsInstance(out, dict, err)
        return rc, out

    @unittest.skipIf(HAS_SHOTS, "this ffmpeg-skill has scenes --shots")
    def test_refused_without_scenes_shots(self):
        rc, out = self.propose({"static": {}})
        self.assertEqual(out["error"]["code"], "TOOL_ERROR", out)

    @unittest.skipUnless(HAS_SHOTS, "needs ffmpeg-skill scenes.py --shots (2.x+)")
    def test_still_span_within_one_frame_and_combined(self):
        frame = 1 / 30
        rc, out = self.propose({"static": {}})
        self.assertEqual(rc, 0, out)
        (p,) = out["proposals"]
        self.assertEqual(p["label"], "static")
        self.assertLessEqual(abs(float(p["start"]["seconds"]) - 2.0), frame + 1e-6, p)
        self.assertLessEqual(abs(float(p["end"]["seconds"]) - 5.0), frame + 1e-6, p)
        rc, out = self.propose({"static": {}, "silence": {"threshold_db": -50, "min_seconds": 0.5}, "combine": {"mode": "and"}})
        (p,) = out["proposals"]
        self.assertEqual(p["labels"], ["silence", "static"])
        self.assertLessEqual(abs(float(p["start"]["seconds"]) - 2.5), 0.05, p)
        self.assertLessEqual(abs(float(p["end"]["seconds"]) - 4.5), 0.05, p)
        rc, out = self.propose({"static": {}, "silence": {"threshold_db": -50, "min_seconds": 0.5}, "combine": {"mode": "or"}})
        (p,) = out["proposals"]
        self.assertEqual(p["label"], "silence+static")
        self.assertLessEqual(abs(float(p["start"]["seconds"]) - 2.0), frame + 1e-6, p)
        self.assertLessEqual(abs(float(p["end"]["seconds"]) - 5.0), frame + 1e-6, p)
        self.assertFalse(out["applied"])


def pcm(path, start, dur, rate=48000):
    """Mono signed 16-bit samples of a stretch of the sound."""
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(start), "-t", str(dur), "-i", path, "-vn", "-ac", "1", "-ar", str(rate),
                          "-f", "s16le", "-"], stdout=subprocess.PIPE, check=True).stdout
    import array
    a = array.array("h")
    a.frombytes(raw[: len(raw) // 2 * 2])
    return a


def tone_hz(samples, rate=48000):
    """Frequency of a pure tone by its zero crossings."""
    crossings = sum(1 for x, y in zip(samples, samples[1:]) if (x < 0) != (y < 0))
    return crossings / 2 / (len(samples) / rate)


@unittest.skipUnless(READY, REASON)
class CondenseTests(unittest.TestCase):
    """ECOSYSTEM_ROADMAP step 16 (ADR-026): a 6 s clip whose sound is a 440 Hz tone, then 2 s of near-silence (an 880 Hz tone
    at -50 dBFS, under the -35 dB silence threshold), then the tone again. The silence is proposed, approved, and kept at
    double speed: the result lasts 5 s and the quiet tone is still 880 Hz (atempo keeps the pitch)."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ves-condense-")
        cls.env = {"VIDEO_EDITING_FFMPEG_SKILL_DIR": FFMPEG_SKILL.root}
        cls.media = os.path.join(cls.root, "media")
        os.makedirs(cls.media)
        cls.src = os.path.join(cls.media, "talk.mp4")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=6",
                        "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000:d=2", "-f", "lavfi", "-i", "sine=f=880:sample_rate=48000:d=2",
                        "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000:d=2",
                        "-filter_complex", "[2]volume=-50dB[q];[1][q][3]concat=n=3:v=0:a=1[a]", "-map", "0:v", "-map", "[a]",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", cls.src], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def cli_doc(self, cmd, doc, ws):
        rc, out, err = cli([cmd, "-", "--json", "--workspace", ws, "--allowed-input", self.media], stdin=json.dumps(doc).encode(), env=self.env)
        self.assertIsInstance(out, dict, err)
        self.assertEqual(rc, 0, json.dumps(out.get("error")) + err)
        return out

    def test_silence_kept_at_double_speed(self):
        ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        props = self.cli_doc("propose", {"schema": "video-editing/propose@1", "source": self.src, "silence": {"threshold_db": -35, "min_seconds": 0.5}}, ws)
        (p,) = props["proposals"]
        self.assertAlmostEqual(float(p["start"]["seconds"]), 2.0, delta=0.05)
        self.assertAlmostEqual(float(p["end"]["seconds"]), 4.0, delta=0.05)
        seg = {"start": {"seconds": p["start"]["seconds"]}, "end": {"seconds": p["end"]["seconds"]}, "label": p["label"]}
        doc = request([{"id": "S", "path": self.src}],
                      [{"id": "c", "type": "CONDENSE", "input": "S", "params": {"segments": [seg], "rules": {"silence": {"speed": 2}}}}],
                      [{"id": "o", "operation": "c", "path": "out/tight.mp4"}])
        out = self.cli_doc("run", doc, ws)
        path = os.path.join(ws, "out", "tight.mp4")
        silent = float(p["end"]["seconds"]) - float(p["start"]["seconds"])
        self.assertAlmostEqual(duration(path), 6 - silent / 2, delta=0.1)
        rec = out["execution"]["operations"][0]
        self.assertEqual([pc["action"] for pc in rec["condense"]["pieces"]], ["keep", "speed", "keep"])
        # pitch: the loud tone untouched, the quiet tone still 880 Hz while it plays twice as fast (no chipmunk 1760 Hz)
        self.assertAlmostEqual(tone_hz(pcm(path, 0.5, 1.0)), 440, delta=15)
        q = pcm(path, 2.2, 0.6)
        self.assertAlmostEqual(tone_hz(q), 880, delta=40)
        self.assertAlmostEqual(tone_hz(pcm(path, 3.4, 1.0)), 440, delta=15)
        seg_tl = out["execution"]["outputs"][0]["timeline"]["tracks"][0]["segments"]
        self.assertEqual([s["speed"] for s in seg_tl], ["1/1", "2/1", "1/1"])

    def test_review_file_lasts_the_removed_spans(self):
        """ECOSYSTEM_ROADMAP step 17 (ADR-027): output removed joins only the spans the rules cut."""
        ws = tempfile.mkdtemp(prefix="ws-", dir=self.root)
        segs = [{"start": 1, "end": 1.5, "label": "retake"}, {"start": 3, "end": 4.2, "label": "retake"}, {"start": 5, "end": 5.5, "label": "silence"}]
        params = {"segments": segs, "rules": {"retake": "cut", "silence": {"speed": 2}}}
        doc = request([{"id": "S", "path": self.src}],
                      [{"id": "r", "type": "CONDENSE", "input": "S", "params": dict(params, output="removed")},
                       {"id": "e", "type": "CONDENSE", "input": "S", "params": params}],
                      [{"id": "review", "operation": "r", "path": "out/review.mp4"}, {"id": "edit", "operation": "e", "path": "out/edit.mp4"}])
        out = self.cli_doc("run", doc, ws)
        removed = 0.5 + 1.2
        self.assertAlmostEqual(duration(os.path.join(ws, "out", "review.mp4")), removed, delta=0.1)
        # the contract's frame tolerance: 0.11.0's join.py of four pieces lands 0.155 s long
        self.assertAlmostEqual(duration(os.path.join(ws, "out", "edit.mp4")), 6 - removed - 0.25, delta=0.35)
        rec = {r["operation"]: r for r in out["execution"]["operations"]}["r"]
        self.assertEqual([(x["source_range"]["start"]["rational"], x["review_range"]["start"]["rational"]) for x in rec["condense"]["review"]],
                         [("1/1", "0/1"), ("3/1", "1/2")])
        self.assertEqual(rec["condense"]["removed"]["rational"], "17/10")


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
            # setparams tags the frames themselves: ffmpeg >= 8 takes the encoder's colour properties from the frames and drops
            # -color_primaries / -color_trc output options, which left an untagged (SDR) "HDR" fixture behind
            "-vf", "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
            "-c:a", "aac", cls.media["hdr"])
        tags = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=color_primaries,color_transfer",
                                          "-of", "json", cls.media["hdr"]], check=True, capture_output=True, text=True).stdout)["streams"][0]
        assert (tags.get("color_primaries"), tags.get("color_transfer")) == ("bt2020", "smpte2084"), f"the HDR fixture is not tagged BT.2020 / PQ: {tags}"
        cls.media["sdr_hevc"] = os.path.join(media, "sdr_hevc.mp4")   # an SDR HEVC phone-style clip (ffmpeg-skill 3.0 cut.py keeps HEVC by default)
        run("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "4",
            "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error", "-pix_fmt", "yuv420p", "-tag:v", "hvc1", "-c:a", "aac", cls.media["sdr_hevc"])
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
                 ({"type": "FIT", "input": "A", "params": {"aspect": "21:9"}}, "a", fit_frame((840, 360), (640, 274))),
                 ({"type": "FILL", "input": "A", "params": {"aspect": "4:3", "width": 334}}, "a", (334, 250)),
                 ({"type": "FIT", "input": "B", "params": {"aspect": "9:16"}}, "b", fit_frame((1280, 2276), (406, 720))),
                 ({"type": "FILL", "input": "A", "params": {"aspect": "1:1"}}, "a", fit_frame((640, 640), (360, 360)))]
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
        for op, frame in (({"type": "RESIZE", "input": "O", "params": {"width": 202}}, (202, 114)), ({"type": "FIT", "input": "O", "params": {"aspect": "1:1"}}, fit_frame((642, 642), (362, 362))),
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

    def test_sdr_hevc_source_is_reencoded_h264_on_every_engine(self):
        """A frame-precision TRIM / CUT re-encodes an SDR source as H.264 like every other operation, also on ffmpeg-skill 3.x
        whose cut.py keeps HEVC by default (--no-keep-hevc is passed there, ADR-017); a keyframe TRIM is left to the engine."""
        for op in ({"type": "TRIM", "input": "S", "params": {"start": 1, "end": 3}},
                   {"type": "CUT", "input": "S", "params": {"keep": [{"start": 0, "end": 1}, {"start": 2, "end": 3}]}}):
            self.setUp()
            doc = request([self.src("sdr_hevc", "S")], [dict(op, id="t")], [{"id": "o", "operation": "t", "path": "out/o.mp4"}])
            out = self.run_cli("run", doc)
            path, streams, rec = self.facts(out, "o", 2.0, (640, 360), audio=True)
            self.assertEqual(next(s for s in streams if s["codec_type"] == "video")["codec_name"], "h264", op["type"])
        self.setUp()
        doc = request([self.src("sdr_hevc", "S")], [{"id": "t", "type": "TRIM", "input": "S", "params": {"start": 1, "end": 3, "precision": "keyframe"}}],
                      [{"id": "o", "operation": "t", "path": "out/o.mp4"}])
        out = self.run_cli("run", doc)
        self.facts(out, "o", 2.0, (640, 360), audio=True, tol=1.5)   # copy or not is the engine's call; the result is validated either way

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
