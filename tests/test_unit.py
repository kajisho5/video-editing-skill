"""Unit tests: schema validation, time arithmetic, mapping, operation ids, graph, serialisation, contract."""
import json
import os
import unittest
from fractions import Fraction

from helpers import make_workspace, request, write_fake_media

from video_editing_skill import contract, contract_check, operations
from video_editing_skill.canonical import canonical_json, stable_hash
from video_editing_skill.errors import ERROR_CODES, EXIT_CODES, EditError
from video_editing_skill.compiler import compile_project
from video_editing_skill.paths import PathPolicy
from video_editing_skill.project import parse_request
from video_editing_skill.timebase import Time, parse_fraction
from video_editing_skill.timeline import build_timelines


class TimeTests(unittest.TestCase):
    def test_forms_are_exact(self):
        self.assertEqual(Time.parse(0.1).value, Fraction(1, 10))
        self.assertEqual(Time.parse("1:30").value, Fraction(90))
        self.assertEqual(Time.parse("00:01:30.250").value, Fraction(3610, 40))
        self.assertEqual(Time.parse("00:01:30,250").value, Fraction(90) + Fraction(1, 4))
        self.assertEqual(Time.parse({"seconds": "2.5"}).value, Fraction(5, 2))
        self.assertEqual(Time.parse({"rational": "25/2"}).value, Fraction(25, 2))
        self.assertEqual(Time.parse({"frames": 300, "timebase": "1/30"}).value, Fraction(10))
        self.assertEqual(Time.parse({"frames": 30000, "fps": "30000/1001"}).value, Fraction(1001))

    def test_serialisation_stable(self):
        t = Time.parse({"frames": 1, "fps": "30000/1001"})
        self.assertEqual(t.to_dict(), {"seconds": "0.033367", "rational": "1001/30000"})
        self.assertEqual(Time.parse(12).tool_arg(), "12.000000")

    def test_rejects(self):
        for bad in (-1, "abc", "1:2:3:4", {"frames": -1, "fps": 30}, {"frames": 1}, True, None, [1], "1e3", float("nan"), {"rational": "1/0"}):
            with self.assertRaises(EditError, msg=repr(bad)):
                Time.parse(bad)
        with self.assertRaises(EditError):
            Time.parse(10 ** 9)

    def test_arithmetic(self):
        a, b = Time.parse("0:10"), Time.parse("0:20")
        self.assertEqual((b - a).value, Fraction(10))
        self.assertEqual(b.scale(Fraction(1, 2)).value, Fraction(10))
        self.assertTrue(a < b)
        self.assertEqual(parse_fraction("30000/1001", "x"), Fraction(30000, 1001))


class OperationParamTests(unittest.TestCase):
    def test_allowlist(self):
        with self.assertRaises(EditError) as cm:
            operations.validate_params("EXPLODE", {}, "op")
        self.assertEqual(cm.exception.code, "UNSUPPORTED_OPERATION")
        for t in ("CROP", "FREEZE", "REVERSE", "IMAGE_INSERT", "REORDER", "TRANSITION"):
            with self.assertRaises(EditError) as cm:
                operations.validate_params(t, {}, "op")
            self.assertEqual(cm.exception.code, "UNSUPPORTED_OPERATION")

    def test_trim_ranges(self):
        p = operations.validate_params("TRIM", {"start": 1, "end": 2}, "op")
        self.assertEqual(p["precision"], "frame")
        for bad in ({"start": 2, "end": 1}, {"start": 1, "end": 1}, {"start": -1, "end": 1}, {"start": 1}, {"start": 1, "end": 2, "x": 1}):
            with self.assertRaises(EditError):
                operations.validate_params("TRIM", bad, "op")

    def test_filter_reaching_values_are_closed(self):
        with self.assertRaises(EditError):
            operations.validate_params("FIT", {"aspect": "16:9", "pad_color": "black:eval=1"}, "op")
        with self.assertRaises(EditError):
            operations.validate_params("FIT", {"aspect": "16:9;drawtext=text=x"}, "op")
        with self.assertRaises(EditError):
            operations.validate_params("OVERLAY", {"image": "l", "position": "W-w-10,'x'"}, "op")
        with self.assertRaises(EditError):
            operations.validate_params("CONCAT", {"transition": {"type": "fade;rm", "duration": 1}}, "op")
        with self.assertRaises(EditError):
            operations.validate_params("FIT", {"aspect": "16:9", "width": 641}, "op")

    def test_speed_bounds(self):
        self.assertEqual(operations.validate_params("SPEED", {"factor": "3/2"}, "op")["factor"], Fraction(3, 2))
        for bad in (1, 0, 5, 0.1, "x"):
            with self.assertRaises(EditError):
                operations.validate_params("SPEED", {"factor": bad}, "op")

    def test_overlay(self):
        p = operations.validate_params("OVERLAY", {"image": "logo", "position": {"x": -10, "y": 5}, "opacity": 0.5, "start": 0, "end": 2}, "op")
        self.assertEqual(p["position"], {"x": -10, "y": 5})
        with self.assertRaises(EditError):
            operations.validate_params("OVERLAY", {"image": "logo", "start": 2, "end": 1}, "op")
        with self.assertRaises(EditError):
            operations.validate_params("OVERLAY", {"image": "logo", "opacity": 0}, "op")


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.ws = make_workspace()
        self.a = write_fake_media(os.path.join(self.ws, "in", "a.mp4"))
        self.b = write_fake_media(os.path.join(self.ws, "in", "b.mp4"), 2048)
        self.logo = write_fake_media(os.path.join(self.ws, "in", "logo.png"))
        self.policy = PathPolicy(self.ws)

    def base(self):
        return request([{"id": "A", "path": "in/a.mp4"}, {"id": "B", "path": "in/b.mp4"}, {"id": "logo", "path": "in/logo.png", "kind": "image"}],
                       [{"id": "t1", "type": "TRIM", "input": "A", "params": {"start": 1, "end": 3}},
                        {"id": "t2", "type": "TRIM", "input": "B", "params": {"start": 0, "end": 2}},
                        {"id": "c", "type": "CONCAT", "inputs": ["t1", "t2"], "params": {"transition": {"type": "fade", "duration": 0.5}}}],
                       [{"id": "final", "operation": "c", "path": "out/final.mp4"}])

    def test_parses_and_orders(self):
        p = parse_request(self.base(), self.policy)
        self.assertEqual(p.order, ["t1", "t2", "c"])
        self.assertEqual(p.operations["c"].depends_on, ["t1", "t2"])
        self.assertEqual(p.operations["c"].source_refs, ["A", "B"])
        self.assertTrue(p.operations["c"].operation_id.startswith("op_"))
        self.assertEqual(p.outputs["final"].path, os.path.join(os.path.realpath(self.ws), "out", "final.mp4"))

    def test_operation_ids_are_deterministic_and_label_free(self):
        p1 = parse_request(self.base(), self.policy)
        doc = self.base()
        # rename every label and reorder lists: identity must not move
        doc["project"]["sources"].reverse()
        doc["project"]["operations"].reverse()
        for o in doc["project"]["operations"]:
            o["id"] = "x_" + o["id"]
            if "input" in o:
                o["input"] = o["input"] if o["input"] in ("A", "B") else "x_" + o["input"]
            if "inputs" in o:
                o["inputs"] = ["x_" + i for i in o["inputs"]]
        doc["project"]["outputs"][0]["operation"] = "x_c"
        p2 = parse_request(doc, self.policy)
        self.assertEqual(p1.operations["c"].operation_id, p2.operations["x_c"].operation_id)
        self.assertEqual(p1.project_hash, p2.project_hash)
        # the same edit against different bytes is a different operation
        with open(self.a, "ab") as fh:
            fh.write(b"\1")
        p3 = parse_request(self.base(), self.policy)
        self.assertNotEqual(p1.operations["t1"].operation_id, p3.operations["t1"].operation_id)
        self.assertEqual(p1.operations["t2"].operation_id, p3.operations["t2"].operation_id)
        self.assertNotEqual(p1.operations["c"].operation_id, p3.operations["c"].operation_id)

    def test_params_change_id_time_forms_do_not(self):
        doc = self.base()
        doc["project"]["operations"][0]["params"] = {"start": "0:01", "end": {"frames": 90, "fps": 30}}
        p = parse_request(doc, self.policy)
        p0 = parse_request(self.base(), self.policy)
        self.assertEqual(p.operations["t1"].operation_id, p0.operations["t1"].operation_id)
        doc["project"]["operations"][0]["params"]["end"] = 3.001
        self.assertNotEqual(parse_request(doc, self.policy).operations["t1"].operation_id, p0.operations["t1"].operation_id)

    def test_dependency_errors(self):
        cases = [
            (lambda d: d["project"]["operations"][0].update(input="nope"), "DEPENDENCY_ERROR"),
            (lambda d: d["project"]["operations"][0].update(input="logo"), "DEPENDENCY_ERROR"),
            (lambda d: d["project"]["operations"][0].update(input="t1"), "DEPENDENCY_ERROR"),
            (lambda d: d["project"]["outputs"][0].update(operation="zzz"), "DEPENDENCY_ERROR"),
            (lambda d: d["project"]["operations"].append({"id": "orphan", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1}}), "DEPENDENCY_ERROR"),
            (lambda d: d["project"]["outputs"].append({"id": "dup", "operation": "c", "path": "out/final.mp4"}), "DEPENDENCY_ERROR"),
        ]
        for mutate, code in cases:
            d = self.base()
            mutate(d)
            with self.assertRaises(EditError) as cm:
                parse_request(d, self.policy)
            self.assertEqual(cm.exception.code, code, cm.exception.message)

    def test_cycle(self):
        d = self.base()
        d["project"]["operations"][0]["input"] = "c"
        d["project"]["operations"][0]["type"] = "TRIM"
        with self.assertRaises(EditError) as cm:
            parse_request(d, self.policy)
        self.assertEqual(cm.exception.details.get("reason"), "cycle")

    def test_schema_errors(self):
        cases = [
            (lambda d: d.update(schema="video-editing/request@9"), "INVALID_REQUEST"),
            (lambda d: d.update(command="rm -rf /"), "INVALID_REQUEST"),
            (lambda d: d["project"].update(argv=["ffmpeg"]), "INVALID_REQUEST"),
            (lambda d: d["project"]["operations"][0].update(filter="scale=1:1"), "INVALID_REQUEST"),
            (lambda d: d["project"]["operations"][0].update(inputs=["A"]), "INVALID_REQUEST"),
            (lambda d: d["project"]["operations"][2].update(input="t1"), "INVALID_REQUEST"),
            (lambda d: d["project"]["operations"][2].update(inputs=["t1"]), "INVALID_REQUEST"),
            (lambda d: d["project"]["sources"][0].update(id="bad id!"), "INVALID_REQUEST"),
            (lambda d: d["project"]["sources"].append({"id": "A", "path": "in/b.mp4"}), "INVALID_REQUEST"),
            (lambda d: d["project"]["sources"][0].update(kind="audio"), "INVALID_REQUEST"),
            (lambda d: d.update(options={"timeout_seconds": 0}), "INVALID_REQUEST"),
            (lambda d: d.update(options={"workspace": "/"}), "INVALID_REQUEST"),
            (lambda d: d["project"]["operations"][0].update(type="CROP", params={"x": 0, "y": 0, "width": 2, "height": 2}), "UNSUPPORTED_OPERATION"),
            (lambda d: d["project"]["outputs"][0].update(path="out/final.avi"), "UNSUPPORTED_FORMAT"),
            (lambda d: d["project"]["sources"][0].update(path="in/missing.mp4"), "MISSING_INPUT"),
            (lambda d: d["project"]["outputs"][0].update(path="in/a.mp4"), "PATH_NOT_ALLOWED"),
            (lambda d: d["project"]["outputs"][0].update(path="/etc/x.mp4"), "PATH_NOT_ALLOWED"),
            (lambda d: d["project"]["outputs"][0].update(path="../x.mp4"), "PATH_NOT_ALLOWED"),
            (lambda d: d["project"]["outputs"][0].update(path="out/CON.mp4"), "PATH_NOT_ALLOWED"),
            (lambda d: d["project"]["operations"][0].update(params={"start": 3, "end": 1}), "INVALID_TIME_RANGE"),
        ]
        for mutate, code in cases:
            d = self.base()
            mutate(d)
            with self.assertRaises(EditError) as cm:
                parse_request(d, self.policy)
            self.assertEqual(cm.exception.code, code, cm.exception.message)
        with self.assertRaises(EditError):
            parse_request([], self.policy)
        with self.assertRaises(EditError):
            parse_request({"schema": "video-editing/request@1", "project": "x"}, self.policy)

    def test_existing_output_needs_overwrite(self):
        write_fake_media(os.path.join(self.ws, "out", "final.mp4"))
        with self.assertRaises(EditError) as cm:
            parse_request(self.base(), self.policy)
        self.assertEqual(cm.exception.details.get("reason"), "exists")
        d = self.base()
        d["options"] = {"overwrite": True}
        parse_request(d, self.policy)

    def test_serialisation_is_stable_json(self):
        p = parse_request(self.base(), self.policy)
        d1 = canonical_json(p.to_dict())
        d2 = canonical_json(parse_request(self.base(), self.policy).to_dict())
        self.assertEqual(d1, d2)
        json.loads(d1)
        self.assertNotIn("Fraction", d1)

    def test_compiler_flags_are_typed(self):
        p = parse_request(self.base(), self.policy)
        steps = compile_project(p)
        argv = steps["c"].argv_for(["/x/1.mp4", "/x/2.mp4"], "/x/out.mp4")
        self.assertEqual(argv[:4], ["/x/1.mp4", "/x/2.mp4", "-o", "/x/out.mp4"])
        self.assertIn("--transition", argv)
        self.assertIn("fade", argv)
        self.assertNotIn("--json", argv)
        t = steps["t1"].argv_for(["/x/1.mp4"], "/x/o.mp4")
        self.assertEqual(t, ["/x/1.mp4", "-o", "/x/o.mp4", "--start", "1.000000", "--end", "3.000000", "--accurate"])


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.ws = make_workspace()
        write_fake_media(os.path.join(self.ws, "in", "a.mp4"))
        write_fake_media(os.path.join(self.ws, "in", "b.mp4"))
        self.policy = PathPolicy(self.ws)

    def project(self, ops, out="c"):
        return parse_request(request([{"id": "A", "path": "in/a.mp4"}, {"id": "B", "path": "in/b.mp4"}], ops,
                                     [{"id": "o", "operation": out, "path": "o.mp4"}]), self.policy)

    def test_source_timeline_mapping(self):
        p = self.project([{"id": "t1", "type": "TRIM", "input": "A", "params": {"start": 10, "end": 20}},
                          {"id": "s", "type": "SPEED", "input": "t1", "params": {"factor": 2}},
                          {"id": "t2", "type": "CUT", "input": "B", "params": {"keep": [{"start": 4, "end": 5}, {"start": 1, "end": 2}]}},
                          {"id": "c", "type": "CONCAT", "inputs": ["s", "t2"], "params": {"transition": {"type": "fade", "duration": 1}}}])
        clips = build_timelines(p, {"A": Time.parse(60), "B": Time.parse(30)})
        self.assertEqual(clips["t1"].duration.value, 10)
        self.assertEqual(clips["s"].duration.value, 5)
        self.assertEqual(clips["t2"].duration.value, 2)
        c = clips["c"]
        self.assertEqual(c.duration.value, 5 + 2 - 1)
        segs = [s.to_dict() for s in c.segments]
        self.assertEqual(segs[0]["source"], "A")
        self.assertEqual(segs[0]["source_range"]["start"]["rational"], "10/1")
        self.assertEqual(segs[0]["source_range"]["end"]["rational"], "20/1")
        self.assertEqual(segs[0]["timeline_range"], {"start": Time.parse(0).to_dict(), "end": Time.parse(5).to_dict()})
        self.assertEqual(segs[0]["speed"], "2/1")
        # second clip starts 1 s early because of the transition; its keep order is preserved (4-5 before 1-2)
        self.assertEqual(segs[1]["source_range"]["start"]["rational"], "4/1")
        self.assertEqual(segs[1]["timeline_range"]["start"]["rational"], "4/1")
        self.assertEqual(segs[2]["source_range"]["start"]["rational"], "1/1")
        self.assertEqual(segs[2]["timeline_range"]["start"]["rational"], "5/1")
        self.assertEqual(segs[2]["timeline_range"]["end"]["rational"], "6/1")

    def test_trim_of_sped_clip_maps_back_to_source(self):
        p = self.project([{"id": "s", "type": "SPEED", "input": "A", "params": {"factor": "1/2"}},
                          {"id": "t", "type": "TRIM", "input": "s", "params": {"start": 4, "end": 6}},
                          {"id": "c", "type": "CONCAT", "inputs": ["t", "B"], "params": {}}])
        clips = build_timelines(p, {"A": Time.parse(10), "B": Time.parse(3)})
        seg = clips["t"].segments[0].to_dict()
        # slowed 2x: timeline 4..6 of the slowed clip is source 2..3
        self.assertEqual(seg["source_range"]["start"]["rational"], "2/1")
        self.assertEqual(seg["source_range"]["end"]["rational"], "3/1")
        self.assertEqual(clips["c"].duration.value, 5)

    def test_unknown_durations_are_not_guessed(self):
        p = self.project([{"id": "c", "type": "CONCAT", "inputs": ["A", "B"], "params": {}}])
        clips = build_timelines(p, {"A": None, "B": Time.parse(3)})
        d = clips["c"].to_dict()
        self.assertFalse(d["duration_known"])
        self.assertIsNone(d["duration"])
        self.assertNotIn("timeline_range", d["tracks"][0]["segments"][0])


class ContractTests(unittest.TestCase):
    def test_contract_consistent_with_code(self):
        c = contract.skill_contract()
        self.assertEqual(sorted(c["operations"]), sorted(operations.OPERATIONS))
        declared = {x["capability"] for x in c["capabilities"]}
        for spec in operations.OPERATIONS.values():
            self.assertIn(spec["capability"], declared)
        for u in c["unsupported"]:
            self.assertNotIn(u["capability"], declared)
            self.assertEqual(u["status"], "NOT_IMPLEMENTED")
        self.assertEqual(set(c["errors"]["codes"]), set(ERROR_CODES))
        self.assertEqual(c["errors"]["exit_codes"], EXIT_CODES)
        self.assertFalse(c["execution"]["shell"])
        self.assertFalse(c["execution"]["raw_ffmpeg_arguments"])
        for t in c["tools"]:
            for k in ("tool_id", "skill_id", "version", "description", "required_capabilities", "inputs", "produces_output", "deterministic", "result_keys"):
                self.assertIn(k, t)
            self.assertEqual(t["tool_id"].split("/")[0], c["skill_id"])
        json.dumps(c)
        self.assertEqual(stable_hash(c), stable_hash(contract.skill_contract()))
        # two independent version axes (docs/decisions.md ADR-007): skill release version, and
        # contract_version, which changes only when a pinned block changes in a breaking way
        self.assertEqual(c["contract_version"], contract.CONTRACT_VERSION)
        self.assertIn("contract_version", contract.PINNED_BLOCKS)
        self.assertNotIn("version", contract.PINNED_BLOCKS)
        self.assertEqual(c["versioning"]["contract_version"], contract.CONTRACT_VERSION)
        # dependencies (docs/decisions.md ADR-008): the ffmpeg-skill range this Skill already enforces at runtime
        self.assertEqual(c["dependencies"], [{"skill_id": "ffmpeg-skill", "version_range": contract.ffmpeg_skill_version_range()}])
        self.assertEqual(c["dependencies"][0]["version_range"], c["engine"]["version_range"])
        self.assertNotIn("dependencies", contract.PINNED_BLOCKS)

    def test_implementation_matches_contract_and_golden(self):
        self.assertEqual(contract_check.verify_implementation(), [])
        golden = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "contract", "contract.json")
        with open(golden, encoding="utf-8") as fh:
            saved = json.load(fh)
        rep = contract_check.run_check(saved)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["drift"]["status"], "ok", rep["drift"])

    def test_drift_is_detected(self):
        live = contract.skill_contract()
        for mutate, expect in (
            (lambda d: d["tools"][0].update(required_capabilities=["ffmpeg"]), "required_capabilities changed"),
            (lambda d: d["tools"][0].update(result_keys=["x"]), "result_keys changed"),
            (lambda d: d["tools"][0].update(produces_output=False), "produces_output changed"),
            (lambda d: d["tools"].pop(), "tool added"),
            (lambda d: d["tools"].append({"tool_id": "video-editing/freeze"}), "tool removed"),
            (lambda d: d.update(contract_version="9.9"), "pinned block contract_version changed"),
            (lambda d: d.update(skill_id="other"), "skill_id changed"),
            (lambda d: d["errors"]["exit_codes"].update(TOOL_ERROR=99), "errors (codes / exit codes / retryable) changed"),
            (lambda d: d["unsupported"].pop(), "unsupported list changed"),
            (lambda d: d["operations"].pop("TRIM"), "operations (types / parameters) changed"),
        ):
            saved = json.loads(json.dumps(live))
            mutate(saved)
            rep = contract_check.check_saved(saved, live)
            self.assertEqual(rep["status"], "drift", expect)
            self.assertTrue(any(expect in p for p in rep["problems"]), (expect, rep["problems"]))
        # the skill's own release version is a separate axis (docs/decisions.md ADR-007) and may
        # move without being a breaking contract change: it is drift, but additive, not a problem
        saved = json.loads(json.dumps(live))
        saved["version"] = "9.9.9"
        rep = contract_check.check_saved(saved, live)
        self.assertEqual(rep["status"], "drift")
        self.assertEqual(rep["compatibility"], "additive")
        self.assertEqual(rep["problems"], [])
        self.assertTrue(any("version" in a for a in rep["additions"]), rep["additions"])
        self.assertEqual(contract_check.check_saved("junk", live)["status"], "drift")
        # a contract that lies about the implementation is caught too
        lying = json.loads(json.dumps(live))
        lying["capabilities"].append({"capability": "video.freeze", "operations": ["FREEZE"], "tool": "x"})
        self.assertTrue(any("capabilities differ" in p for p in contract_check.verify_implementation(lying)))
        lying = json.loads(json.dumps(live))
        lying["operations"]["FREEZE"] = {}
        self.assertTrue(any("allowlist" in p for p in contract_check.verify_implementation(lying)))
        lying = json.loads(json.dumps(live))
        lying["execution"]["shell"] = True
        self.assertTrue(any("execution.shell" in p for p in contract_check.verify_implementation(lying)))

    def test_error_exit_codes_unique(self):
        self.assertEqual(len(set(EXIT_CODES.values())), len(EXIT_CODES))
        self.assertEqual(EXIT_CODES["CANCELLED"], 130)
        e = EditError("TOOL_ERROR", "x")
        self.assertTrue(e.retryable)
        self.assertEqual(e.envelope()["ok"], False)
        with self.assertRaises(ValueError):
            EditError("NOPE", "x")


if __name__ == "__main__":
    unittest.main()


class ExecutorHarness(unittest.TestCase):
    """An Executor fed probe documents directly (no engine), for media / normalization / validation tests."""

    def setUp(self):
        self.ws = make_workspace()
        self.a = write_fake_media(os.path.join(self.ws, "in", "a.mp4"))
        self.b = write_fake_media(os.path.join(self.ws, "in", "b.mp4"), 2048)
        self.logo = write_fake_media(os.path.join(self.ws, "in", "logo.png"))
        self.policy = PathPolicy(self.ws)

    @staticmethod
    def probe(width=640, height=360, fps=30.0, audio=True, duration=6.0):
        return {"duration": duration, "video": {"codec": "h264", "width": width, "height": height, "fps": fps},
                "audio": {"codec": "aac", "channels": 2} if audio else None}

    def executor(self, ops, outputs=None, probes=None):
        from video_editing_skill.executor import Executor
        from video_editing_skill.ffmpeg_skill import FfmpegSkill
        doc = request([{"id": "A", "path": "in/a.mp4"}, {"id": "B", "path": "in/b.mp4"}, {"id": "logo", "path": "in/logo.png", "kind": "image"}],
                      ops, outputs or [{"id": "o", "operation": ops[-1]["id"], "path": "out/o.mp4"}])
        project = parse_request(doc, self.policy)
        ex = Executor(project, FfmpegSkill(self.ws, "0.9.0", ["probe", "cut", "join", "fit", "overlay"]), {"ffmpeg": "6", "ffprobe": "6"},
                      engine_doctor={"ffmpeg": "6", "ffprobe": "6", "ok": True, "missing": [], "available": ["ffmpeg", "ffprobe", "encoder:libx264", "encoder:aac", "filter:xfade", "filter:acrossfade"]})
        ex.source_probes = probes or {"A": self.probe(), "B": self.probe(1280, 720, 25.0), "logo": {"duration": None, "video": {"codec": "png", "width": 120, "height": 40}, "audio": None}}
        ex.durations = {r: Time.from_float(float(p["duration"])) for r, p in ex.source_probes.items() if p.get("duration")}
        ex.clips = build_timelines(project, ex.durations)
        return ex

    def sources_(self):
        return [{"id": "A", "path": "in/a.mp4"}, {"id": "B", "path": "in/b.mp4"}, {"id": "logo", "path": "in/logo.png", "kind": "image"}]


class MediaAndValidationTests(ExecutorHarness):
    """operations.MEDIA (media compatibility) and the executor's profile derivation / pre-execution refusals."""

    def test_media_table_matches_operations_and_contract(self):
        self.assertEqual(set(operations.MEDIA), set(operations.OPERATIONS))
        for t, m in operations.MEDIA.items():
            self.assertTrue(m["requires"]["video"], t)
            self.assertEqual(m["requires"]["image"], t == "OVERLAY", t)
            self.assertEqual(m["requires"]["audio"], t == "OVERLAY", "only OVERLAY needs audio (ffmpeg-skill 0.9.x overlay hangs without it)")
        c = contract.skill_contract()
        self.assertEqual(c["media_compatibility"], operations.media_compatibility())
        for t in c["tools"]:
            self.assertEqual(t["media"]["requires"], operations.MEDIA[t["operation_type"]]["requires"])
        # additive blocks live outside the pinned ones (agents compare the pinned blocks verbatim)
        for k in ("media_compatibility", "graph", "validation", "doctor_shape", "versioning"):
            self.assertIn(k, c)
            self.assertNotIn(k, contract.PINNED_BLOCKS)
        self.assertEqual(tuple(c["versioning"]["pinned_blocks"]), contract.PINNED_BLOCKS)
        self.assertEqual(sorted(c["graph"]["forbidden_keys"]), sorted(operations.FORBIDDEN_KEYS))

    def test_profiles_are_observed_or_derived_never_guessed(self):
        ex = self.executor([{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 2}},
                            {"id": "c", "type": "CONCAT", "inputs": ["t", "B"], "params": {"width": 640, "height": 360, "fps": 30}},
                            {"id": "f", "type": "FIT", "input": "c", "params": {"aspect": "1:1"}},
                            {"id": "r", "type": "RESIZE", "input": "f", "params": {"width": 320}}])
        self.assertEqual(ex.profile("A")["provenance"], "OBSERVED")
        self.assertEqual((ex.profile("t")["width"], ex.profile("t")["audio"], ex.profile("t")["provenance"]), (640, True, "EXPECTED"))
        self.assertEqual((ex.profile("c")["width"], ex.profile("c")["height"], ex.profile("c")["fps"]), (640, 360, 30.0))
        self.assertEqual((ex.profile("f")["width"], ex.profile("f")["height"]), (640, 640), "FIT 1:1 without a width keeps the source width (fit.py rule)")
        self.assertEqual((ex.profile("r")["width"], ex.profile("r")["height"]), (320, 320), "RESIZE keeps the (now square) aspect: 320 x even(320 * 640 / 640)")
        # CONCAT audio: any input with audio -> audio; none -> none; unknown stays unknown
        ex2 = self.executor([{"id": "c", "type": "CONCAT", "inputs": ["A", "B"], "params": {}}],
                            probes={"A": self.probe(audio=False), "B": self.probe(audio=False), "logo": {"duration": None, "video": {"width": 1, "height": 1}, "audio": None}})
        self.assertIs(ex2.profile("c")["audio"], False)
        ex3 = self.executor([{"id": "c", "type": "CONCAT", "inputs": ["A", "B"], "params": {}}],
                            probes={"A": self.probe(audio=False), "B": self.probe(audio=True), "logo": {"duration": None, "video": {"width": 1, "height": 1}, "audio": None}})
        self.assertIs(ex3.profile("c")["audio"], True)

    def test_overlay_without_audio_is_refused_before_execution(self):
        ex = self.executor([{"id": "o", "type": "OVERLAY", "input": "A", "params": {"image": "logo"}}],
                           probes={"A": self.probe(audio=False), "B": self.probe(), "logo": {"duration": None, "video": {"width": 120, "height": 40}, "audio": None}})
        with self.assertRaises(EditError) as cm:
            ex._check_media()
        self.assertEqual((cm.exception.code, cm.exception.details["reason"]), ("INVALID_INPUT", "audio_required"))
        # through an upstream operation too (a TRIM keeps the input's audio profile)
        ex = self.executor([{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1}}, {"id": "o", "type": "OVERLAY", "input": "t", "params": {"image": "logo"}}],
                           probes={"A": self.probe(audio=False), "B": self.probe(), "logo": {"duration": None, "video": {"width": 120, "height": 40}, "audio": None}})
        with self.assertRaises(EditError):
            ex._check_media()
        # a CONCAT that adds audio (one input has it) satisfies OVERLAY
        ex = self.executor([{"id": "c", "type": "CONCAT", "inputs": ["A", "B"], "params": {}}, {"id": "o", "type": "OVERLAY", "input": "c", "params": {"image": "logo"}}],
                           probes={"A": self.probe(audio=False), "B": self.probe(), "logo": {"duration": None, "video": {"width": 120, "height": 40}, "audio": None}})
        ex._check_media()

    def test_engine_gaps_are_refused_before_execution(self):
        ex = self.executor([{"id": "c", "type": "CONCAT", "inputs": ["A", "B"], "params": {}}])
        ex.engine_doctor["available"] = ["ffmpeg", "ffprobe", "encoder:libx264", "encoder:aac"]
        with self.assertRaises(EditError) as cm:
            ex._check_engine()
        self.assertEqual((cm.exception.code, cm.exception.retryable, cm.exception.details["missing"]), ("TOOL_ERROR", False, ["filter:xfade", "filter:acrossfade"]))
        ex.skill.tools.remove("join")
        with self.assertRaises(EditError) as cm:
            ex._check_engine()
        self.assertEqual(cm.exception.details["missing"], ["tool:ffmpeg-skill/join"])

    def test_output_validation_rules(self):
        ex = self.executor([{"id": "r", "type": "RESIZE", "input": "A", "params": {"width": 320, "fps": 25}}])
        op = ex.project.operations["r"]
        ex._validate_media(op, self.probe(320, 180, 25.0), "x")
        for bad, reason in ((self.probe(300, 180, 25.0), "frame_size"), (self.probe(320, 180, 30.0), "fps"), (self.probe(320, 180, 25.0, audio=False), "audio_lost")):
            with self.assertRaises(EditError) as cm:
                ex._validate_media(op, bad, "x")
            self.assertEqual((cm.exception.code, cm.exception.details["reason"]), ("VALIDATION_ERROR", reason))
        ex = self.executor([{"id": "f", "type": "FILL", "input": "A", "params": {"aspect": "1:1"}}])
        ex._validate_media(ex.project.operations["f"], self.probe(640, 640), "x")   # fit.py: width kept, height = even(640 / 1)
        with self.assertRaises(EditError) as cm:
            ex._validate_media(ex.project.operations["f"], self.probe(360, 360), "x")
        self.assertEqual(cm.exception.details["reason"], "frame_size")
        ex = self.executor([{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1}}])
        with self.assertRaises(EditError) as cm:
            ex._validate_media(ex.project.operations["t"], self.probe(1280, 720), "x")
        self.assertEqual(cm.exception.details["reason"], "frame_size")


class ResponseCheckTests(unittest.TestCase):
    """The response self-check refuses every malformed success document the contract forbids."""

    def good_run(self):
        rec = {"operation": "t", "operation_id": "op_" + "a" * 16, "type": "TRIM", "capability": "video.trim", "status": "completed", "skill": "video-editing",
               "skill_version": contract.VERSION, "tool": "ffmpeg-skill/cut", "tool_versions": {"ffmpeg-skill": "0.9.0"}, "idempotency_key": "k" * 64,
               "parameters": {}, "inputs": [], "output": {"path": "/w/x.mp4", "sha256": "b" * 64}, "probe": {"video": {"width": 1, "height": 1}},
               "commands": ["ffmpeg -i x"], "started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:01Z", "seconds": 1.0, "provenance": "OBSERVED"}
        tl = {"duration_known": True, "duration": {"seconds": "1.000000", "rational": "1/1"},
              "tracks": [{"id": "V1", "kind": "video", "segments": [{"source": "A", "source_range": {"start": {"seconds": "0.000000", "rational": "0/1"}, "end": {"seconds": "1.000000", "rational": "1/1"}},
                                                                     "timeline_range": {"start": {"seconds": "0.000000", "rational": "0/1"}, "end": {"seconds": "1.000000", "rational": "1/1"}}, "speed": "1/1"}]}]}
        out = {"id": "o", "operation": "t", "path": "/w/out/o.mp4", "delivered": True, "sha256": "c" * 64, "size": 10, "container": ".mp4", "reused": False,
               "operation_id": "op_" + "a" * 16, "timeline": tl,
               "observation": {"kind": "media.probe", "provenance": "OBSERVED", "source": "ffmpeg-skill/probe@0.9.0", "data": {"video": {"width": 1, "height": 1}}}}
        return {"ok": True, "schema": "video-editing/response@1", "skill": {"id": "video-editing", "version": contract.VERSION}, "status": "completed", "command": "run",
                "engine": {"ffmpeg-skill": "0.9.0"}, "project": {}, "warnings": [],
                "execution": {"status": "completed", "started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:01Z", "work_dir": "/w", "engine": {},
                              "reused": False, "request_sha256": "d" * 64, "sources": [{"id": "A", "kind": "video", "observation": None}], "operations": [rec], "outputs": [out]}}

    def test_good_documents_pass(self):
        from video_editing_skill.response import check_response
        self.assertEqual(check_response(self.good_run(), "run"), [])
        self.assertEqual(check_response({"ok": False, "error": {"code": "TOOL_ERROR", "message": "x", "retryable": True, "details": {}}}), [])
        self.assertEqual(check_response({"ok": True, "schema": "video-editing/response@1", "skill": {"id": "video-editing", "version": contract.VERSION},
                                         "status": "valid", "command": "validate", "project": {}, "warnings": []}, "validate"), [])

    def test_malformed_success_documents_are_refused(self):
        import copy
        from video_editing_skill.response import check_response
        cases = {
            "exit-0 without delivery": lambda d: d["execution"]["outputs"][0].update(delivered=False),
            "missing sha256": lambda d: d["execution"]["outputs"][0].pop("sha256"),
            "bad sha256": lambda d: d["execution"]["outputs"][0].update(sha256="zz"),
            "empty output": lambda d: d["execution"]["outputs"][0].update(size=0),
            "no timeline": lambda d: d["execution"]["outputs"][0].update(timeline=None),
            "timeline without tracks": lambda d: d["execution"]["outputs"][0]["timeline"].update(tracks=[]),
            "duration unknown but given": lambda d: d["execution"]["outputs"][0]["timeline"].update(duration_known=False),
            "inferred observation": lambda d: d["execution"]["outputs"][0]["observation"].update(provenance="INFERRED"),
            "foreign observation source": lambda d: d["execution"]["outputs"][0]["observation"].update(source="ai:model"),
            "observation without video": lambda d: d["execution"]["outputs"][0]["observation"].update(data={}),
            "no outputs": lambda d: d["execution"].update(outputs=[]),
            "no operations": lambda d: d["execution"].update(operations=[]),
            "failed record in a success": lambda d: d["execution"]["operations"][0].update(status="failed", error={"code": "TOOL_ERROR"}),
            "record without commands": lambda d: d["execution"]["operations"][0].update(commands="ffmpeg"),
            "record tool not ffmpeg-skill": lambda d: d["execution"]["operations"][0].update(tool="ffmpeg"),
            "record without OBSERVED probe": lambda d: d["execution"]["operations"][0].update(provenance=None),
            "status reused with completed records": lambda d: (d.update(status="reused"), d["execution"].update(status="reused", reused=True)),
            "status disagrees with execution": lambda d: d.update(status="reused"),
            "wrong schema": lambda d: d.update(schema="video-editing/response@2"),
            "wrong skill": lambda d: d.update(skill={"id": "other", "version": "1"}),
            "unknown status": lambda d: (d.update(status="done"), d["execution"].update(status="done")),
            "plan schema on run": lambda d: d.update(schema="video-editing/plan@1"),
            "sources not a list": lambda d: d["execution"].update(sources=None),
            "bad request hash": lambda d: d["execution"].update(request_sha256="nope"),
        }
        for name, mutate in cases.items():
            d = copy.deepcopy(self.good_run())
            mutate(d)
            self.assertNotEqual(check_response(d, "run"), [], name)
        self.assertNotEqual(check_response({"ok": False, "error": {"code": "NOPE", "message": "x", "retryable": True, "details": {}}}), [])
        self.assertNotEqual(check_response({"ok": False, "error": {"code": "TOOL_ERROR", "message": "", "retryable": "yes", "details": {}}}), [])
        self.assertNotEqual(check_response({"ok": "true"}), [])
        self.assertNotEqual(check_response([]), [])


class DriftClassificationTests(unittest.TestCase):
    def test_additive_versus_breaking(self):
        live = contract.skill_contract()
        saved = json.loads(json.dumps(live))
        saved.pop("media_compatibility")                # the live contract gained a block: additive
        saved["tools"][0].pop("media")
        rep = contract_check.check_saved(saved, live)
        self.assertEqual((rep["status"], rep["compatibility"], rep["problems"]), ("drift", "additive", []))
        self.assertIn("top-level key added: media_compatibility", rep["additions"])
        saved = json.loads(json.dumps(live))
        saved["operations"]["TRIM"]["parameters"]["fade"] = "x"   # a pinned block differs: breaking
        rep = contract_check.check_saved(saved, live)
        self.assertEqual((rep["status"], rep["compatibility"]), ("drift", "breaking"))
        self.assertIn("pinned block operations changed", rep["problems"])
        saved = json.loads(json.dumps(live))
        saved["extra_saved_only"] = 1                              # the live contract lost a block: breaking
        rep = contract_check.check_saved(saved, live)
        self.assertEqual(rep["compatibility"], "breaking")
        run = contract_check.run_check(saved)
        self.assertFalse(run["ok"])
        self.assertTrue(any(p.startswith("[breaking]") for p in run["drift"]["problems"]))
        self.assertEqual(contract_check.check_saved(json.loads(json.dumps(live)), live)["status"], "ok")


class DoctorAvailabilityTests(unittest.TestCase):
    """doctor never reports an operation AVAILABLE unless its tool and every capability it needs are present."""

    def test_operation_availability(self):
        from video_editing_skill.doctor import operation_availability
        from video_editing_skill.ffmpeg_skill import FfmpegSkill
        full = {"ffmpeg": "6", "ffprobe": "6", "ok": True, "missing": [], "available": ["ffmpeg", "ffprobe", "encoder:libx264", "encoder:aac", "filter:xfade", "filter:acrossfade"]}
        skill = FfmpegSkill("/x", "0.9.0", ["probe", "cut", "join", "fit", "overlay"])
        rows = {r["type"]: r for r in operation_availability(skill, full)}
        self.assertEqual(sorted(rows), sorted(operations.OPERATIONS))
        self.assertTrue(all(r["status"] == "AVAILABLE" and r["missing"] == [] for r in rows.values()), rows)
        self.assertEqual(rows["CONCAT"]["tool_id"], "video-editing/concat")
        no_xfade = dict(full, available=[a for a in full["available"] if "xfade" not in a], missing=["filter:xfade", "filter:acrossfade"])
        rows = {r["type"]: r for r in operation_availability(skill, no_xfade)}
        self.assertEqual((rows["CONCAT"]["status"], rows["CONCAT"]["missing"]), ("MISSING", ["filter:xfade", "filter:acrossfade"]))
        self.assertEqual(rows["TRIM"]["status"], "AVAILABLE")
        rows = {r["type"]: r for r in operation_availability(FfmpegSkill("/x", "0.9.0", ["probe", "cut"]), full)}
        self.assertEqual(rows["OVERLAY"]["missing"], ["tool:ffmpeg-skill/overlay"])
        rows = {r["type"]: r for r in operation_availability(FfmpegSkill("/x", "1.2.0", ["probe", "cut", "join", "fit", "overlay"]), full)}
        self.assertTrue(all(r["status"] == "MISSING" for r in rows.values()))
        rows = {r["type"]: r for r in operation_availability(None, None)}
        self.assertTrue(all(r["missing"] == ["ffmpeg-skill"] for r in rows.values()))
        # a doctor that lists nothing (older engine) only disqualifies on explicit `missing`
        rows = {r["type"]: r for r in operation_availability(skill, {"ffmpeg": "6", "ffprobe": "6", "ok": True, "missing": ["encoder:aac"], "available": None})}
        self.assertEqual(rows["CUT"]["missing"], ["encoder:aac"])
        self.assertEqual(rows["CUT"]["status"], "MISSING")


class FrameSemanticsAndEncodingTests(ExecutorHarness):
    """RESIZE / FIT / FILL targets are normalized before execution with ffmpeg-skill fit.py's own rule and verified exactly;
    the encoding profile is typed, closed, part of identity, and refused where it could not apply."""

    def test_even_and_targets_follow_fit_py(self):
        self.assertEqual([operations.even(x) for x in (168.75, 140.625, 405, 721.78, 360, 361, 2275.5)], [170, 142, 406, 722, 360, 362, 2276])
        cases = [  # (op params, source frame) -> target, mirrors fit.py: out_w = width or (sw if ratio <= src_ratio else sh * ratio); out_h = even(out_w / ratio)
            ({"type": "RESIZE", "params": {"width": 300}}, (1280, 720), (300, 170)),
            ({"type": "RESIZE", "params": {"width": 250}}, (640, 360), (250, 142)),
            ({"type": "RESIZE", "params": {"width": 320}}, (640, 360), (320, 180)),
            ({"type": "FIT", "params": {"aspect": "9:16"}}, (1280, 720), (1280, 2276)),
            ({"type": "FILL", "params": {"aspect": "1:1"}}, (640, 360), (640, 640)),
            ({"type": "FIT", "params": {"aspect": "21:9"}}, (640, 360), (840, 360)),
            ({"type": "FIT", "params": {"aspect": "1:1", "width": 360}}, (640, 360), (360, 360)),
            ({"type": "FILL", "params": {"aspect": "4:3", "width": 334}}, (640, 360), (334, 250)),
        ]
        for op, (sw, sh), want in cases:
            ex = self.executor([dict(op, id="x", input="A")], probes={"A": self.probe(sw, sh), "B": self.probe(), "logo": {"duration": None, "video": {"width": 1, "height": 1}, "audio": None}})
            self.assertEqual(ex.target_frame("x"), want, (op, sw, sh))
            self.assertEqual(ex.normalized("x")["target_frame"], list(want))
        # rotation metadata: the source is measured as displayed (640x360 stored, rotation 90 -> 360x640)
        rotated = self.probe(640, 360)
        rotated["video"]["rotation"] = 90
        ex = self.executor([{"id": "x", "type": "RESIZE", "input": "A", "params": {"width": 180}}], probes={"A": rotated, "B": self.probe(), "logo": {"duration": None, "video": {"width": 1, "height": 1}, "audio": None}})
        self.assertEqual((ex.profile("A")["width"], ex.profile("A")["height"]), (360, 640))
        self.assertEqual(ex.target_frame("x"), (180, 320))
        # CONCAT (join.py): both given -> as given; one given -> the other from the first input's aspect, rounded; none -> the first input; floored to even
        odd = {"A": self.probe(641, 361), "B": self.probe(), "logo": {"duration": None, "video": {"width": 1, "height": 1}, "audio": None}}
        ex = self.executor([{"id": "c", "type": "CONCAT", "inputs": ["A", "B"], "params": {}}], probes=odd)
        self.assertEqual(ex.target_frame("c"), (640, 360))
        for params, first, want in (({"width": 300}, (640, 360), (300, 168)), ({"width": 300}, (360, 640), (300, 532)), ({"height": 250}, (640, 360), (444, 250)),
                                    ({"height": 250}, (360, 640), (140, 250)), ({"width": 1000}, (640, 360), (1000, 562)), ({"width": 300, "height": 250}, (360, 640), (300, 250))):
            ex = self.executor([{"id": "c", "type": "CONCAT", "inputs": ["A", "B"], "params": params}],
                               probes={"A": self.probe(*first), "B": self.probe(), "logo": {"duration": None, "video": {"width": 1, "height": 1}, "audio": None}})
            self.assertEqual(ex.target_frame("c"), want, (params, first))
        # an odd input frame: normalized to even by RESIZE / FIT / FILL / CONCAT, refused up front by the frame-keeping operations
        for op in ({"type": "TRIM", "params": {"start": 0, "end": 1}}, {"type": "CUT", "params": {"keep": [{"start": 0, "end": 1}]}}, {"type": "SPEED", "params": {"factor": 2}},
                   {"type": "OVERLAY", "params": {"image": "logo"}}):
            ex = self.executor([dict(op, id="x", input="A")], probes=dict(odd, logo={"duration": None, "video": {"width": 120, "height": 40}, "audio": None}))
            with self.assertRaises(EditError, msg=op["type"]) as cm:
                ex._check_media()
            self.assertEqual((cm.exception.code, cm.exception.details["reason"], cm.exception.details["frame"]), ("INVALID_INPUT", "odd_frame", [641, 361]))
        for op in ({"type": "RESIZE", "params": {"width": 202}}, {"type": "FIT", "params": {"aspect": "1:1"}}, {"type": "FILL", "params": {"aspect": "9:16"}}):
            ex = self.executor([dict(op, id="x", input="A")], probes=odd)
            ex._check_media()
            self.assertTrue(all(v % 2 == 0 for v in ex.target_frame("x")), op)
        # the semantics block names the three types and no operation stretches
        sem = contract.skill_contract()["frame_semantics"]
        self.assertEqual(set(sem) - {"rules"}, {"RESIZE", "FIT", "FILL"})
        self.assertTrue(any("stretch" in r for r in sem["rules"]))

    def test_hdr_vfr_and_codec(self):
        hdr = self.probe()
        hdr["video"]["hdr"] = True
        vfr = self.probe()
        vfr["video"]["variable_frame_rate_suspected"] = True
        ex = self.executor([{"id": "c", "type": "CONCAT", "inputs": ["A", "B"], "params": {}}], probes={"A": hdr, "B": self.probe(), "logo": {"duration": None, "video": {"width": 1, "height": 1}, "audio": None}})
        with self.assertRaises(EditError) as cm:
            ex._check_media()
        self.assertEqual((cm.exception.code, cm.exception.details["reason"]), ("INVALID_INPUT", "hdr_mismatch"))
        ex = self.executor([{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1}}], probes={"A": hdr, "B": vfr, "logo": {"duration": None, "video": {"width": 1, "height": 1}, "audio": None}})
        ex._check_media()
        self.assertTrue(any("HDR" in w for w in ex.project.warnings) and any("variable-frame-rate" in w for w in ex.project.warnings), ex.project.warnings)
        self.assertEqual(ex.normalized("t")["video_codec"], "hevc")
        out = self.probe()
        out["video"]["codec"] = "h264"
        with self.assertRaises(EditError) as cm:
            ex._validate_media(ex.project.operations["t"], out, "x")
        self.assertEqual(cm.exception.details["reason"], "codec")
        out["video"]["codec"] = "hevc"
        ex._validate_media(ex.project.operations["t"], out, "x")
        # a keyframe TRIM may stream-copy: no codec claim
        ex = self.executor([{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1, "precision": "keyframe"}}], probes={"A": hdr, "B": self.probe(), "logo": {"duration": None, "video": {"width": 1, "height": 1}, "audio": None}})
        self.assertIsNone(ex.normalized("t")["encoding"])
        ex._validate_media(ex.project.operations["t"], self.probe(), "x")

    def test_encoding_profile_is_typed_closed_and_part_of_identity(self):
        from video_editing_skill.compiler import compile_operation
        for good in ({"crf": 18}, {"preset": "veryfast"}, {"crf": 14, "preset": "veryslow"}, {"crf": 28}):
            self.assertEqual(operations.validate_encoding(good, "x"), good)
        for bad in ({}, {"crf": 13}, {"crf": 29}, {"crf": "18"}, {"crf": 18.5}, {"preset": "placebo"}, {"preset": "medium --tune film"}, {"codec": "libx264"},
                    {"bitrate": "5M"}, {"filter": "x"}, {"crf": True}, "fast", ["fast"]):
            with self.assertRaises(EditError, msg=str(bad)) as cm:
                operations.validate_encoding(bad, "x")
            self.assertEqual(cm.exception.code, "INVALID_REQUEST")
        base = [{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1}}]
        plain = parse_request(request(self.sources_(), base, [{"id": "o", "operation": "t", "path": "out/o.mp4"}]), self.policy)
        prof = parse_request(request(self.sources_(), base, [{"id": "o", "operation": "t", "path": "out/o.mp4", "encoding": {"crf": 20, "preset": "fast"}}]), self.policy)
        self.assertNotEqual(plain.operations["t"].operation_id, prof.operations["t"].operation_id, "a profile is part of the identity")
        again = parse_request(request(self.sources_(), base, [{"id": "o", "operation": "t", "path": "out/o.mp4", "encoding": {"preset": "fast", "crf": 20}}]), self.policy)
        self.assertEqual(prof.operations["t"].operation_id, again.operations["t"].operation_id, "key order does not matter")
        self.assertEqual(prof.outputs["o"].encoding, {"crf": 20, "preset": "fast"})
        self.assertEqual(prof.operations["t"].to_dict()["encoding"], {"crf": 20, "preset": "fast"})
        step = compile_operation(prof.operations["t"])
        argv = step.argv_for(["/in/a.mp4"], "/ws/o.mp4")
        self.assertEqual(argv[argv.index("--crf") + 1], "20")
        self.assertEqual(argv[argv.index("--preset") + 1], "fast")
        self.assertNotIn("--crf", compile_operation(plain.operations["t"]).argv_for(["/in/a.mp4"], "/ws/o.mp4"), "no profile: the engine's defaults, no flags")
        # two outputs of one operation must agree; keyframe precision cannot take a profile
        with self.assertRaises(EditError) as cm:
            parse_request(request(self.sources_(), base, [{"id": "o", "operation": "t", "path": "out/o.mp4", "encoding": {"crf": 20}},
                                                          {"id": "p", "operation": "t", "path": "out/p.mp4", "encoding": {"crf": 22}}]), self.policy)
        self.assertEqual((cm.exception.code, cm.exception.details["reason"]), ("DEPENDENCY_ERROR", "conflicting_encodings"))
        parse_request(request(self.sources_(), base, [{"id": "o", "operation": "t", "path": "out/o.mp4", "encoding": {"crf": 20}},
                                                      {"id": "p", "operation": "t", "path": "out/p.mp4", "encoding": {"crf": 20}}]), self.policy)
        with self.assertRaises(EditError) as cm:
            parse_request(request(self.sources_(), [{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1, "precision": "keyframe"}}],
                                  [{"id": "o", "operation": "t", "path": "out/o.mp4", "encoding": {"crf": 20}}]), self.policy)
        self.assertEqual(cm.exception.details["reason"], "encoding_needs_reencode")
        # the contract's encoding block is the source of truth for the vocabulary
        c = contract.skill_contract()
        self.assertEqual(c["encoding"], operations.ENCODING)
        self.assertIn("video codec choice", c["encoding"]["not_configurable"])
        self.assertEqual(c["request_shape"], json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "contract", "contract.json")))["request_shape"])

    def test_pinned_blocks_are_unchanged_since_0_1_0(self):
        """The blocks video-production-agent pins (PR #18 / #19) must be byte-identical to the 0.1.0 snapshot the agent carries."""
        c = contract.skill_contract()
        golden = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "contract", "contract.json")))
        for k in ("schema", "skill_id", "version", "operations", "unsupported", "errors", "execution", "capabilities", "capability_names", "schemas",
                  "engine", "response_shape", "request_shape", "formats"):
            self.assertEqual(c[k], golden[k], k)
        self.assertEqual(c["version"], "0.1.0")
        for t in c["tools"]:
            g = next(x for x in golden["tools"] if x["tool_id"] == t["tool_id"])
            for f in ("parameters", "required_capabilities", "inputs", "result_keys", "executed_by", "deterministic", "produces_output", "writes_media", "kind"):
                self.assertEqual(t[f], g[f], (t["tool_id"], f))
