import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from video_editing_skill.ffmpeg_skill import locate  # noqa: E402

FFMPEG_SKILL = locate(os.environ.get("VIDEO_EDITING_FFMPEG_SKILL_DIR"))
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def make_workspace():
    ws = tempfile.mkdtemp(prefix="ves-")
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
    proc = subprocess.run([sys.executable, "-m", "video_editing_skill", *args], input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=dict(e, PYTHONPATH=SRC + os.pathsep + e.get("PYTHONPATH", "")))
    out = proc.stdout.decode("utf-8", errors="replace")
    try:
        doc = json.loads(out) if out.strip() else None
    except ValueError:
        doc = ("NOT_JSON", out)
    return proc.returncode, doc, proc.stderr.decode("utf-8", errors="replace")
