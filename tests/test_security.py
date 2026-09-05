"""Security boundary: static analysis of the package plus black-box attempts through the CLI.

Static: no shell, no eval/exec, no dynamic imports, subprocess only with argv lists built in the package.
Black-box: command / argv / executable / filter keys, shell metacharacters, malformed JSON, oversized input,
traversal and absolute outputs are all refused with a structured error and never reach a subprocess.
"""
import ast
import json
import os
import re
import unittest

from helpers import SRC, cli, make_workspace, request, write_fake_media

PKG = os.path.join(SRC, "video_editing_skill")
FORBIDDEN_TEXT = [
    (re.compile(r"\bos\.system\s*\("), "os.system"),
    (re.compile(r"\bos\.popen\s*\("), "os.popen"),
    (re.compile(r"shell\s*=\s*True"), "shell=True"),
    (re.compile(r"(?<![\w.])eval\s*\("), "eval("),
    (re.compile(r"(?<![\w.])exec\s*\("), "exec("),
    (re.compile(r"\bimportlib\b"), "importlib"),
    (re.compile(r"__import__"), "__import__"),
    (re.compile(r"\bimport\s+(pty|commands|shlex)\b"), "shell helpers"),
]
FORBIDDEN_IMPORTS = {"video_agent", "anthropic", "openai", "requests", "httpx", "urllib", "socket", "http"}
SUBPROCESS_FUNCS = {"run", "Popen", "call", "check_call", "check_output"}


def _sources():
    for name in sorted(os.listdir(PKG)):
        if name.endswith(".py"):
            with open(os.path.join(PKG, name), encoding="utf-8") as fh:
                yield name, fh.read()


class StaticTests(unittest.TestCase):
    def test_no_shell_primitives(self):
        for name, text in _sources():
            for rx, label in FORBIDDEN_TEXT:
                self.assertIsNone(rx.search(text), f"{name}: {label}")

    def test_no_network_or_agent_imports(self):
        for name, text in _sources():
            tree = ast.parse(text)
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    mods = [node.module.split(".")[0]]
                for m in mods:
                    self.assertNotIn(m, FORBIDDEN_IMPORTS, f"{name} imports {m}")

    def test_subprocess_only_with_lists(self):
        """Every subprocess call's first argument is a list literal or a list-typed name; never a string."""
        for name, text in _sources():
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                is_sub = isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "subprocess" and f.attr in SUBPROCESS_FUNCS
                if not is_sub:
                    continue
                self.assertTrue(node.args, f"{name}: subprocess call without argv")
                first = node.args[0]
                self.assertIsInstance(first, (ast.List, ast.Name, ast.BinOp), f"{name}: subprocess argv must be a list")
                self.assertNotIsInstance(first, (ast.Constant, ast.JoinedStr), f"{name}: string command")
                for kw in node.keywords:
                    self.assertNotEqual(kw.arg, "shell", f"{name}: shell kwarg")
        # the only process launchers are in ffmpeg_skill.py (engine) and the fixture-free tool_versions
        for name, text in _sources():
            if name != "ffmpeg_skill.py":
                self.assertNotIn("subprocess.Popen", text, f"{name} launches processes")

    def test_request_cannot_name_executables(self):
        from video_editing_skill.operations import FORBIDDEN_KEYS
        with open(os.path.join(PKG, "operations.py"), encoding="utf-8") as fh:
            text = fh.read()
        for k in ("command", "argv", "shell", "executable", "filter", "filter_complex", "env", "api_key", "workspace", "ffmpeg_skill_dir", "allowed_input"):
            self.assertIn(k, FORBIDDEN_KEYS)
            self.assertIn(f'"{k}"', text)
        with open(os.path.join(PKG, "project.py"), encoding="utf-8") as fh:
            self.assertIn("FORBIDDEN_KEYS", fh.read(), "the request parser applies the forbidden-key list at every level")


class BlackBoxTests(unittest.TestCase):
    def setUp(self):
        self.ws = make_workspace()
        write_fake_media(os.path.join(self.ws, "in", "a.mp4"))
        write_fake_media(os.path.join(self.ws, "in", "b.mp4"))
        self.env = {"VIDEO_EDITING_FFMPEG_SKILL_DIR": "/nonexistent"}

    def good(self):
        return request([{"id": "A", "path": "in/a.mp4"}, {"id": "B", "path": "in/b.mp4"}],
                       [{"id": "c", "type": "CONCAT", "inputs": ["A", "B"], "params": {}}],
                       [{"id": "o", "operation": "c", "path": "out/o.mp4"}])

    def validate(self, doc):
        return cli(["validate", "-", "--json", "--workspace", self.ws], stdin=json.dumps(doc).encode(), env=self.env)

    def assert_refused(self, doc, code, reason=None):
        rc, out, err = self.validate(doc)
        self.assertIsInstance(out, dict, err)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], code, out["error"])
        self.assertNotEqual(rc, 0)
        if reason:
            self.assertEqual(out["error"]["details"].get("reason"), reason, out["error"])

    def test_valid_request_passes(self):
        rc, out, err = self.validate(self.good())
        self.assertEqual(rc, 0, err)
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "valid")

    def test_command_injection_keys(self):
        for key in ("command", "argv", "shell", "executable", "cmd", "script", "filter_complex", "ffmpeg", "env"):
            d = self.good()
            d["project"]["operations"][0][key] = "rm -rf /"
            self.assert_refused(d, "INVALID_REQUEST", "forbidden_key")
        d = self.good()
        d["project"]["operations"][0]["params"]["argv"] = ["-i", "x"]
        self.assert_refused(d, "INVALID_REQUEST")

    def test_shell_metacharacters_in_values(self):
        for meta in ("a.mp4; rm -rf /", "$(id).mp4", "`id`.mp4", "a|b.mp4", "a&&b.mp4", "in/a.mp4\n--json"):
            d = self.good()
            d["project"]["sources"][0]["path"] = meta
            rc, out, err = self.validate(d)
            self.assertFalse(out["ok"], meta)
            self.assertIn(out["error"]["code"], ("MISSING_INPUT", "PATH_NOT_ALLOWED", "INVALID_REQUEST"), meta)
        d = self.good()
        d["project"]["operations"][0]["params"]["pad_color"] = "black;system(id)"
        self.assert_refused(d, "INVALID_REQUEST")
        d = self.good()
        d["project"]["operations"][0]["params"]["transition"] = {"type": "fade:eval=1", "duration": 1}
        self.assert_refused(d, "INVALID_REQUEST")

    def test_executable_and_filter_injection(self):
        d = self.good()
        d["project"]["operations"][0] = {"id": "c", "type": "ffmpeg-skill/cut", "inputs": ["A", "B"], "params": {}}
        self.assert_refused(d, "UNSUPPORTED_OPERATION")
        d = self.good()
        d["project"]["sources"].append({"id": "E", "path": "/bin/sh", "kind": "video"})
        rc, out, _ = self.validate(d)
        self.assertIn(out["error"]["code"], ("PATH_NOT_ALLOWED", "UNSUPPORTED_FORMAT"))

    def test_paths(self):
        d = self.good()
        d["project"]["outputs"][0]["path"] = "../escape.mp4"
        self.assert_refused(d, "PATH_NOT_ALLOWED", "traversal")
        d = self.good()
        d["project"]["outputs"][0]["path"] = "/tmp/escape.mp4"
        self.assert_refused(d, "PATH_NOT_ALLOWED", "absolute_output")
        d = self.good()
        d["project"]["outputs"][0]["path"] = "C:\\escape.mp4"
        self.assert_refused(d, "PATH_NOT_ALLOWED", "absolute_output")
        d = self.good()
        d["project"]["sources"][0]["path"] = "/etc/hostname"
        self.assert_refused(d, "PATH_NOT_ALLOWED")
        d = self.good()
        d["project"]["outputs"][0]["path"] = "in/a.mp4"
        self.assert_refused(d, "PATH_NOT_ALLOWED", "overwrite_input")
        d = self.good()
        d["project"]["outputs"][0]["path"] = "out/aux.mp4"
        self.assert_refused(d, "PATH_NOT_ALLOWED", "reserved_name")

    def test_request_cannot_set_workspace(self):
        d = self.good()
        d["options"] = {"workspace": "/", "allowed_input_roots": ["/"]}
        self.assert_refused(d, "INVALID_REQUEST")

    def test_malformed_json(self):
        for raw in (b"{not json", b"", b"\xff\xfe", b"[1,2]", b'"string"', b"null"):
            rc, out, err = cli(["validate", "-", "--json", "--workspace", self.ws], stdin=raw, env=self.env)
            self.assertIsInstance(out, dict, (raw, err))
            self.assertFalse(out["ok"])
            self.assertEqual(out["error"]["code"], "INVALID_REQUEST")
            self.assertEqual(rc, 2)

    def test_oversized(self):
        d = self.good()
        d["project"]["id"] = "p"
        raw = json.dumps(d).encode() + b" " * (5 * 1024 * 1024)
        rc, out, _ = cli(["validate", "-", "--json", "--workspace", self.ws], stdin=raw, env=self.env)
        self.assertEqual(out["error"]["details"].get("reason"), "oversized")
        d = self.good()
        d["project"]["operations"] = [{"id": f"t{i}", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1}} for i in range(600)]
        self.assert_refused(d, "INVALID_REQUEST")

    def test_stdout_is_json_only(self):
        rc, out, err = cli(["validate", "-", "--json", "--workspace", self.ws, "--verbose"], stdin=b"{", env=self.env)
        self.assertIsInstance(out, dict)
        self.assertIn("error:", err)

    def test_missing_workspace_flag(self):
        rc, out, _ = cli(["validate", "-", "--json"], stdin=json.dumps(self.good()).encode(), env=self.env)
        self.assertEqual(out["error"]["code"], "INVALID_REQUEST")

    def test_engine_missing_is_tool_error(self):
        rc, out, _ = cli(["plan", "-", "--json", "--workspace", self.ws], stdin=json.dumps(self.good()).encode(), env=self.env)
        self.assertEqual(out["error"]["code"], "TOOL_ERROR")
        self.assertFalse(out["error"]["retryable"])
        self.assertEqual(rc, 10)


if __name__ == "__main__":
    unittest.main()


class InjectionTests(unittest.TestCase):
    """argv / environment / boundary injection through request values and keys: everything is typed, so a value that
    looks like a flag, a shell fragment or an environment reference is refused as an invalid parameter or path, never
    forwarded; boundary settings (workspace, allowed roots, engine location) are CLI flags the request cannot carry."""

    def setUp(self):
        self.ws = make_workspace()
        write_fake_media(os.path.join(self.ws, "in", "a.mp4"))
        write_fake_media(os.path.join(self.ws, "in", "logo.png"))
        self.env = {"VIDEO_EDITING_FFMPEG_SKILL_DIR": "/nonexistent"}

    def doc(self, params, t="OVERLAY", extra_top=None):
        d = request([{"id": "A", "path": "in/a.mp4"}, {"id": "logo", "path": "in/logo.png", "kind": "image"}],
                    [{"id": "x", "type": t, "input": "A", "params": params}], [{"id": "o", "operation": "x", "path": "out/o.mp4"}])
        if extra_top:
            d.update(extra_top)
        return d

    def refused(self, doc, code="INVALID_REQUEST", reason=None):
        rc, out, err = cli(["validate", "-", "--json", "--workspace", self.ws], stdin=json.dumps(doc).encode(), env=self.env)
        self.assertIsInstance(out, dict, err)
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["error"]["code"], code, out["error"])
        if reason:
            self.assertEqual(out["error"]["details"].get("reason"), reason, out["error"])
        self.assertNotEqual(rc, 0)

    def test_argv_injection_in_values(self):
        for pos in ("--crf 0", "-o /tmp/x", "top-right --preset ultrafast", "10,10;rm -rf /", "$(id)", "`id`", "10|10"):
            self.refused(self.doc({"image": "logo", "position": pos}))
        self.refused(self.doc({"image": "logo", "margin": "24 --fast"}))
        self.refused(self.doc({"image": "logo", "scale": "--scale-percent 200"}))
        self.refused(self.doc({"image": "logo", "start": "--dry-run"}))
        self.refused(self.doc({"aspect": "16:9 --fit crop"}, t="FIT"))
        self.refused(self.doc({"aspect": "16:9", "pad_color": "black,drawtext=text=x"}, t="FIT"))
        self.refused(self.doc({"factor": "2 --smooth interpolate"}, t="SPEED"))

    def test_environment_and_boundary_keys(self):
        for key in ("env", "environment", "cwd", "pythonpath", "workspace", "allowed_input", "allowed_inputs", "ffmpeg_skill_dir", "api_key", "token"):
            self.refused(self.doc({"image": "logo", key: "x"}), reason="forbidden_key")
            self.refused(self.doc({"image": "logo"}, extra_top={key: "x"}), reason="forbidden_key")
            d = self.doc({"image": "logo"})
            d["project"][key] = "x"
            self.refused(d, reason="forbidden_key")
            d = self.doc({"image": "logo"})
            d["options"] = {key: "x"}
            self.refused(d, reason="forbidden_key")
        # an environment reference in a path is a literal file name, never expanded
        d = self.doc({"image": "logo"})
        d["project"]["sources"][0]["path"] = "$HOME/in/a.mp4"
        self.refused(d, "MISSING_INPUT")
        d["project"]["sources"][0]["path"] = "%USERPROFILE%/in/a.mp4"
        self.refused(d, "MISSING_INPUT")
        d["project"]["sources"][0]["path"] = "~/in/a.mp4"
        self.refused(d, "MISSING_INPUT")

    def test_unknown_keys_and_wrong_types(self):
        self.refused(self.doc({"image": "logo", "crf": 0}))
        self.refused(self.doc({"image": "logo", "position": 5}))
        self.refused(self.doc({"image": "logo", "opacity": "1"}))
        self.refused(self.doc({"image": "logo", "margin": 2.5}))
        self.refused(self.doc({"image": "logo", "margin": True}))
        self.refused(self.doc({"width": "640"}, t="RESIZE"))
        self.refused(self.doc({"width": 641}, t="RESIZE"))
        self.refused(self.doc({"width": 640, "height": 360}, t="RESIZE"))
        self.refused(self.doc({"factor": 0}, t="SPEED"))
        self.refused(self.doc({"factor": 8}, t="SPEED"))
        self.refused(self.doc({"start": 2, "end": 1}, t="TRIM"), "INVALID_TIME_RANGE")
        self.refused(self.doc({"keep": []}, t="CUT"))
        self.refused(self.doc({"keep": [{"start": 0, "end": 1, "x": 1}]}, t="CUT"))
        self.refused(self.doc({"image": "logo"}, t="FREEZE"), "UNSUPPORTED_OPERATION")
        self.refused(self.doc({"image": "logo"}, t="overlay"), "UNSUPPORTED_OPERATION")


class ArgvAuditTests(unittest.TestCase):
    """Every argv token this skill hands to ffmpeg-skill is a flag from the allowlist, a closed-vocabulary word, a number /
    time / range / ratio, or a resolved path. There is no token a request string can shape freely: argument injection
    through typed parameters is impossible by construction, and this test pins it for every operation type."""

    FLAG = re.compile(r"^--[a-z][a-z-]*(=-?[0-9.,/:-]+)?$")
    VALUE = re.compile(r"^-?[0-9]+(\.[0-9]+)?(/[0-9]+)?$|^[0-9.]+-[0-9.]+(,[0-9.]+-[0-9.]+)*$|^[0-9]+:[0-9]+$|^-?[0-9]+,-?[0-9]+$|^[a-z][a-z-]*$|^0x[0-9A-Fa-f]{6}$")

    def test_every_token_is_typed(self):
        from video_editing_skill.compiler import ALLOWED_FLAGS, compile_operation
        from video_editing_skill.operations import validate_encoding, validate_params
        from video_editing_skill.project import EditOperation
        from video_editing_skill.timebase import Time
        samples = {
            "TRIM": {"start": "0:01.5", "end": {"frames": 90, "fps": "30000/1001"}, "precision": "frame"},
            "CUT": {"keep": [{"start": 4, "end": 5.25}, {"start": "1:00", "end": "1:02.5"}], "precision": "keyframe"},
            "CONCAT": {"transition": {"type": "wipeleft", "duration": 0.75}, "width": 1920, "height": 1080, "fps": "30000/1001", "mode": "crop", "pad_color": "0xA0B0C0"},
            "SPEED": {"factor": "3/2"},
            "FIT": {"aspect": "9:16", "width": 1080, "pad_color": "white", "fps": 25},
            "FILL": {"aspect": "4:5", "width": 1080},
            "RESIZE": {"width": 1280, "fps": "24000/1001"},
            "OVERLAY": {"image": "logo", "position": {"x": -10, "y": 10}, "margin": 0, "scale": 200, "opacity": 0.5, "start": 1, "end": 2, "fade": 0.5},
        }
        paths = {"a": "/媒体 media/clip 1.mp4", "b": "/媒体 media/clip 2.mp4", "logo": "/媒体 media/logo (1).png", "out": "/ws/out dir/o.mp4"}
        for t, params in samples.items():
            op = EditOperation("x", t, ["a", "b", "logo"] if t == "CONCAT" else (["a", "logo"] if t == "OVERLAY" else ["a"]), validate_params(t, params, t))
            op.encoding = validate_encoding({"crf": 20, "preset": "veryfast"}, "e") if t != "CUT" else None
            step = compile_operation(op)
            argv = step.argv_for([paths["a"], paths["b"]] if t == "CONCAT" else [paths["a"]], paths["out"], paths["logo"] if t == "OVERLAY" else None, Time.parse(10))
            allowed = {"--" + f.replace("_", "-") for f in ALLOWED_FLAGS[step.script]} | {"-o"}
            for tok in argv:
                if tok in paths.values():
                    continue
                if tok.startswith("-"):
                    self.assertTrue(self.FLAG.match(tok) or tok == "-o", (t, tok))
                    self.assertIn(tok.split("=", 1)[0], allowed, (t, tok))
                else:
                    self.assertTrue(self.VALUE.match(tok), (t, tok))
            self.assertNotIn("--filter", " ".join(argv))
            self.assertNotIn("--filter-complex", " ".join(argv))

    def test_child_environment_is_an_allowlist(self):
        from video_editing_skill.ffmpeg_skill import clean_env
        poisoned = {"LD_PRELOAD": "/evil.so", "DYLD_INSERT_LIBRARIES": "/evil.dylib", "PYTHONPATH": "/evil", "PYTHONSTARTUP": "/evil.py", "FFMPEG_BINARY": "/evil",
                    "VIDEO_EDITING_FFMPEG_SKILL_DIR": "/evil", "AV_LOG_FORCE_COLOR": "1", "HTTP_PROXY": "http://x", "AWS_SECRET_ACCESS_KEY": "s", "PATH": os.environ.get("PATH", "/usr/bin")}
        saved = dict(os.environ)
        try:
            os.environ.update(poisoned)
            env = clean_env()
        finally:
            os.environ.clear()
            os.environ.update(saved)
        for k in poisoned:
            if k != "PATH":
                self.assertNotIn(k, env, k)
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertIn("PATH", env)

    def test_encoding_profile_cannot_smuggle_flags(self):
        ws = make_workspace()
        write_fake_media(os.path.join(ws, "in", "a.mp4"))
        for enc in ({"preset": "medium -tune film"}, {"preset": "medium;id"}, {"crf": "18 --preset ultrafast"}, {"crf": 18, "x264opts": "x"}, {"filter": "x"}, {"codec": "libx265"}):
            doc = request([{"id": "A", "path": "in/a.mp4"}], [{"id": "t", "type": "TRIM", "input": "A", "params": {"start": 0, "end": 1}}],
                          [{"id": "o", "operation": "t", "path": "out/o.mp4", "encoding": enc}])
            rc, out, err = cli(["validate", "-", "--json", "--workspace", ws], stdin=json.dumps(doc).encode(), env={"VIDEO_EDITING_FFMPEG_SKILL_DIR": "/nonexistent"})
            self.assertIsInstance(out, dict, err)
            self.assertEqual(out["error"]["code"], "INVALID_REQUEST", enc)
