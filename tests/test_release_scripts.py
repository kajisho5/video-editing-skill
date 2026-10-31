"""The release workflow's version decision and file bump (.github/scripts), run against throwaway git repositories.

The rule that matters most: the first release (no tag yet) releases pyproject.toml's version unchanged -- never a
computed bump -- and a later push only bumps when pyproject.toml still equals the latest tag. POSIX shell only; skipped
where bash or git is unavailable (the Windows unit runner).
"""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, ".github", "scripts")
USABLE = os.name != "nt" and shutil.which("bash") is not None and shutil.which("git") is not None


@unittest.skipUnless(USABLE, "needs bash and git (POSIX)")
class ComputeVersionTests(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="ves-release-")
        self.git("init", "-q")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "init")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def compute(self, current, drafter=""):
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_OUTPUT"}
        env.update(CURRENT_VERSION=current, DRAFTER_RESOLVED_VERSION=drafter)
        out = subprocess.run(["bash", os.path.join(SCRIPTS, "compute_version.sh")], cwd=self.repo, env=env, check=True,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def test_first_release_is_the_pyproject_version(self):
        # no tag yet: whatever release-drafter resolved (it would compute a bump from labels), pyproject.toml wins
        self.assertEqual(self.compute("0.4.0", drafter="0.5.0"), {"next_version": "0.4.0", "latest_tag": "", "mode": "manual", "should_release": "true"})

    def test_auto_bump_only_from_the_tagged_version(self):
        self.git("tag", "v0.4.0")
        self.assertEqual(self.compute("0.4.0", drafter="0.4.1"), {"next_version": "0.4.1", "latest_tag": "v0.4.0", "mode": "auto", "should_release": "true"})
        # nothing resolved since the tag: no release
        self.assertEqual(self.compute("0.4.0")["should_release"], "false")
        # a version bumped by hand in the merged PR is released as written, not overwritten by the computed one
        self.assertEqual(self.compute("0.5.0", drafter="0.4.1"), {"next_version": "0.5.0", "latest_tag": "v0.4.0", "mode": "manual", "should_release": "true"})

    def test_bump_writes_both_version_files_once(self):
        os.makedirs(os.path.join(self.repo, "src", "video_editing_skill"))
        for rel in ("pyproject.toml", os.path.join("src", "video_editing_skill", "__init__.py")):
            shutil.copyfile(os.path.join(ROOT, rel), os.path.join(self.repo, rel))
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
            current = next(line.split('"')[1] for line in fh if line.startswith("version = "))
        env = dict(os.environ, CURRENT_VERSION=current, NEXT_VERSION="9.8.7")
        subprocess.run(["bash", os.path.join(SCRIPTS, "bump_version_files.sh")], cwd=self.repo, env=env, check=True, stdout=subprocess.DEVNULL)
        with open(os.path.join(self.repo, "pyproject.toml"), encoding="utf-8") as fh:
            self.assertIn('\nversion = "9.8.7"\n', fh.read())
        with open(os.path.join(self.repo, "src", "video_editing_skill", "__init__.py"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('\nVERSION = "9.8.7"\n', text)
        self.assertNotIn(f'VERSION = "{current}"', text)


if __name__ == "__main__":
    unittest.main()
