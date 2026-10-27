import asyncio
import base64
import io
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from asciime.core.gif_player_fast import (
    ESC, SYNC_END, SYNC_START, GIFPlayer, GIFPlayerError,
)

TERMINAL = os.terminal_size((100, 30))


class RendererTests(unittest.TestCase):
    def test_default_preserves_block_art(self):
        with patch.dict(os.environ, {"TERM": "xterm-kitty"}, clear=True):
            self.assertFalse(GIFPlayer()._select_renderer())

    def test_native_auto_detection_and_cpu_terminal_fallback(self):
        with patch("sys.stdout.isatty", return_value=True):
            for term, expected in (("xterm-kitty", True), ("xterm-256color", False)):
                with patch.dict(os.environ, {"TERM": term}, clear=True):
                    self.assertEqual(GIFPlayer(renderer="auto")._select_renderer(), expected)
            with patch.dict(os.environ, {"TERM": "xterm-kitty", "TMUX": "session"}, clear=True):
                self.assertFalse(GIFPlayer(renderer="auto")._select_renderer())
                with self.assertRaisesRegex(GIFPlayerError, "tmux/screen"):
                    GIFPlayer(renderer="kitty")._select_renderer()
        with patch("sys.stdout.isatty", return_value=False):
            with self.assertRaisesRegex(GIFPlayerError, "interactive terminal"):
                GIFPlayer(renderer="kitty")._select_renderer()

    def test_invalid_backend_names_are_rejected(self):
        with self.assertRaises(ValueError):
            GIFPlayer(renderer="invalid")
        with self.assertRaises(ValueError):
            GIFPlayer(acceleration="invalid")

    def test_block_modes_and_tiny_terminal_fit(self):
        image = Image.new("RGBA", (40, 30), (30, 60, 200, 255))
        for mode in ("truecolor", "256", "256fgbg", "nocolor"):
            player = GIFPlayer(mode, acceleration="cpu")
            rendered, uploads = player._prepare_frames([(image, 0.02)], TERMINAL)
            self.assertEqual(len(rendered), 1)
            self.assertEqual(uploads, [])
            self.assertTrue(rendered[0].startswith(SYNC_START))
            self.assertTrue(rendered[0].endswith(SYNC_END))
            if mode == "nocolor":
                self.assertNotIn(ESC + "38;", rendered[0])
            else:
                self.assertIn(ESC + "48;", rendered[0])
            self.assertEqual(player._fit((40, 30), (1, 1)), (1, 1))

    def test_native_chunking_retains_original_image_resolution(self):
        player = GIFPlayer(renderer="kitty")
        player._native = True
        player._image_ids = [123]
        image = Image.effect_noise((180, 140), 80).convert("RGBA")
        rendered, uploads = player._prepare_frames([(image, 0.02)], TERMINAL)
        chunks = re.findall(r"\x1b_G([^;]*);([A-Za-z0-9+/=]*)\x1b\\", uploads[0])
        self.assertGreater(len(chunks), 1)
        self.assertIn("a=t,f=100,t=d,i=123,q=2,m=1", chunks[0][0])
        self.assertEqual(chunks[-1][0], "m=0")
        self.assertTrue(all(len(chunk) <= 4096 for _, chunk in chunks))
        encoded = "".join(chunk for _, chunk in chunks)
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as decoded:
            self.assertEqual(decoded.size, image.size)
            self.assertEqual(decoded.tobytes(), image.tobytes())
        self.assertIn("a=p,i=123,p=1", rendered[0])
        self.assertLess(len(rendered[0]), 100)

    def test_cleanup_deletes_only_owned_images_and_restores_terminal(self):
        player = GIFPlayer(renderer="kitty")
        player._native = True
        player._image_ids = [123, 124]
        with patch.object(player, "_write") as write:
            player._cleanup_terminal()
        output = write.call_args.args[0]
        self.assertIn("a=d,d=I,i=123,q=2", output)
        self.assertIn("a=d,d=I,i=124,q=2", output)
        self.assertNotIn("d=A", output)
        self.assertIn(ESC + "?25h", output)
        self.assertIn(ESC + "?1049l", output)
        self.assertTrue(output.startswith(SYNC_END))


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_once_before_playback_and_reuse_across_loops(self):
        player = GIFPlayer(loop_count=2, acceleration="cpu")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.gif"
            Image.new("RGB", (8, 8), "red").save(path)
            with patch.object(player, "_prepare_frames", wraps=player._prepare_frames) as prepare:
                with patch.object(player, "_setup_terminal") as setup:
                    with patch.object(player, "_write") as write:
                        with patch.object(player, "_cleanup_terminal") as cleanup:
                            with patch("asciime.core.gif_player_fast.STILL_IMAGE_DURATION", 0.02):
                                await player.play_animation(str(path))
            prepare.assert_called_once()
            setup.assert_called_once()
            cleanup.assert_called_once()
            self.assertEqual(write.call_count, 2)
            self.assertFalse(player._running)

    async def test_late_frames_are_skipped_without_resetting_timeline(self):
        player = GIFPlayer(loop_count=1)
        player._running = True
        image = Image.new("RGBA", (2, 2))
        frames = [(image, 0.02)] * 5
        now = [0.0]
        displayed = []

        def write(frame):
            displayed.append(frame)
            now[0] += 0.07

        async def sleep(delay):
            now[0] += delay

        with patch("asciime.core.gif_player_fast.time.monotonic", side_effect=lambda: now[0]):
            with patch("asciime.core.gif_player_fast.shutil.get_terminal_size", return_value=TERMINAL):
                with patch("asciime.core.gif_player_fast.asyncio.sleep", side_effect=sleep):
                    with patch.object(player, "_write", side_effect=write):
                        await player._play_frames(frames, ["0", "1", "2", "3", "4"], TERMINAL)
        self.assertEqual(displayed, ["0", "3"])
        self.assertAlmostEqual(now[0], 0.14)

    async def test_resize_reprepares_blocks(self):
        player = GIFPlayer(acceleration="cpu", loop_count=1)
        player._running = True
        frames = [(Image.new("RGBA", (8, 8)), 0.02)]
        changed = os.terminal_size((80, 20))
        with patch("asciime.core.gif_player_fast.shutil.get_terminal_size", return_value=changed):
            with patch.object(player, "_prepare_frames", wraps=player._prepare_frames) as prepare:
                with patch.object(player, "_write") as write:
                    await player._play_frames(frames, ["old"], TERMINAL)
        prepare.assert_called_once_with(frames, changed)
        self.assertEqual(write.call_args_list[0].args[0], ESC + "2J")

    async def test_cancel_restores_terminal(self):
        player = GIFPlayer(acceleration="cpu", loop_count=0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.gif"
            Image.new("RGB", (8, 8), "red").save(path)
            with patch.object(player, "_play_frames", new=AsyncMock(side_effect=asyncio.CancelledError)):
                with patch.object(player, "_setup_terminal"):
                    with patch.object(player, "_cleanup_terminal") as cleanup:
                        with self.assertRaises(asyncio.CancelledError):
                            await player.play_animation(str(path))
        cleanup.assert_called_once()
        self.assertFalse(player._running)

    async def test_native_uploads_before_timeline_and_only_once(self):
        player = GIFPlayer(renderer="kitty", loop_count=2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.gif"
            Image.new("RGB", (8, 8), "red").save(path)
            with patch.object(player, "_select_renderer", return_value=True):
                with patch("asciime.core.gif_player_fast.STILL_IMAGE_DURATION", 0.02):
                    with patch.object(player, "_write") as write:
                        await player.play_animation(str(path))
        writes = [call.args[0] for call in write.call_args_list]
        uploads = [text for text in writes if "a=t," in text]
        placements = [text for text in writes if "a=p," in text]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(len(placements), 2)
        self.assertLess(writes.index(uploads[0]), writes.index(placements[0]))
