import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from typing import Any, Optional, Set

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from video_editing_skill.ffmpeg_skill import locate  # noqa: E402

FFMPEG_SKILL = locate(os.environ.get("VIDEO_EDITING_FFMPEG_SKILL_DIR"))
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


# Temporary directories of the tests (docs/decisions.md ADR-037). Every one is made by tmp_dir() and removed again:
#   - owner a TestCase class (setUpClass): addClassCleanup, which also runs when setUpClass itself fails;
#   - owner a TestCase instance: addCleanup, after that test whether it passed, failed or raised;
#   - whatever is still there at interpreter exit (atexit) or on SIGTERM (a CI cancel) is removed then.
# VIDEO_EDITING_TEST_TMPDIR=DIR makes them under DIR; VIDEO_EDITING_KEEP_TEST_TMP=1 keeps them (paths on stderr).
TMP_ROOT = os.environ.get("VIDEO_EDITING_TEST_TMPDIR") or None
KEEP_TMP = os.environ.get("VIDEO_EDITING_KEEP_TEST_TMP") == "1"
_LIVE: Set[str] = set()
_SIGTERM_SET = False


def _remove(path: str) -> None:
    if KEEP_TMP:
        return
    shutil.rmtree(path, ignore_errors=True)
    if not os.path.exists(path):
        _LIVE.discard(path)


@atexit.register
def remove_all_tmp_dirs() -> None:
    """Remove every directory tmp_dir() made that is still there (or, with VIDEO_EDITING_KEEP_TEST_TMP=1, list them)."""
    if KEEP_TMP:
        for path in sorted(_LIVE):
            print(f"kept test temp dir: {path}", file=sys.stderr)
        return
    for path in sorted(_LIVE):
        _remove(path)


def _on_sigterm(signum: int, _frame: Any) -> None:
    # remove first, then end the way SIGTERM would have: a SystemExit raised here could be caught by the code under test
    remove_all_tmp_dirs()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _catch_sigterm() -> None:
    global _SIGTERM_SET
    if _SIGTERM_SET or threading.current_thread() is not threading.main_thread():
        return
    _SIGTERM_SET = True
    try:
        if signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, None):
            signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError, AttributeError):  # no SIGTERM handler on this platform / thread
        pass


def tmp_dir(prefix: str = "ves-", owner: Optional[Any] = None) -> str:
    """A new temporary directory, removed when `owner` (a TestCase class or instance) is cleaned up, or at exit at the latest."""
    path = tempfile.mkdtemp(prefix=prefix, dir=TMP_ROOT)
    _LIVE.add(path)
    _catch_sigterm()
    if isinstance(owner, type):
        owner.addClassCleanup(_remove, path)
    elif owner is not None:
        owner.addCleanup(_remove, path)
    return path


def make_workspace(owner: Optional[Any] = None) -> str:
    ws = tmp_dir("ves-", owner)
    os.makedirs(os.path.join(ws, "in"))
    return ws


def write_fake_media(path: str, size: int = 1024) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)
    return path


def request(sources, operations, outputs, options=None, project_id="p"):
    doc = {"schema": "video-editing/request@1", "project": {"id": project_id, "sources": sources, "operations": operations, "outputs": outputs}}
    if options is not None:
        doc["options"] = options
    return doc


def cli(args, stdin=None, env=None):
    """Run the CLI as a real subprocess: returns (exit, stdout-json-or-None, stderr)."""
    e = dict(os.environ)
    if env:
        e.update(env)
    proc = subprocess.run([sys.executable, "-m", "video_editing_skill", *args], input=stdin, capture_output=True,
                          env=dict(e, PYTHONPATH=SRC + os.pathsep + e.get("PYTHONPATH", "")))
    out = proc.stdout.decode("utf-8", errors="replace")
    try:
        doc = json.loads(out) if out.strip() else None
    except ValueError:
        doc = ("NOT_JSON", out)
    return proc.returncode, doc, proc.stderr.decode("utf-8", errors="replace")
