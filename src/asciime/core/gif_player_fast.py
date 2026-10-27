"""Terminal GIF player with half-block, sextant, Kitty, and Sixel renderers."""

import asyncio
import base64
import concurrent.futures
import io
import logging
import multiprocessing
import os
import re
import secrets
import shutil
import sys
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from PIL import Image, ImageOps, ImageSequence

from asciime.core import sixel
from asciime.core.image_accelerator import ImageResizer

logger = logging.getLogger(__name__)

Color = Tuple[int, int, int]

UPPER_HALF = "\u2580"
LOWER_HALF = "\u2584"
FULL_BLOCK = "\u2588"
LEFT_HALF = "\u258c"
RIGHT_HALF = "\u2590"
# Sextant sub-pixels, as (dx, dy) in bit order: top-left is bit 0, bottom-right bit 5.
SEXTANT_CELLS = ((0, 0), (1, 0), (0, 1), (1, 1), (0, 2), (1, 2))
# Pixel grid per cell and vertical squash (cells are ~1:2) for each text renderer.
TEXT_GEOMETRY = {"ascii": (1, 1, 0.5), "blocks": (1, 2, 1.0), "sextant": (2, 3, 0.75)}
RENDERERS = ("blocks", "sextant", "kitty", "sixel", "auto")
# Windows Terminal maps Sixel images onto a virtual 10x20 pixel cell.
DEFAULT_CELL_PIXELS = (10, 20)
ASCII_RAMP = " .'`^\",:;Il!i><~+_-?][}{1)(|/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$"
ALPHA_THRESHOLD = 128
MIN_FRAME_DURATION = 0.02
DEFAULT_FRAME_DURATION = 0.1
STILL_IMAGE_DURATION = 3.0

ESC = "\x1b["
SYNC_START = ESC + "?2026h"
SYNC_END = ESC + "?2026l"


class GIFPlayerError(Exception):
    """Base exception for GIF player errors"""
    pass


def _xterm_256(color: Color) -> int:
    """Map an RGB color to the nearest xterm-256 palette index."""
    levels = (0, 95, 135, 175, 215, 255)

    def nearest_level(value: int) -> int:
        return min(range(6), key=lambda i: abs(levels[i] - value))

    r, g, b = color
    ri, gi, bi = nearest_level(r), nearest_level(g), nearest_level(b)
    cube = (levels[ri], levels[gi], levels[bi])
    cube_index = 16 + 36 * ri + 6 * gi + bi

    gray = (r + g + b) // 3
    gray_step = max(0, min(23, round((gray - 8) / 10)))
    gray_value = 8 + gray_step * 10
    gray_index = 232 + gray_step

    def distance(other: Color) -> int:
        return sum((a - c) ** 2 for a, c in zip(color, other))

    if distance((gray_value,) * 3) < distance(cube):
        return gray_index
    return cube_index


def _to_rgba(frame: Image.Image) -> Image.Image:
    """Convert any decoded frame to upright 8-bit RGBA."""
    frame = ImageOps.exif_transpose(frame)
    if frame.mode.startswith("I;16") or frame.mode == "I":
        # Pillow clips 16/32-bit values instead of scaling them to 8 bits.
        frame = frame.convert("I").point(lambda value: value * (1 / 256)).convert("L")
    elif frame.mode == "F":
        frame = frame.point(lambda value: value * 255).convert("L")
    return frame.convert("RGBA")


def sextant_char(pattern: int) -> str:
    """Return the character for a 6-bit sextant pattern (bit 0 = top-left)."""
    if pattern == 0:
        return " "
    if pattern == 63:
        return FULL_BLOCK
    if pattern == 21:
        return LEFT_HALF
    if pattern == 42:
        return RIGHT_HALF
    # U+1FB00..1FB3B skip the empty, full, and left/right half patterns.
    return chr(0x1FB00 + pattern - 1 - (pattern > 21) - (pattern > 42))


def _average(colors: Sequence[Color]) -> Color:
    count = len(colors)
    return (
        sum(c[0] for c in colors) // count,
        sum(c[1] for c in colors) // count,
        sum(c[2] for c in colors) // count,
    )


def _split_cell(pixels: List[Tuple[int, Color]]) -> Tuple[int, Optional[Color], Optional[Color]]:
    """Split a cell's sub-pixels into (pattern, fg, bg); None means terminal default."""
    if not pixels:
        return 0, None, None
    if len(pixels) < 6:
        # Partly transparent: draw opaque sub-pixels over the default background.
        pattern = 0
        for bit, _ in pixels:
            pattern |= 1 << bit
        return pattern, _average([color for _, color in pixels]), None
    channel = max(range(3), key=lambda c: max(p[1][c] for p in pixels) - min(p[1][c] for p in pixels))
    values = [color[channel] for _, color in pixels]
    low, high = min(values), max(values)
    if high - low < 8:
        return 0, None, _average([color for _, color in pixels])
    middle = (low + high) / 2
    pattern = 0
    fg, bg = [], []
    for bit, color in pixels:
        if color[channel] > middle:
            pattern |= 1 << bit
            fg.append(color)
        else:
            bg.append(color)
    return pattern, _average(fg), _average(bg)


def _cell_pixels() -> Tuple[int, int]:
    """Return the terminal cell size in pixels, defaulting to Windows Terminal's."""
    try:
        import fcntl
        import struct
        import termios

        packed = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
        rows, cols, x_pixels, y_pixels = struct.unpack("HHHH", packed)
        if rows and cols and x_pixels and y_pixels:
            return max(1, x_pixels // cols), max(1, y_pixels // rows)
    except (ImportError, OSError, ValueError, AttributeError):
        pass
    return DEFAULT_CELL_PIXELS


def _da1_reports_sixel(response: bytes) -> bool:
    match = re.search(rb"\x1b\[\?([\d;]*)c", response)
    return bool(match) and b"4" in match.group(1).split(b";")


def _terminal_supports_sixel(timeout: float = 0.5) -> bool:
    """Ask the terminal for its Primary Device Attributes; '4' means Sixel."""
    try:
        import select
        import termios
    except ImportError:
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    fd = sys.stdin.fileno()
    try:
        original = termios.tcgetattr(fd)
    except termios.error:
        return False
    raw = termios.tcgetattr(fd)
    raw[3] &= ~(termios.ICANON | termios.ECHO)
    response = b""
    try:
        termios.tcsetattr(fd, termios.TCSANOW, raw)
        sys.stdout.write(ESC + "c")
        sys.stdout.flush()
        deadline = time.monotonic() + timeout
        while not _da1_reports_sixel(response) and not response.endswith(b"c"):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                break
            chunk = os.read(fd, 256)
            if not chunk:
                break
            response += chunk
    except (OSError, termios.error):
        return False
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, original)
    return _da1_reports_sixel(response)


class _ColorEncoder:
    """Builds SGR escape sequences for a display mode, caching conversions."""

    def __init__(self, mode: str):
        self.mode = mode

    def key(self, color: Color):
        if self.mode == "256color":
            return _xterm_256(color)
        return color

    def fg(self, key) -> str:
        if key is None:
            return ESC + "39m"
        if self.mode == "256color":
            return f"{ESC}38;5;{key}m"
        return f"{ESC}38;2;{key[0]};{key[1]};{key[2]}m"

    def bg(self, key) -> str:
        if key is None:
            return ESC + "49m"
        if self.mode == "256color":
            return f"{ESC}48;5;{key}m"
        return f"{ESC}48;2;{key[0]};{key[1]};{key[2]}m"


class _FrameList(list):
    """Rendered frames (strings or pending futures) plus their source images."""

    def __init__(self, rendered, frames):
        super().__init__(rendered)
        self.source = [image for image, _ in frames]


class GIFPlayer:
    def __init__(
        self,
        display_mode: str = "truecolor",
        terminal_title: Optional[str] = None,
        width_percentage: float = 0.8,
        max_height_percentage: float = 0.8,
        loop_count: int = 3,
        renderer: str = "blocks",
        acceleration: str = "auto",
    ):
        """Initialize GIF player

        Args:
            display_mode (str): Display mode ('nocolor', '256', '256fgbg', or 'truecolor')
            terminal_title (str, optional): Custom terminal title during playback
            width_percentage (float): Percentage of terminal width to use (0.1 to 1.0)
            max_height_percentage (float): Maximum percentage of terminal height to use (0.1 to 1.0)
            loop_count (int): Number of times to loop (0 for infinite)
            renderer (str): 'blocks', 'sextant', 'kitty', 'sixel', or 'auto'
            acceleration (str): 'auto', 'cpu', or 'gpu' for image resizing
        """
        mode_mapping = {
            'nocolor': 'ascii',
            '256': '256color',
            '256fgbg': '256color',
            'truecolor': 'truecolor'
        }
        self.display_mode = mode_mapping.get(display_mode, 'truecolor')
        self.terminal_title = terminal_title
        self.width_percentage = max(0.1, min(1.0, width_percentage))
        self.max_height_percentage = max(0.1, min(1.0, max_height_percentage))
        self.loop_count = loop_count
        if renderer not in RENDERERS:
            raise ValueError(f"Invalid renderer: {renderer}")
        if acceleration not in ("auto", "cpu", "gpu"):
            raise ValueError(f"Invalid acceleration: {acceleration}")
        self.renderer = renderer
        self.acceleration = acceleration
        self._mode = "blocks"
        self._image_ids: List[int] = []
        self._running = False
        self._pool: Optional[concurrent.futures.ProcessPoolExecutor] = None

    def _available_cells(self, terminal: os.terminal_size) -> Tuple[int, int]:
        cols = max(1, int(terminal.columns * self.width_percentage))
        rows = max(1, int(terminal.lines * self.max_height_percentage))
        return cols, rows

    def _fit(self, image_size: Tuple[int, int], cells: Tuple[int, int]) -> Tuple[int, int]:
        """Return the pixel size to resize to, preserving aspect ratio.

        Terminal cells are roughly twice as tall as wide. Half-block modes draw
        two square pixels per cell, sextants a 2x3 grid of slightly tall
        pixels, and ASCII one tall pixel per cell.
        """
        width, height = image_size
        cols, rows = cells
        per_col, per_row, height_scale = TEXT_GEOMETRY[self._text_kind()]
        max_width, max_height = cols * per_col, rows * per_row
        scale = min(max_width / width, max_height / (height * height_scale))
        return (
            max(1, min(max_width, round(width * scale))),
            max(1, min(max_height, round(height * height_scale * scale))),
        )

    def _text_kind(self) -> str:
        if self.display_mode == "ascii":
            return "ascii"
        return "sextant" if self._mode == "sextant" else "blocks"

    @staticmethod
    def _load_frames(gif_path: str) -> List[Tuple[Image.Image, float]]:
        """Decode a GIF, animated WebP/APNG, or static image into RGBA frames."""
        frames = []
        with Image.open(gif_path) as image:
            for frame in ImageSequence.Iterator(image):
                # WebP only populates per-frame timing once the frame is loaded.
                frame.load()
                duration_ms = frame.info.get("duration") or 0
                # Browsers treat tiny or missing delays as 100ms; match that.
                duration = duration_ms / 1000 if duration_ms > 10 else DEFAULT_FRAME_DURATION
                frames.append((_to_rgba(frame), max(MIN_FRAME_DURATION, duration)))
        if not frames:
            raise GIFPlayerError(f"No frames found in {gif_path}")
        if len(frames) == 1:
            frames[0] = (frames[0][0], STILL_IMAGE_DURATION)
        return frames

    def _render_halfblock(self, image: Image.Image) -> List[str]:
        encoder = _ColorEncoder(self.display_mode)
        width, height = image.size
        data = image.tobytes()
        key_cache: Dict[Color, object] = {}

        def pixel(x: int, y: int):
            if y >= height:
                return None
            i = (y * width + x) * 4
            if data[i + 3] < ALPHA_THRESHOLD:
                return None
            color = (data[i], data[i + 1], data[i + 2])
            key = key_cache.get(color)
            if key is None:
                key = key_cache[color] = encoder.key(color)
            return key

        lines = []
        for y in range(0, height, 2):
            parts = []
            current_fg: object = ()
            current_bg: object = ()
            for x in range(width):
                top, bottom = pixel(x, y), pixel(x, y + 1)
                if top == bottom:
                    char, fg, bg = " ", current_fg, top
                elif top is None:
                    char, fg, bg = LOWER_HALF, bottom, None
                else:
                    char, fg, bg = UPPER_HALF, top, bottom
                if fg != current_fg:
                    parts.append(encoder.fg(fg))
                    current_fg = fg
                if bg != current_bg:
                    parts.append(encoder.bg(bg))
                    current_bg = bg
                parts.append(char)
            parts.append(ESC + "0m")
            lines.append("".join(parts))
        return lines

    def _render_sextant(self, image: Image.Image) -> List[str]:
        encoder = _ColorEncoder(self.display_mode)
        width, height = image.size
        data = image.tobytes()
        key_cache: Dict[Optional[Color], object] = {None: None}

        def key(color: Optional[Color]):
            cached = key_cache.get(color, key_cache)
            if cached is key_cache:
                cached = key_cache[color] = encoder.key(color)
            return cached

        lines = []
        for top in range(0, height, 3):
            parts = []
            current_fg: object = ()
            current_bg: object = ()
            for left in range(0, width, 2):
                pixels = []
                for bit, (dx, dy) in enumerate(SEXTANT_CELLS):
                    x, y = left + dx, top + dy
                    if x < width and y < height:
                        i = (y * width + x) * 4
                        if data[i + 3] >= ALPHA_THRESHOLD:
                            pixels.append((bit, (data[i], data[i + 1], data[i + 2])))
                pattern, fg_color, bg_color = _split_cell(pixels)
                bg = key(bg_color)
                fg = current_fg if pattern == 0 else key(fg_color)
                if fg != current_fg:
                    parts.append(encoder.fg(fg))
                    current_fg = fg
                if bg != current_bg:
                    parts.append(encoder.bg(bg))
                    current_bg = bg
                parts.append(sextant_char(pattern))
            parts.append(ESC + "0m")
            lines.append("".join(parts))
        return lines

    @staticmethod
    def _render_ascii(image: Image.Image) -> List[str]:
        width, height = image.size
        gray = image.convert("L").tobytes()
        alpha = image.getchannel("A").tobytes()
        last = len(ASCII_RAMP) - 1
        lines = []
        for y in range(height):
            row = []
            for x in range(width):
                i = y * width + x
                if alpha[i] < ALPHA_THRESHOLD:
                    row.append(" ")
                else:
                    row.append(ASCII_RAMP[gray[i] * last // 255])
            lines.append("".join(row))
        return lines

    def _render_frame(
        self, image: Image.Image, terminal: os.terminal_size,
        resizer: Optional[ImageResizer] = None,
    ) -> str:
        size = self._fit(image.size, self._available_cells(terminal))
        resized = (
            resizer.resize(image, size) if resizer is not None
            else image.resize(size, Image.Resampling.LANCZOS)
        )
        kind = self._text_kind()
        if kind == "ascii":
            lines = self._render_ascii(resized)
        elif kind == "sextant":
            lines = self._render_sextant(resized)
        else:
            lines = self._render_halfblock(resized)

        cols = -(-size[0] // TEXT_GEOMETRY[kind][0])
        top = max(1, (terminal.lines - len(lines)) // 2 + 1)
        left = max(1, (terminal.columns - cols) // 2 + 1)
        output = [SYNC_START]
        for offset, line in enumerate(lines):
            output.append(f"{ESC}{top + offset};{left}H{line}")
        output.append(SYNC_END)
        return "".join(output)

    def _select_renderer(self) -> str:
        """Resolve the configured renderer to 'blocks', 'sextant', 'kitty', or 'sixel'."""
        if self.renderer in ("blocks", "sextant"):
            return self.renderer
        multiplexed = bool(os.environ.get("TMUX") or os.environ.get("STY"))
        interactive = sys.stdout.isatty()
        if self.renderer == "kitty":
            if multiplexed:
                raise GIFPlayerError(
                    "Native images are not supported through tmux/screen; use --renderer blocks"
                )
            if not interactive:
                raise GIFPlayerError("Native image playback requires an interactive terminal")
            return "kitty"
        if self.renderer == "sixel":
            if not interactive:
                raise GIFPlayerError("Sixel playback requires an interactive terminal")
            return "sixel"
        if multiplexed or not interactive:
            logger.debug("Using block rendering outside a direct interactive terminal")
            return "blocks"
        if (
            os.environ.get("KITTY_WINDOW_ID")
            or os.environ.get("TERM") in ("xterm-kitty", "xterm-ghostty")
            or os.environ.get("TERM_PROGRAM") == "ghostty"
        ):
            return "kitty"
        if _terminal_supports_sixel():
            return "sixel"
        return "blocks"

    @staticmethod
    def _kitty_command(controls: str, payload: str = "") -> str:
        return f"\x1b_G{controls};{payload}\x1b\\"

    def _native_placement(
        self, image_id: int, image: Image.Image, terminal: os.terminal_size
    ) -> str:
        cols, rows = self._available_cells(terminal)
        scale = min(cols / image.width, rows * 2 / image.height)
        width = min(cols, max(1, round(image.width * scale)))
        height = min(rows, max(1, round(image.height * scale / 2)))
        top = max(1, (terminal.lines - height) // 2 + 1)
        left = max(1, (terminal.columns - width) // 2 + 1)
        return f"{ESC}{top};{left}H" + self._kitty_command(
            f"a=p,i={image_id},p=1,c={width},r={height},C=1,q=2"
        )

    def _sixel_layout(
        self, image_size: Tuple[int, int], terminal: os.terminal_size
    ) -> Tuple[Tuple[int, int], Tuple[int, int], Tuple[int, int]]:
        """Return (pixel size, cell footprint, top-left cell) for a Sixel frame."""
        cell_width, cell_height = _cell_pixels()
        cols, rows = self._available_cells(terminal)
        # Keep the last line free so the cursor never scrolls the image.
        rows = max(1, min(rows, terminal.lines - 1))
        max_width = cols * cell_width
        # Sixel draws 6-pixel bands; keep whole bands inside the reserved rows.
        max_height = max(6, rows * cell_height // 6 * 6)
        width, height = image_size
        scale = min(max_width / width, max_height / height)
        size = (
            max(1, min(max_width, round(width * scale))),
            max(1, min(max_height, round(height * scale))),
        )
        footprint = (-(-size[0] // cell_width), -(-size[1] // cell_height))
        origin = (
            max(1, (terminal.lines - footprint[1]) // 2 + 1),
            max(1, (terminal.columns - footprint[0]) // 2 + 1),
        )
        return size, footprint, origin

    def _sixel_pool(self) -> Optional[concurrent.futures.ProcessPoolExecutor]:
        if self._pool is None and (os.cpu_count() or 1) > 1:
            try:
                self._pool = concurrent.futures.ProcessPoolExecutor(
                    min(8, os.cpu_count() or 1),
                    # Forking a threaded asyncio process can deadlock; spawn is safe.
                    mp_context=multiprocessing.get_context("spawn"),
                    initializer=sixel.ignore_interrupts,
                )
            except (OSError, ValueError) as e:
                logger.debug("Sixel worker pool unavailable (%s); encoding serially", e)
        return self._pool

    def _shutdown_pool(self) -> None:
        if self._pool is not None:
            # Queued frames are useless once playback ends; don't wait for them.
            workers = list((getattr(self._pool, "_processes", None) or {}).values())
            self._pool.shutdown(wait=False, cancel_futures=True)
            for worker in workers:
                worker.terminate()
            self._pool = None

    def _prepare_sixel(
        self, frames: List[Tuple[Image.Image, float]], terminal: os.terminal_size
    ) -> List[Union[str, "concurrent.futures.Future[str]"]]:
        """Encode Sixel frames, in worker processes for animations.

        Animated frames are returned as futures so playback can start as soon
        as the first frame is ready instead of waiting for the whole GIF.
        """
        still = len(frames) == 1
        grayscale = self.display_mode == "ascii"
        jobs = []
        for image, _ in frames:
            size, footprint, origin = self._sixel_layout(image.size, terminal)
            jobs.append((image, size, origin, footprint, still, grayscale))
        pool = None if still else self._sixel_pool()
        if pool is None:
            return [sixel.render(*job) for job in jobs]
        return [pool.submit(sixel.render, *job) for job in jobs]

    async def _resolve_frame(self, rendered: List, index: int) -> Tuple[str, bool]:
        """Return a frame's output and whether playback had to wait for it."""
        frame = rendered[index]
        if isinstance(frame, str):
            return frame, False
        waited = not frame.done()
        try:
            text = await asyncio.wrap_future(frame)
        except concurrent.futures.process.BrokenProcessPool as e:
            logger.debug("Sixel worker pool failed (%s); encoding serially", e)
            self._pool = None
            image, terminal = rendered.source[index], shutil.get_terminal_size()
            size, footprint, origin = self._sixel_layout(image.size, terminal)
            text = await self._run_in_executor(
                sixel.render, image, size, origin, footprint, False, self.display_mode == "ascii"
            )
            waited = True
        rendered[index] = text
        return text, waited

    def _prepare_frames(
        self, frames: List[Tuple[Image.Image, float]], terminal: os.terminal_size
    ) -> Tuple[List[str], List[str]]:
        if self._mode == "sixel":
            return _FrameList(self._prepare_sixel(frames, terminal), frames), []
        if self._mode == "kitty":
            uploads = []
            rendered = []
            for image_id, (image, _) in zip(self._image_ids, frames):
                png = io.BytesIO()
                image.save(png, format="PNG", compress_level=1)
                encoded = base64.b64encode(png.getvalue()).decode("ascii")
                commands = []
                for start in range(0, len(encoded), 4096):
                    chunk = encoded[start:start + 4096]
                    more = int(start + 4096 < len(encoded))
                    controls = (
                        f"a=t,f=100,t=d,i={image_id},q=2,m={more}"
                        if start == 0 else f"m={more}"
                    )
                    commands.append(self._kitty_command(controls, chunk))
                uploads.append("".join(commands))
                rendered.append(self._native_placement(image_id, image, terminal))
            return rendered, uploads

        resizer = ImageResizer(self.acceleration)
        try:
            return [self._render_frame(image, terminal, resizer) for image, _ in frames], []
        finally:
            resizer.close()

    def _write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def _setup_terminal(self) -> None:
        sequences = [ESC + "?1049h", ESC + "?25l", ESC + "?7l", ESC + "2J"]
        if self.terminal_title:
            sequences.insert(0, ESC + "22;0t")
            sequences.append(f"\x1b]0;{self.terminal_title}\x07")
        self._write("".join(sequences))

    def _cleanup_terminal(self) -> None:
        sequences = [SYNC_END]
        if self._mode == "kitty":
            sequences.extend(
                self._kitty_command(f"a=d,d=I,i={image_id},q=2")
                for image_id in self._image_ids
            )
        sequences.extend([ESC + "0m", ESC + "?7h", ESC + "?25h", ESC + "?1049l"])
        if self.terminal_title:
            sequences.append(ESC + "23;0t")
        self._write("".join(sequences))

    async def _run_in_executor(self, func: Callable, *args):
        return await asyncio.get_running_loop().run_in_executor(None, func, *args)

    async def _play_frames(
        self, frames: List[Tuple[Image.Image, float]], rendered: List[str],
        terminal: os.terminal_size,
    ) -> None:
        loops_done = 0
        deadline = time.monotonic()
        previous: Optional[int] = None

        while self._running and (self.loop_count == 0 or loops_done < self.loop_count):
            for index, (_, duration) in enumerate(frames):
                current = shutil.get_terminal_size()
                if current != terminal:
                    terminal = current
                    if self._mode == "kitty":
                        rendered = [
                            self._native_placement(image_id, image, terminal)
                            for image_id, (image, _) in zip(self._image_ids, frames)
                        ]
                    else:
                        for stale in rendered:
                            if isinstance(stale, concurrent.futures.Future):
                                stale.cancel()
                        rendered, _ = await self._run_in_executor(
                            self._prepare_frames, frames, terminal
                        )
                    self._write(ESC + "2J")
                    deadline = time.monotonic()

                output, waited = await self._resolve_frame(rendered, index)
                if waited:
                    # Frames still being encoded play as soon as they are ready.
                    deadline = time.monotonic()
                deadline += duration
                # Keep the GIF's timeline when output is slow, rather than
                # showing overdue frames in a burst or slowing the animation.
                if time.monotonic() < deadline:
                    if self._mode == "kitty":
                        remove = (
                            self._kitty_command(
                                f"a=d,d=i,i={self._image_ids[previous]},q=2"
                            ) if previous is not None else ""
                        )
                        self._write(SYNC_START + remove + rendered[index] + SYNC_END)
                        previous = index
                    else:
                        self._write(output)
                delay = deadline - time.monotonic()
                await asyncio.sleep(max(0, delay))
            loops_done += 1

    async def play_animation(self, gif_path: str):
        """Play GIF animation in terminal

        Args:
            gif_path (str): Path to the GIF file
        """
        if not os.path.isfile(gif_path):
            raise GIFPlayerError(f"Image file not found: {gif_path}")

        self._mode = self._select_renderer()
        if self._mode == "kitty" and self.acceleration == "gpu":
            logger.debug("Native-image scaling is handled by the terminal, not the OpenGL resizer")
        if self._mode == "sixel" and self.acceleration == "gpu":
            logger.debug("Sixel frames are scaled by CPU worker processes")
        try:
            try:
                frames = await self._run_in_executor(self._load_frames, gif_path)
                self._image_ids = []
                if self._mode == "kitty":
                    first_id = secrets.randbelow(1 << 30) + 1
                    self._image_ids = list(range(first_id, first_id + len(frames)))
                terminal = shutil.get_terminal_size()
                logger.debug("Preparing %d frames for %s playback", len(frames), self._mode)
                rendered, uploads = await self._run_in_executor(
                    self._prepare_frames, frames, terminal
                )
            except GIFPlayerError:
                raise
            except Exception as e:
                raise GIFPlayerError(f"Failed to load image: {e}") from e

            self._running = True
            try:
                self._setup_terminal()
                for upload in uploads:
                    self._write(upload)
                    await asyncio.sleep(0)
                await self._play_frames(frames, rendered, terminal)
            finally:
                self._running = False
                self._cleanup_terminal()
        finally:
            self._shutdown_pool()
