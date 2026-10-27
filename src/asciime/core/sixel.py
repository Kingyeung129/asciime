"""Pure-Python Sixel encoder tuned for pre-rendering animation frames."""

import re
from typing import Dict, List, Optional

from PIL import Image

try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised only without the dependency
    np = None

ALPHA_THRESHOLD = 128
MAX_COLORS = 255
TRANSPARENT_INDEX = 255

# Byte value v (a 6-bit column mask) -> sixel character chr(63 + v).
_TO_SIXEL = bytes((63 + value) if value < 64 else 63 for value in range(256))
# Empty and full columns dominate; matching them without a backreference is ~3x faster.
_RUN = re.compile(rb"\?{4,}|~{4,}|(.)\1{3,}")


def _run(match: "re.Match[bytes]") -> bytes:
    run = match.group()
    return b"!%d%c" % (len(run), run[0])


def _compress(line: bytes) -> bytes:
    return _RUN.sub(_run, line)


def _palette_entries(image: Image.Image, used: List[int]) -> bytes:
    palette = image.getpalette() or []
    entries = []
    for index in used:
        r, g, b = palette[index * 3:index * 3 + 3]
        # Sixel colors are percentages; round to the closest of 101 levels.
        entries.append(b"#%d;2;%d;%d;%d" % (
            index, (r * 100 + 127) // 255, (g * 100 + 127) // 255, (b * 100 + 127) // 255,
        ))
    return b"".join(entries)


def quantize(image: Image.Image, still: bool) -> Image.Image:
    """Reduce an RGBA image to <=255 colors, reserving index 255 for transparency.

    Stills get a slower median-cut palette with dithering; animation frames use
    the fast octree quantizer without dithering, which would shimmer in motion.
    """
    paletted = image.convert("RGB").quantize(
        colors=MAX_COLORS,
        method=Image.Quantize.MEDIANCUT if still else Image.Quantize.FASTOCTREE,
        dither=Image.Dither.FLOYDSTEINBERG if still else Image.Dither.NONE,
    )
    alpha = image.getchannel("A")
    if alpha.getextrema()[0] < ALPHA_THRESHOLD:
        mask = alpha.point(lambda value: 255 if value < ALPHA_THRESHOLD else 0, mode="1")
        paletted.paste(TRANSPARENT_INDEX, mask=mask)
    return paletted


def _wrap(paletted: Image.Image, used: List[int], bands: List[bytes]) -> str:
    width, height = paletted.size
    header = b'\x1bP0;1;0q"1;1;%d;%d' % (width, height)
    return (header + _palette_entries(paletted, used) + b"-".join(bands) + b"\x1b\\").decode("ascii")


def _encode_numpy(paletted: Image.Image) -> str:
    width, height = paletted.size
    pixels = np.frombuffer(paletted.tobytes(), np.uint8).reshape(height, width)
    if height % 6:
        padding = np.full((-height % 6, width), TRANSPARENT_INDEX, np.uint8)
        pixels = np.vstack([pixels, padding])
    columns = np.arange(width)
    used = np.zeros(256, bool)
    bands = []
    for band in pixels.reshape(-1, 6, width):
        counts = np.bincount(band.ravel(), minlength=256)
        counts[TRANSPARENT_INDEX] = 0
        present = np.flatnonzero(counts)
        used[present] = True
        # One scatter per pixel row builds every color's column masks at once;
        # transparent pixels land in a spare last row that is never emitted.
        slot = np.full(256, present.size, np.intp)
        slot[present] = np.arange(present.size)
        masks = np.zeros((present.size + 1, width), np.uint8)
        for bit in range(6):
            masks[slot[band[bit]], columns] |= 1 << bit
        data = (masks[:-1] + 63).tobytes()
        commands = []
        for i, color in enumerate(present.tolist()):
            line = data[i * width:(i + 1) * width].rstrip(b"?")
            if line:
                commands.append(b"#%d%s" % (color, _compress(line)))
        bands.append(b"$".join(commands))
    return _wrap(paletted, np.flatnonzero(used).tolist(), bands)


def encode(paletted: Image.Image) -> str:
    """Encode a paletted image as a Sixel DCS string with transparent background."""
    if np is not None:
        return _encode_numpy(paletted)
    return _encode_pure(paletted)


def _encode_pure(paletted: Image.Image) -> str:
    width, height = paletted.size
    data = paletted.tobytes()
    bands: List[bytes] = []
    used_colors: Dict[int, None] = {}

    # One translate table per (color, bit): pixel == color -> 1 << bit, else 0.
    tables: Dict[int, List[bytes]] = {}

    def table(color: int, bit: int) -> bytes:
        cached = tables.get(color)
        if cached is None:
            cached = tables[color] = [
                bytes((1 << b) if i == color else 0 for i in range(256)) for b in range(6)
            ]
        return cached[bit]

    for top in range(0, height, 6):
        rows = [data[y * width:(y + 1) * width] for y in range(top, min(top + 6, height))]
        present = set()
        for row in rows:
            present.update(row)
        present.discard(TRANSPARENT_INDEX)
        commands = []
        for color in sorted(present):
            used_colors[color] = None
            # Bits never overlap, so adding the rows as big integers is a bytewise OR.
            mask = 0
            for bit, row in enumerate(rows):
                mask += int.from_bytes(row.translate(table(color, bit)), "big")
            line = mask.to_bytes(width, "big").translate(_TO_SIXEL).rstrip(b"?")
            if line:
                commands.append(b"#%d%s" % (color, _compress(line)))
        bands.append(b"$".join(commands))

    return _wrap(paletted, sorted(used_colors), bands)


def frame_to_sixel(image: Image.Image, still: bool = False) -> "SixelFrame":
    paletted = quantize(image, still)
    return SixelFrame(encode(paletted), paletted.histogram()[TRANSPARENT_INDEX] > 0)


class SixelFrame:
    __slots__ = ("data", "transparent")

    def __init__(self, data: str, transparent: bool):
        self.data = data
        self.transparent = transparent


def decode(data: str, palette_size: int = 256) -> Optional[Image.Image]:
    """Minimal decoder for tests: returns an RGBA image of an encoded frame."""
    match = re.match(r'\x1bP0;1;0q"1;1;(\d+);(\d+)(.*)\x1b\\\Z', data, re.S)
    if not match:
        return None
    width, height, body = int(match.group(1)), int(match.group(2)), match.group(3)
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pixels = image.load()
    colors: Dict[int, tuple] = {}
    color = 0
    x = band = 0
    tokens = re.finditer(r"#(\d+)(?:;2;(\d+);(\d+);(\d+))?|!(\d+)(.)|([?-~])|(\$)|(-)", body)
    for token in tokens:
        if token.group(1) is not None:
            color = int(token.group(1))
            if token.group(2) is not None:
                colors[color] = tuple(round(int(token.group(i)) * 255 / 100) for i in (2, 3, 4)) + (255,)
            continue
        if token.group(8):
            x = 0
            continue
        if token.group(9):
            x = 0
            band += 1
            continue
        count, char = (int(token.group(5)), token.group(6)) if token.group(5) else (1, token.group(7))
        value = ord(char) - 63
        for _ in range(count):
            for bit in range(6):
                y = band * 6 + bit
                if value & (1 << bit) and y < height and x < width:
                    pixels[x, y] = colors[color]
            x += 1
    return image


def ignore_interrupts() -> None:
    """Worker initializer: the parent process handles Ctrl+C and cleanup."""
    import signal

    signal.signal(signal.SIGINT, signal.SIG_IGN)


def render(
    image: Image.Image,
    size: tuple,
    origin: tuple,
    footprint: tuple,
    still: bool = False,
    grayscale: bool = False,
) -> str:
    """Scale, encode, and position one frame; designed to run in a worker process."""
    if image.size != tuple(size):
        image = image.resize(tuple(size), Image.Resampling.LANCZOS)
    if grayscale:
        image = image.convert("LA").convert("RGBA")
    frame = frame_to_sixel(image, still)
    top, left = origin
    cols, rows = footprint
    output = ["\x1b[?2026h"]
    if frame.transparent:
        # Sixel only paints opaque pixels, so clear the previous frame first.
        output.extend(f"\x1b[{top + row};{left}H\x1b[{cols}X" for row in range(rows))
    output.append(f"\x1b[{top};{left}H{frame.data}\x1b[?2026l")
    return "".join(output)
