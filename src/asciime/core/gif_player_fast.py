"""Terminal GIF player using half-block characters for high-detail rendering."""

import asyncio
import base64
import io
import logging
import os
import secrets
import shutil
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

from PIL import Image, ImageSequence

from asciime.core.image_accelerator import ImageResizer

logger = logging.getLogger(__name__)

Color = Tuple[int, int, int]

UPPER_HALF = "\u2580"
LOWER_HALF = "\u2584"
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
            renderer (str): 'blocks', 'kitty', or 'auto' for native image detection
            acceleration (str): 'auto', 'cpu', or 'gpu' for block image resizing
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
        if renderer not in ("blocks", "kitty", "auto"):
            raise ValueError(f"Invalid renderer: {renderer}")
        if acceleration not in ("auto", "cpu", "gpu"):
            raise ValueError(f"Invalid acceleration: {acceleration}")
        self.renderer = renderer
        self.acceleration = acceleration
        self._native = False
        self._image_ids: List[int] = []
        self._running = False

    def _available_cells(self, terminal: os.terminal_size) -> Tuple[int, int]:
        cols = max(1, int(terminal.columns * self.width_percentage))
        rows = max(1, int(terminal.lines * self.max_height_percentage))
        return cols, rows

    def _fit(self, image_size: Tuple[int, int], cells: Tuple[int, int]) -> Tuple[int, int]:
        """Return the pixel size to resize to, preserving aspect ratio.

        Terminal cells are roughly twice as tall as wide. Half-block modes draw
        two square pixels per cell, while ASCII draws one tall pixel per cell.
        """
        width, height = image_size
        cols, rows = cells
        pixels_per_row = 1 if self.display_mode == "ascii" else 2
        height_scale = 0.5 if self.display_mode == "ascii" else 1.0
        scale = min(cols / width, rows * pixels_per_row / (height * height_scale))
        return (
            max(1, round(width * scale)),
            max(1, round(height * height_scale * scale)),
        )

    @staticmethod
    def _load_frames(gif_path: str) -> List[Tuple[Image.Image, float]]:
        frames = []
        with Image.open(gif_path) as image:
            for frame in ImageSequence.Iterator(image):
                duration_ms = frame.info.get("duration") or 0
                # Browsers treat tiny or missing delays as 100ms; match that.
                duration = duration_ms / 1000 if duration_ms > 10 else DEFAULT_FRAME_DURATION
                frames.append((frame.convert("RGBA"), max(MIN_FRAME_DURATION, duration)))
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
        if self.display_mode == "ascii":
            lines = self._render_ascii(resized)
        else:
            lines = self._render_halfblock(resized)

        top = max(1, (terminal.lines - len(lines)) // 2 + 1)
        left = max(1, (terminal.columns - size[0]) // 2 + 1)
        output = [SYNC_START]
        for offset, line in enumerate(lines):
            output.append(f"{ESC}{top + offset};{left}H{line}")
        output.append(SYNC_END)
        return "".join(output)

    def _select_renderer(self) -> bool:
        if self.renderer == "blocks":
            return False
        if os.environ.get("TMUX") or os.environ.get("STY"):
            if self.renderer == "kitty":
                raise GIFPlayerError(
                    "Native images are not supported through tmux/screen; use --renderer blocks"
                )
            logger.debug("Using block rendering inside tmux/screen")
            return False
        if not sys.stdout.isatty():
            if self.renderer == "kitty":
                raise GIFPlayerError("Native image playback requires an interactive terminal")
            return False
        if self.renderer == "kitty":
            return True
        return bool(
            os.environ.get("KITTY_WINDOW_ID")
            or os.environ.get("TERM") in ("xterm-kitty", "xterm-ghostty")
            or os.environ.get("TERM_PROGRAM") == "ghostty"
        )

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

    def _prepare_frames(
        self, frames: List[Tuple[Image.Image, float]], terminal: os.terminal_size
    ) -> Tuple[List[str], List[str]]:
        if self._native:
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
        if self._native:
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
                    if self._native:
                        rendered = [
                            self._native_placement(image_id, image, terminal)
                            for image_id, (image, _) in zip(self._image_ids, frames)
                        ]
                    else:
                        rendered, _ = await self._run_in_executor(
                            self._prepare_frames, frames, terminal
                        )
                    self._write(ESC + "2J")
                    deadline = time.monotonic()

                deadline += duration
                # Keep the GIF's timeline when output is slow, rather than
                # showing overdue frames in a burst or slowing the animation.
                if time.monotonic() < deadline:
                    if self._native:
                        remove = (
                            self._kitty_command(
                                f"a=d,d=i,i={self._image_ids[previous]},q=2"
                            ) if previous is not None else ""
                        )
                        self._write(SYNC_START + remove + rendered[index] + SYNC_END)
                        previous = index
                    else:
                        self._write(rendered[index])
                delay = deadline - time.monotonic()
                await asyncio.sleep(max(0, delay))
            loops_done += 1

    async def play_animation(self, gif_path: str):
        """Play GIF animation in terminal

        Args:
            gif_path (str): Path to the GIF file
        """
        if not os.path.isfile(gif_path):
            raise GIFPlayerError(f"Invalid GIF source: {gif_path}")

        self._native = self._select_renderer()
        if self._native and self.acceleration == "gpu":
            logger.debug("Native-image scaling is handled by the terminal, not the OpenGL resizer")
        try:
            frames = await self._run_in_executor(self._load_frames, gif_path)
            self._image_ids = []
            if self._native:
                first_id = secrets.randbelow(1 << 30) + 1
                self._image_ids = list(range(first_id, first_id + len(frames)))
            terminal = shutil.get_terminal_size()
            logger.debug("Preparing %d frames for %s playback", len(frames),
                        "native-image" if self._native else "block")
            rendered, uploads = await self._run_in_executor(
                self._prepare_frames, frames, terminal
            )
        except GIFPlayerError:
            raise
        except Exception as e:
            raise GIFPlayerError(f"Failed to prepare GIF: {e}") from e

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
