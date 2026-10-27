import asyncio
import concurrent.futures
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from asciime.core import sixel
from asciime.core.gif_player_fast import (
    ESC, SYNC_END, SYNC_START, GIFPlayer, GIFPlayerError, _da1_reports_sixel,
    _split_cell, sextant_char,
)

MODULE = "asciime.core.gif_player_fast"
TERMINAL = os.terminal_size((80, 24))


def _gradient(size=(64, 48)):
    image = Image.new("RGBA", size)
    draw = ImageDraw.Draw(image)
    for x in range(size[0]):
        draw.line([(x, 0), (x, size[1])], fill=(x * 4 % 256, 255 - x * 4 % 256, 90, 255))
    return image


class SixelEncoderTests(unittest.TestCase):
    def test_round_trip_preserves_opaque_and_transparent_pixels(self):
        image = Image.new("RGBA", (13, 9), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle([2, 1, 10, 7], fill=(255, 0, 0, 255))
        draw.point((0, 8), fill=(0, 0, 255, 255))
        frame = sixel.frame_to_sixel(image)
        self.assertTrue(frame.transparent)
        self.assertTrue(frame.data.startswith('\x1bP0;1;0q"1;1;13;9'))
        self.assertTrue(frame.data.endswith("\x1b\\"))
        self.assertEqual(sixel.decode(frame.data).tobytes(), image.tobytes())

    def test_palette_is_capped_and_runs_are_compressed(self):
        frame = sixel.frame_to_sixel(_gradient((400, 12)), still=True)
        colors = {int(n) for n in re.findall(r"#(\d+);2;", frame.data)}
        self.assertLessEqual(len(colors), 255)
        self.assertNotIn(sixel.TRANSPARENT_INDEX, colors)
        self.assertFalse(frame.transparent)
        solid = sixel.frame_to_sixel(Image.new("RGBA", (200, 6), (9, 9, 9, 255)))
        self.assertIn("!200~", solid.data)

    @unittest.skipIf(sixel.np is None, "numpy not installed")
    def test_numpy_and_pure_python_encoders_are_identical(self):
        image = _gradient((97, 31))
        ImageDraw.Draw(image).ellipse([10, 3, 60, 28], fill=(0, 0, 0, 0))
        for still in (False, True):
            paletted = sixel.quantize(image, still)
            self.assertEqual(sixel._encode_numpy(paletted), sixel._encode_pure(paletted))


class SextantTests(unittest.TestCase):
    def test_character_mapping(self):
        self.assertEqual(sextant_char(0), " ")
        self.assertEqual(sextant_char(63), "\u2588")
        self.assertEqual(sextant_char(21), "\u258c")
        self.assertEqual(sextant_char(42), "\u2590")
        self.assertEqual(sextant_char(1), "\U0001FB00")
        self.assertEqual(sextant_char(20), "\U0001FB13")
        self.assertEqual(sextant_char(22), "\U0001FB14")
        self.assertEqual(sextant_char(43), "\U0001FB28")
        self.assertEqual(sextant_char(62), "\U0001FB3B")
        glyphs = {sextant_char(p) for p in range(1, 63)} - {"\u258c", "\u2590"}
        self.assertEqual(len(glyphs), 60)

    def test_cell_split_uses_contrasting_colors_and_transparency(self):
        black, white = (0, 0, 0), (250, 250, 250)
        pattern, fg, bg = _split_cell([(bit, white if bit % 2 else black) for bit in range(6)])
        self.assertEqual((pattern, fg, bg), (42, white, black))
        self.assertEqual(_split_cell([(bit, white) for bit in range(6)]), (0, None, white))
        self.assertEqual(_split_cell([(0, white), (5, black)]), (33, (125, 125, 125), None))
        self.assertEqual(_split_cell([]), (0, None, None))

    def test_render_uses_finer_grid_and_falls_back_to_ascii(self):
        player = GIFPlayer(renderer="sextant", acceleration="cpu")
        player._mode = player._select_renderer()
        image = Image.new("RGBA", (40, 30), (255, 255, 255, 255))
        ImageDraw.Draw(image).polygon([(0, 0), (39, 29), (0, 29)], fill=(0, 0, 0, 255))
        self.assertEqual(player._fit((40, 30), (10, 10)), (20, 11))
        cell = Image.new("RGBA", (2, 3), (255, 255, 255, 255))
        ImageDraw.Draw(cell).line([(0, 0), (0, 2)], fill=(0, 0, 0, 255))
        self.assertEqual(
            player._render_sextant(cell),
            [f"{ESC}38;2;255;255;255m{ESC}48;2;0;0;0m\u2590{ESC}0m"],
        )
        rendered, _ = player._prepare_frames([(image, 0.1)], TERMINAL)
        self.assertTrue(any(0x1FB00 <= ord(c) <= 0x1FB3B or c in "\u2590\u258c" for c in rendered[0]))
        self.assertIn(ESC + "38;2;", rendered[0])

        ascii_player = GIFPlayer("nocolor", renderer="sextant", acceleration="cpu")
        ascii_player._mode = ascii_player._select_renderer()
        rendered, _ = ascii_player._prepare_frames([(image, 0.1)], TERMINAL)
        self.assertNotIn(ESC + "38;", rendered[0])
        self.assertFalse(any(0x1FB00 <= ord(c) <= 0x1FB3B for c in rendered[0]))


def _resolve(rendered):
    return [f.result(timeout=60) if isinstance(f, concurrent.futures.Future) else f for f in rendered]


class SixelPlayerTests(unittest.TestCase):
    def _player(self, **kwargs):
        player = GIFPlayer(renderer="sixel", acceleration="cpu", **kwargs)
        player._mode = "sixel"
        self.addCleanup(player._shutdown_pool)
        return player

    def test_frame_fits_reserved_rows_and_is_centered(self):
        player = self._player()
        with patch(f"{MODULE}._cell_pixels", return_value=(10, 20)):
            rendered, uploads = player._prepare_frames([(_gradient(), 0.1)], TERMINAL)
        self.assertEqual(uploads, [])
        output = rendered[0]
        self.assertTrue(output.startswith(SYNC_START) and output.endswith(SYNC_END))
        width, height = map(int, re.search(r'"1;1;(\d+);(\d+)', output).groups())
        rows = int(24 * 0.8)
        self.assertLessEqual(height, rows * 20)
        self.assertEqual(height % 6, 0)
        self.assertAlmostEqual(width / height, 64 / 48, delta=0.02)
        top, left = map(int, re.search(r"\x1b\[(\d+);(\d+)H\x1bP", output).groups())
        self.assertEqual(top, (24 - -(-height // 20)) // 2 + 1)
        self.assertEqual(left, (80 - -(-width // 10)) // 2 + 1)
        self.assertNotIn(ESC + "X", output.split("\x1bP")[0])

    def test_transparent_frames_erase_their_footprint_first(self):
        image = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        ImageDraw.Draw(image).ellipse([5, 5, 35, 35], fill=(200, 10, 10, 255))
        player = self._player()
        with patch(f"{MODULE}._cell_pixels", return_value=(10, 20)):
            rendered, _ = player._prepare_frames([(image, 0.1), (image, 0.1)], TERMINAL)
        rendered = _resolve(rendered)
        erase = re.findall(r"\x1b\[\d+;\d+H\x1b\[(\d+)X", rendered[0])
        self.assertTrue(erase)
        self.assertLess(rendered[0].index(ESC + erase[0] + "X"), rendered[0].index("\x1bP"))
        self.assertEqual(rendered[0], rendered[1])

    def test_animation_frames_are_encoded_by_workers_like_serial(self):
        frames = [(_gradient((30 + i, 20)), 0.1) for i in range(3)]
        player = self._player()
        rendered, _ = player._prepare_frames(frames, TERMINAL)
        self.assertTrue(all(isinstance(f, concurrent.futures.Future) for f in rendered))
        with patch.object(player, "_sixel_pool", return_value=None):
            serial, _ = player._prepare_frames(frames, TERMINAL)
        self.assertTrue(all(isinstance(f, str) for f in serial))
        self.assertEqual(_resolve(rendered), list(serial))

    def test_nocolor_sixel_is_grayscale(self):
        player = self._player(display_mode="nocolor")
        rendered, _ = player._prepare_frames([(_gradient(), 0.1)], TERMINAL)
        for r, g, b in re.findall(r"#\d+;2;(\d+);(\d+);(\d+)", rendered[0]):
            self.assertEqual(len({r, g, b}), 1)


class SixelPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_playback_waits_for_pending_frames_and_shuts_down_pool(self):
        player = GIFPlayer(renderer="sixel", acceleration="cpu", loop_count=1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "anim.gif"
            frames = [Image.new("RGB", (24, 12), color) for color in ("red", "green", "blue")]
            frames[0].save(path, save_all=True, append_images=frames[1:], duration=20)
            with patch.object(player, "_select_renderer", return_value="sixel"), \
                    patch(f"{MODULE}._cell_pixels", return_value=(10, 20)), \
                    patch("shutil.get_terminal_size", return_value=TERMINAL), \
                    patch.object(player, "_write") as write:
                await player.play_animation(str(path))
        sixels = [c.args[0] for c in write.call_args_list if "\x1bP0;1;0q" in c.args[0]]
        self.assertEqual(len(sixels), 3)
        self.assertEqual(len(set(sixels)), 3)
        self.assertIsNone(player._pool)

    async def test_cancel_while_waiting_for_a_frame_cleans_up(self):
        player = GIFPlayer(renderer="sixel", acceleration="cpu")
        player._mode = "sixel"
        pending = concurrent.futures.Future()
        task = asyncio.create_task(player._resolve_frame([pending], 0))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(pending.cancelled())


class SelectionTests(unittest.TestCase):
    def test_da1_parsing(self):
        self.assertTrue(_da1_reports_sixel(b"\x1b[?61;4;6;7;14;21;22;23;24;28;32;42c"))
        self.assertTrue(_da1_reports_sixel(b"\x1b[?4c"))
        self.assertFalse(_da1_reports_sixel(b"\x1b[?62;22c"))
        self.assertFalse(_da1_reports_sixel(b"\x1b[?1;2;14c"))
        self.assertFalse(_da1_reports_sixel(b""))

    def test_auto_prefers_kitty_then_sixel_then_blocks(self):
        with patch("sys.stdout.isatty", return_value=True):
            with patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True):
                with patch(f"{MODULE}._terminal_supports_sixel", return_value=True):
                    self.assertEqual(GIFPlayer(renderer="auto")._select_renderer(), "sixel")
                with patch(f"{MODULE}._terminal_supports_sixel", return_value=False):
                    self.assertEqual(GIFPlayer(renderer="auto")._select_renderer(), "blocks")
            with patch.dict(os.environ, {"TERM": "xterm-kitty"}, clear=True), \
                    patch(f"{MODULE}._terminal_supports_sixel") as query:
                self.assertEqual(GIFPlayer(renderer="auto")._select_renderer(), "kitty")
                query.assert_not_called()
            with patch.dict(os.environ, {"TMUX": "1"}, clear=True), \
                    patch(f"{MODULE}._terminal_supports_sixel") as query:
                self.assertEqual(GIFPlayer(renderer="auto")._select_renderer(), "blocks")
                self.assertEqual(GIFPlayer(renderer="sixel")._select_renderer(), "sixel")
                query.assert_not_called()
        with patch("sys.stdout.isatty", return_value=False):
            with self.assertRaisesRegex(GIFPlayerError, "interactive terminal"):
                GIFPlayer(renderer="sixel")._select_renderer()
        self.assertEqual(GIFPlayer(renderer="sextant")._select_renderer(), "sextant")


if __name__ == "__main__":
    unittest.main()
