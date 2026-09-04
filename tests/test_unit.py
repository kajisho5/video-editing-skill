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
            (lambda d: d.update(version="9.9.9"), "version changed"),
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
