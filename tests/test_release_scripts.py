"""The release workflow's version decision and file bump (.github/scripts), run against throwaway git repositories.

The rule that matters most: the first release (no tag yet) releases pyproject.toml's version unchanged -- never a
computed bump -- and a later push only bumps when pyproject.toml still equals the latest tag. POSIX shell only; skipped
where bash or git is unavailable (the Windows unit runner).
"""
import importlib.util
import os
import re
import shutil
import subprocess
import unittest

from helpers import tmp_dir

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, ".github", "scripts")
USABLE = os.name != "nt" and shutil.which("bash") is not None and shutil.which("git") is not None


@unittest.skipUnless(USABLE, "needs bash and git (POSIX)")
class ComputeVersionTests(unittest.TestCase):
    def setUp(self):
        self.repo = tmp_dir("ves-release-", self)
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


# ---- Velisa ADR-023: the CHANGELOG heading form, the release gate, and the test before the push ----

def _version():
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        return next(line.split('"')[1] for line in fh if line.startswith("version = "))


def _run(args, cwd, **env):
    full = {k: v for k, v in os.environ.items() if k != "GITHUB_OUTPUT"}
    full.update(env)
    return subprocess.run(args, cwd=cwd, env=full, check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True).stdout


@unittest.skipUnless(USABLE, "needs bash and git (POSIX)")
class ReleaseCommitScriptTests(unittest.TestCase):
    def setUp(self):
        self.repo = tmp_dir("ves-release-gate-", self)
        self.git("init", "-q")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "init")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def compute(self, current, drafter, releasable):
        out = _run(["bash", os.path.join(SCRIPTS, "compute_version.sh")], self.repo, CURRENT_VERSION=current,
                   DRAFTER_RESOLVED_VERSION=drafter, RELEASABLE=releasable)
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def test_nothing_releasable_since_the_tag_releases_nothing(self):
        self.git("tag", "v0.4.0")
        self.assertEqual(self.compute("0.4.0", "0.4.1", "false"),
                         {"next_version": "0.4.0", "latest_tag": "v0.4.0", "mode": "auto", "should_release": "false"})
        self.assertEqual(self.compute("0.4.0", "0.4.1", "true")["next_version"], "0.4.1")
        # a version bumped by hand is released whatever the gate says
        self.assertEqual(self.compute("0.5.0", "0.4.1", "false")["should_release"], "true")

    def test_changelog_heading_is_the_form_this_changelog_uses(self):
        with open(os.path.join(self.repo, "CHANGELOG.md"), "w", encoding="utf-8") as fh:
            fh.write("# Changelog\n\n## old\n")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "fix: a thing")
        _run(["bash", os.path.join(SCRIPTS, "render_changelog.sh")], self.repo, NEXT_VERSION="9.8.7", LATEST_TAG="")
        with open(os.path.join(self.repo, "CHANGELOG.md"), encoding="utf-8") as fh:
            headings = [line for line in fh.read().splitlines() if line.startswith("## ")]
        self.assertRegex(headings[0], r"^## v9\.8\.7 - \d{4}-\d{2}-\d{2}$")
        self.assertEqual(headings[1], "## old")


class ChangelogFormatTests(unittest.TestCase):
    def test_changelog_headings_keep_this_files_form(self):
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as fh:
            headings = [line for line in fh.read().splitlines() if line.startswith("## ")]
        self.assertTrue(headings)
        self.assertRegex(next(h for h in headings if re.match(r"^## v?\d", h)), r"^## v\d+\.\d+\.\d+ - ")
        self.assertIn(f"## v{_version()} - ", "\n".join(headings))


def _load_gate():
    spec = importlib.util.spec_from_file_location("release_gate", os.path.join(SCRIPTS, "release_gate.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseGateTests(unittest.TestCase):
    """chore / ci / docs / test pull requests alone release nothing (imagemagick-skill's rule)."""

    def setUp(self):
        self.gate = _load_gate()

    def test_release_level_labels_and_unlabelled_pull_requests_release(self):
        for labels in (["feature"], ["fix"], ["bug"], ["minor"], ["patch"], ["enhancement"], ["breaking"], ["ci", "fix"], []):
            self.assertTrue(self.gate.pull_releases(labels), labels)

    def test_maintenance_labels_alone_release_nothing(self):
        for labels in (["ci"], ["docs"], ["documentation"], ["chore"], ["test"], ["maintenance"],
                       ["ci", "docs", "contract:none"]):
            self.assertFalse(self.gate.pull_releases(labels), labels)

    def test_dependencies_pull_requests_release_as_a_patch(self):
        # Velisa D37 addendum: a pull request labelled `dependencies` (Dependabot) is released, as a patch.
        self.assertIn("dependencies", self.gate.release_labels())
        for labels in (["dependencies"], ["dependencies", "github_actions"], ["dependencies", "chore"]):
            self.assertTrue(self.gate.pull_releases(labels), labels)
        self.assertTrue(self.gate.decide([("chore(deps): bump x (#3)", [["dependencies"]])])[0])

    def test_decide_over_the_commits_since_the_tag(self):
        self.assertFalse(self.gate.decide([("ci: x (#1)", [["ci"]]), ("docs: y (#2)", [["documentation"]])])[0])
        self.assertTrue(self.gate.decide([("ci: x (#1)", [["ci"]]), ("fix: y (#2)", [["fix"]])])[0])
        self.assertFalse(self.gate.decide([])[0])
        self.assertFalse(self.gate.decide([("chore: tidy", []), ("docs(readme): typo", []), ("test: more", [])])[0])
        self.assertTrue(self.gate.decide([("Fix the thing", [])])[0])

    def test_release_drafter_bumps_only_on_release_level_labels(self):
        with open(os.path.join(ROOT, ".github", "release-drafter.yml"), encoding="utf-8") as fh:
            text = fh.read()
        block = text[text.index("version-resolver:"):text.index("autolabeler:")]
        self.assertEqual(set(re.findall(r"'([^']+)'", block)), set(self.gate.RELEASE_LABELS))


class ReleaseWorkflowOrderTests(unittest.TestCase):
    def test_release_commit_is_tested_before_it_is_pushed(self):
        with open(os.path.join(ROOT, ".github", "workflows", "release.yml"), encoding="utf-8") as fh:
            text = fh.read()
        test_step = text.index("- name: Test the release commit before pushing it")
        pushes = [m.start() for m in re.finditer(r"git push", text)]
        self.assertTrue(pushes)
        self.assertLess(text.index("git commit -m"), test_step)
        for push in pushes:
            self.assertLess(test_step, push)
        self.assertIn("RELEASABLE: ${{ steps.gate.outputs.releasable }}", text)


if __name__ == "__main__":
    unittest.main()
