"""Synthetic fixtures built with ffmpeg lavfi at test time (nothing binary is committed).

  a.mp4     6 s, 640x360 @30 H.264 + 440 Hz mono AAC
  b.mp4     5 s, 1280x720 @25 H.264 + 880 Hz mono AAC
  logo.png  120x40 red RGBA
"""
import os
import shutil
import subprocess
from typing import Dict


def available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def build_all(directory: str) -> Dict[str, str]:
    os.makedirs(directory, exist_ok=True)
    a = os.path.join(directory, "a.mp4")
    b = os.path.join(directory, "b.mp4")
    logo = os.path.join(directory, "logo.png")
    _ff("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "6",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", a)
    _ff("-f", "lavfi", "-i", "smptebars=size=1280x720:rate=25", "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000", "-t", "5",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", b)
    _ff("-f", "lavfi", "-i", "color=c=red@0.8:s=120x40,format=rgba", "-frames:v", "1", logo)
    return {"a": a, "b": b, "logo": logo}
