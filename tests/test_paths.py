"""Path security: traversal, symlink escape, prefix collisions, Windows semantics, reserved names, outputs."""
import ntpath
import os
import posixpath
import unittest

from helpers import make_workspace, write_fake_media

from video_editing_skill.errors import EditError
from video_editing_skill.paths import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, PathPolicy, has_traversal, is_within, reserved_component


class ContainmentTests(unittest.TestCase):
    def test_posix(self):
        self.assertTrue(is_within("/w/media", "/w/media/a.mp4", posixpath))
        self.assertTrue(is_within("/w/media", "/w/media", posixpath))
        self.assertFalse(is_within("/w/media", "/w/media_evil/a.mp4", posixpath))
        self.assertFalse(is_within("/w/media", "/w/media/../secret.mp4", posixpath))
        self.assertFalse(is_within("/w/media", "/etc/passwd", posixpath))
        self.assertFalse(is_within("/w/media", "relative/a.mp4", posixpath))

    def test_windows_semantics(self):
        self.assertTrue(is_within(r"C:\w\media", r"C:\w\media\a.mp4", ntpath))
        self.assertTrue(is_within(r"C:\w\media", r"c:\W\MEDIA\A.MP4", ntpath))
        self.assertTrue(is_within(r"C:\w\media", "C:/w/media/sub/a.mp4", ntpath))
        self.assertFalse(is_within(r"C:\w\media", r"C:\w\media_evil\a.mp4", ntpath))
        self.assertFalse(is_within(r"C:\w\media", r"C:\w\media\..\secret.mp4", ntpath))
        self.assertFalse(is_within(r"C:\w\media", r"D:\w\media\a.mp4", ntpath))
        self.assertFalse(is_within(r"C:\w\media", r"\\server\share\a.mp4", ntpath))
        self.assertFalse(is_within(r"\\server\share\media", r"\\server\share\media_evil\a.mp4", ntpath))
        self.assertTrue(is_within(r"\\server\share\media", r"\\server\share\media\a.mp4", ntpath))
        self.assertFalse(is_within(r"C:\w\media", r"\w\media\a.mp4", ntpath))

    def test_traversal_and_reserved(self):
        self.assertTrue(has_traversal("../x"))
        self.assertTrue(has_traversal("a\\..\\x"))
        self.assertFalse(has_traversal("a/..b/x"))
        for bad in ("CON", "con.mp4", "out/NUL.mp4", "COM1.mov", "LPT9", "clip?.mp4", "a<b.mp4", "trailing. ", "x/y./z.mp4", 'q"uote.mp4', "C:\\x\\aux.txt"):
            self.assertIsNotNone(reserved_component(bad), bad)
        for ok in ("console.mp4", "out/final.mp4", "C:\\w\\a.mp4", "comma,x.mp4", "consul/lpt10.mp4", "コピー.mp4", ".", "./out/a.mp4", "a/./b.mp4"):
            self.assertIsNone(reserved_component(ok), ok)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.ws = make_workspace()
        self.media = write_fake_media(os.path.join(self.ws, "in", "a.mp4"))
        self.outside = make_workspace()
        self.secret = write_fake_media(os.path.join(self.outside, "in", "secret.mp4"))
        self.policy = PathPolicy(self.ws)

    def test_default_root_is_workspace(self):
        self.assertEqual(self.policy.resolve_input("in/a.mp4", "x", VIDEO_EXTENSIONS), os.path.realpath(self.media))
        self.assertEqual(self.policy.resolve_input(self.media, "x", VIDEO_EXTENSIONS), os.path.realpath(self.media))

    def test_outside_root(self):
        with self.assertRaises(EditError) as cm:
            self.policy.resolve_input(self.secret, "x", VIDEO_EXTENSIONS)
        self.assertEqual(cm.exception.code, "PATH_NOT_ALLOWED")
        self.assertEqual(cm.exception.details["reason"], "outside_allowed_roots")
        self.assertNotIn(self.outside, cm.exception.message)  # no path leak
        with self.assertRaises(EditError) as cm:
            self.policy.resolve_input("../" + os.path.basename(self.outside) + "/in/secret.mp4", "x", VIDEO_EXTENSIONS)
        self.assertEqual(cm.exception.details["reason"], "traversal")

    def test_allowed_roots(self):
        p = PathPolicy(self.ws, [self.outside])
        self.assertEqual(p.resolve_input(self.secret, "x", VIDEO_EXTENSIONS), os.path.realpath(self.secret))
        with self.assertRaises(EditError):
            p.resolve_input(self.media, "x", VIDEO_EXTENSIONS)  # workspace is no longer a root when roots are given
        with self.assertRaises(EditError):
            PathPolicy(self.ws, [os.path.join(self.ws, "nope")])
        with self.assertRaises(EditError):
            PathPolicy(os.path.join(self.ws, "missing"))

    def test_prefix_collision(self):
        evil = make_workspace()
        sibling = self.ws + "_evil"
        os.rename(evil, sibling)
        try:
            f = write_fake_media(os.path.join(sibling, "in", "a.mp4"))
            with self.assertRaises(EditError):
                self.policy.resolve_input(f, "x", VIDEO_EXTENSIONS)
        finally:
            import shutil
            shutil.rmtree(sibling, ignore_errors=True)

    def test_symlink_escape(self):
        link = os.path.join(self.ws, "in", "link.mp4")
        try:
            os.symlink(self.secret, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        with self.assertRaises(EditError) as cm:
            self.policy.resolve_input("in/link.mp4", "x", VIDEO_EXTENSIONS)
        self.assertEqual(cm.exception.details["reason"], "symlink_escape")
        # a symlinked directory that leaves the workspace is refused for outputs too
        os.symlink(self.outside, os.path.join(self.ws, "outlink"))
        with self.assertRaises(EditError) as cm:
            self.policy.resolve_output("outlink/x.mp4", "o", [], False)
        self.assertEqual(cm.exception.details["reason"], "workspace_escape")
        # an internal symlink is fine
        os.symlink(self.media, os.path.join(self.ws, "in", "inner.mp4"))
        self.assertEqual(self.policy.resolve_input("in/inner.mp4", "x", VIDEO_EXTENSIONS), os.path.realpath(self.media))

    def test_input_kinds(self):
        for bad, code in ((os.path.join(self.ws, "in"), "INVALID_INPUT"), ("in/none.mp4", "MISSING_INPUT"), ("in/a.mp4", "UNSUPPORTED_FORMAT")):
            with self.assertRaises(EditError) as cm:
                self.policy.resolve_input(bad, "x", IMAGE_EXTENSIONS if code == "UNSUPPORTED_FORMAT" else VIDEO_EXTENSIONS)
            self.assertEqual(cm.exception.code, code)
        write_fake_media(os.path.join(self.ws, "in", "empty.mp4"), 0)
        with self.assertRaises(EditError) as cm:
            self.policy.resolve_input("in/empty.mp4", "x", VIDEO_EXTENSIONS)
        self.assertEqual(cm.exception.code, "INVALID_INPUT")
        for bad in ("", "a\0b", 5, None, "x" * 5000):
            with self.assertRaises(EditError):
                self.policy.resolve_input(bad, "x", VIDEO_EXTENSIONS)

    def test_outputs(self):
        out = self.policy.resolve_output("out/final.mp4", "o", [self.media], False)
        self.assertTrue(is_within(self.policy.workspace, out))
        os.makedirs(os.path.join(self.ws, "dir.mp4"))
        for bad, reason in (("../x.mp4", "traversal"), ("in/a.mp4", "overwrite_input"), ("dir.mp4", "not_regular_file"), ("nul.mp4", "reserved_name")):
            with self.assertRaises(EditError) as cm:
                self.policy.resolve_output(bad, "o", [os.path.realpath(self.media)], False)
            self.assertEqual(cm.exception.details.get("reason"), reason, bad)
        with self.assertRaises(EditError) as cm:
            self.policy.resolve_output(os.path.join(self.outside, "x.mp4"), "o", [], False)
        self.assertEqual(cm.exception.details["reason"], "workspace_escape")
        with self.assertRaises(EditError) as cm:
            self.policy.resolve_output("x.txt", "o", [], False)
        self.assertEqual(cm.exception.code, "UNSUPPORTED_FORMAT")
        with self.assertRaises(EditError) as cm:
            self.policy.resolve_output("in/a.mp4", "o", [], False)  # exists
        self.assertIn(cm.exception.details["reason"], ("exists",))
        self.policy.resolve_output("in/a.mp4", "o", [], True)


if __name__ == "__main__":
    unittest.main()
