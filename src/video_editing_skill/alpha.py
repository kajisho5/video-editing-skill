"""The alpha of one picture, read from a PNG that ffmpeg-skill wrote (standard library only; docs/decisions.md ADR-032).

A stinger must cover the whole picture at its cut point. That is measured on the stinger's own frame: ffmpeg-skill look.py
writes the frame as a PNG and keeps its alpha (RGBA, 8 or 16 bits), and this module reads the alpha of every pixel. Nothing
here runs ffmpeg or decodes video.

Read: non-interlaced PNG of colour type 6 (RGBA) or 4 (grey + alpha) at 8 or 16 bits, and colour type 3 (palette; its alpha
from tRNS, opaque without one). A picture with no alpha channel (colour types 0 and 2), an interlaced one, or a file that is
not a well-formed PNG gives None: an alpha that could not be read is never taken as opaque.
"""
import struct
import zlib
from typing import Any, Dict, List, Optional

SIGNATURE = b"\x89PNG\r\n\x1a\n"
OPAQUE_WITHIN = 2   # of 255: a pixel covers when its alpha is at least 253 (ffmpeg-skill's own alpha tolerance)
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_MAX_PIXELS = 64 * 1024 * 1024   # an 8K frame is 33 M pixels; anything larger is not a frame look.py writes


def _chunks(data: bytes) -> List[tuple]:
    if not data.startswith(SIGNATURE):
        raise ValueError("not a PNG")
    out, p = [], len(SIGNATURE)
    while p + 12 <= len(data):
        n, kind = struct.unpack(">I4s", data[p:p + 8])
        body = data[p + 8:p + 8 + n]
        if len(body) != n:
            raise ValueError("truncated chunk")
        out.append((kind, body))
        p += 12 + n
        if kind == b"IEND":
            break
    return out


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def unfilter(raw: bytes, height: int, stride: int, bpp: int) -> List[bytes]:
    """The scanlines of a non-interlaced PNG image, filters undone (PNG specification, section 9). `stride` is the bytes of one
    scanline without its filter byte, `bpp` the bytes of one whole pixel (at least 1)."""
    if len(raw) < height * (stride + 1):
        raise ValueError("image data is shorter than the image")
    rows: List[bytes] = []
    prev = bytes(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind, line = raw[start], raw[start + 1:start + 1 + stride]
        if kind == 0:
            cur = bytes(line)
        elif kind == 2:
            cur = bytes((x + b) & 0xFF for x, b in zip(line, prev))
        elif kind in (1, 3, 4):
            out = bytearray(line)
            for i in range(stride):
                a = out[i - bpp] if i >= bpp else 0
                if kind == 1:
                    out[i] = (out[i] + a) & 0xFF
                elif kind == 3:
                    out[i] = (out[i] + ((a + prev[i]) >> 1)) & 0xFF
                else:
                    out[i] = (out[i] + _paeth(a, prev[i], prev[i - bpp] if i >= bpp else 0)) & 0xFF
            cur = bytes(out)
        else:
            raise ValueError(f"unknown filter type {kind}")
        rows.append(cur)
        prev = cur
    return rows


def _palette_index_rows(rows: List[bytes], width: int, depth: int) -> List[List[int]]:
    if depth == 8:
        return [list(r[:width]) for r in rows]
    per = 8 // depth
    mask = (1 << depth) - 1
    out = []
    for r in rows:
        idx = []
        for byte in r:
            for k in range(per):
                idx.append((byte >> (8 - depth * (k + 1))) & mask)
        out.append(idx[:width])
    return out


def alpha_coverage(path: str, opaque_within: int = OPAQUE_WITHIN) -> Optional[Dict[str, Any]]:
    """Every pixel's alpha in the PNG at `path`: {width, height, bit_depth, colour_type, pixels, min_alpha (0..255), below,
    threshold}, where `below` counts the pixels whose alpha is under `threshold` = 255 - opaque_within (a 16-bit alpha is
    compared on the same scale: 65535 / 255 = 257 per step). None when the picture has no alpha to read."""
    try:
        with open(path, "rb") as fh:
            chunks = _chunks(fh.read())
    except (OSError, ValueError, struct.error):
        return None
    head = next((b for k, b in chunks if k == b"IHDR"), None)
    if head is None or len(head) != 13:
        return None
    width, height, depth, colour, _comp, _filt, interlace = struct.unpack(">IIBBBBB", head)
    if colour not in (3, 4, 6) or interlace != 0 or width == 0 or height == 0 or width * height > _MAX_PIXELS:
        return None
    if (colour in (4, 6) and depth not in (8, 16)) or (colour == 3 and depth not in (1, 2, 4, 8)):
        return None
    try:
        raw = zlib.decompress(b"".join(b for k, b in chunks if k == b"IDAT"))
    except zlib.error:
        return None
    bits = _CHANNELS[colour] * depth
    stride = (width * bits + 7) // 8
    try:
        rows = unfilter(raw, height, stride, max(1, bits // 8))
    except ValueError:
        return None
    threshold = 255 - opaque_within
    below = 0
    low = 255.0
    if colour == 3:
        trns = next((b for k, b in chunks if k == b"tRNS"), b"")
        table = [trns[i] if i < len(trns) else 255 for i in range(256)]
        for idx in _palette_index_rows(rows, width, depth):
            alphas = [table[i] for i in idx]
            low = min(low, float(min(alphas)))
            below += sum(1 for a in alphas if a < threshold)
    elif depth == 8:
        step, offset = _CHANNELS[colour], _CHANNELS[colour] - 1
        flags = bytes(1 if v < threshold else 0 for v in range(256))
        for r in rows:
            a = r[offset::step]
            low = min(low, float(min(a)))
            below += a.translate(flags).count(1)
    else:
        step, offset = 2 * _CHANNELS[colour], 2 * (_CHANNELS[colour] - 1)
        limit = threshold * 257
        for r in rows:
            hi, lo = r[offset::step], r[offset + 1::step]
            if min(hi) == 255:   # every alpha is 65280 + its low byte: above the limit, the row's lowest from the low bytes
                low = min(low, (65280 + min(lo)) / 257)
                continue
            values = [h * 256 + v for h, v in zip(hi, lo)]
            low = min(low, min(values) / 257)
            below += sum(1 for v in values if v < limit)
    return {"width": width, "height": height, "bit_depth": depth, "colour_type": colour, "pixels": width * height,
            "min_alpha": round(low, 2), "below": below, "threshold": threshold}
