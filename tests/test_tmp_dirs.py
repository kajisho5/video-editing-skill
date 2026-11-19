"""ADR-037: the tests' temporary directories are always removed (helpers.tmp_dir / make_workspace)."""
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _child_env(root, **extra):
    env = dict(os.environ, VIDEO_EDITING_TEST_TMPDIR=root, PYTHONPATH=HERE + os.pathsep + os.environ.get("PYTHONPATH", ""))
    env.pop("VIDEO_EDITING_KEEP_TEST_TMP", None)
    env.update(extra)
    return env


class TmpDirTests(unittest.TestCase):
    def setUp(self):
        # the root every child process writes under; made and removed here, not through tmp_dir (which is under test)
        self.root = tempfile.mkdtemp(prefix="ves-tmpcheck-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def run_child(self, code, **env):
        return subprocess.run([sys.executable, "-c", code], cwd=HERE, env=_child_env(self.root, **env),
                              capture_output=True, text=True, timeout=120)

    def test_instance_owner_removes_after_a_failing_test(self):
        code = (
            "import unittest\nfrom helpers import make_workspace, tmp_dir\n"
            "class T(unittest.TestCase):\n"
            "    def test_fails(self):\n"
            "        open(make_workspace(self) + '/in/x.bin', 'wb').write(b'x')\n"
            "        tmp_dir('ves-other-', self)\n"
            "        self.fail('on purpose')\n"
            "    def test_raises(self):\n"
            "        tmp_dir('ves-raise-', self)\n"
            "        raise RuntimeError('on purpose')\n"
            "import os, sys\n"
            "r = unittest.TextTestRunner(stream=open(os.devnull, 'w')).run(unittest.defaultTestLoader.loadTestsFromTestCase(T))\n"
            "print(len(r.failures), len(r.errors), sorted(os.listdir(os.environ['VIDEO_EDITING_TEST_TMPDIR'])), flush=True)\n"
            "os._exit(0)\n"  # skip atexit: the cleanups alone must have removed them
        )
        r = self.run_child(code)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1 1 []")

    def test_class_owner_removes_even_when_setupclass_fails(self):
        code = (
            "import unittest\nfrom helpers import tmp_dir\n"
            "class T(unittest.TestCase):\n"
            "    @classmethod\n"
            "    def setUpClass(cls):\n"
            "        cls.root = tmp_dir('ves-cls-', cls)\n"
            "        raise RuntimeError('fixture build failed')\n"
            "    def test_never(self):\n"
            "        pass\n"
            "import os\n"
            "r = unittest.TextTestRunner(stream=open(os.devnull, 'w')).run(unittest.defaultTestLoader.loadTestsFromTestCase(T))\n"
            "print(len(r.errors), sorted(os.listdir(os.environ['VIDEO_EDITING_TEST_TMPDIR'])), flush=True)\n"
            "os._exit(0)\n"
        )
        r = self.run_child(code)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1 []")

    def test_no_owner_is_removed_at_exit(self):
        r = self.run_child("from helpers import make_workspace, tmp_dir\nmake_workspace(); tmp_dir('ves-x-')\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(self.root), [])

    @unittest.skipIf(os.name == "nt", "SIGTERM cannot be handled on Windows")
    def test_sigterm_removes_and_still_ends_by_sigterm(self):
        code = (
            "import time\nfrom helpers import tmp_dir\n"
            "print(tmp_dir('ves-term-'), flush=True)\n"
            "while True:\n"
            "    try:\n"
            "        time.sleep(1)\n"
            "    except BaseException:\n"  # code under test that swallows everything must not keep the directory
            "        pass\n"
        )
        p = subprocess.Popen([sys.executable, "-c", code], cwd=HERE, env=_child_env(self.root), stdout=subprocess.PIPE, text=True)
        try:
            path = p.stdout.readline().strip()
            self.assertTrue(os.path.isdir(path))
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(timeout=30), -signal.SIGTERM)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stdout.close()
        self.assertEqual(os.listdir(self.root), [])

    def test_keep_env_keeps_them_and_prints_where(self):
        r = self.run_child("from helpers import tmp_dir\nopen(tmp_dir('ves-kept-') + '/f', 'w').write('x')\n",
                           VIDEO_EDITING_KEEP_TEST_TMP="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        left = os.listdir(self.root)
        self.assertEqual(len(left), 1)
        self.assertTrue(left[0].startswith("ves-kept-"))
        self.assertIn(os.path.join(self.root, left[0]), r.stderr)

    def test_offline_modules_leave_nothing_behind(self):
        # whole test modules, in a child process with TMPDIR pointed at the empty root as well
        r = subprocess.run([sys.executable, "-m", "unittest", "-q", "test_paths", "test_security", "test_release_scripts"],
                           cwd=HERE, env=_child_env(self.root, TMPDIR=self.root), capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertRegex(r.stderr, r"Ran \d{2,} tests")
        self.assertEqual(os.listdir(self.root), [])

    def test_test_files_make_top_level_temp_dirs_only_through_helpers(self):
        bad = re.compile(r"tempfile\.(mkdtemp|TemporaryDirectory)\((?![^)]*dir=)")
        for name in sorted(os.listdir(HERE)):
            if not name.startswith("test_") or not name.endswith(".py") or name == "test_tmp_dirs.py":
                continue
            with open(os.path.join(HERE, name), encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    self.assertIsNone(bad.search(line), f"{name}:{n}: use helpers.tmp_dir(prefix, owner)")


if __name__ == "__main__":
    unittest.main()
