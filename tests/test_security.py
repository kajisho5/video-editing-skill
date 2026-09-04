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
        with open(os.path.join(PKG, "project.py"), encoding="utf-8") as fh:
            text = fh.read()
        for k in ("command", "argv", "shell", "executable", "filter"):
            self.assertIn(f'"{k}"', text)


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
