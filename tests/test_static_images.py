import argparse
import contextlib
import io
import os
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from asciime import __main__ as cli
from asciime.core.cache_manager import CacheManager
from asciime.core.gif_player_fast import (
    STILL_IMAGE_DURATION, GIFPlayer, GIFPlayerError,
)


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)


class FrameLoadingTests(TempDirTestCase):
    def test_static_formats_load_as_single_still_frame(self):
        for name, mode, options in (
            ("still.png", "RGBA", {}),
            ("still.jpg", "RGB", {"quality": 95}),
            ("still.webp", "RGB", {"lossless": True}),
            ("still.bmp", "RGB", {}),
            ("still.tiff", "RGB", {}),
            ("cmyk.jpg", "CMYK", {}),
        ):
            with self.subTest(name=name):
                path = self.root / name
                # Pillow's named colors don't map to CMYK, so give CMYK red explicitly.
                color = (0, 255, 255, 0) if mode == "CMYK" else "red"
                Image.new(mode, (6, 4), color).save(path, **options)
                frames = GIFPlayer._load_frames(str(path))
                self.assertEqual(len(frames), 1)
                image, duration = frames[0]
                self.assertEqual(image.mode, "RGBA")
                self.assertEqual(image.size, (6, 4))
                self.assertEqual(duration, STILL_IMAGE_DURATION)
                red, green, blue, alpha = image.getpixel((2, 2))
                self.assertGreater(red, 200)
                self.assertLess(green + blue, 60)
                self.assertEqual(alpha, 255)

    def test_png_transparency_is_preserved(self):
        path = self.root / "alpha.png"
        image = Image.new("RGBA", (2, 1), (0, 0, 0, 0))
        image.putpixel((1, 0), (0, 255, 0, 255))
        image.save(path)
        frame = GIFPlayer._load_frames(str(path))[0][0]
        self.assertEqual(frame.getpixel((0, 0))[3], 0)
        self.assertEqual(frame.getpixel((1, 0)), (0, 255, 0, 255))

    def test_exif_orientation_is_applied(self):
        path = self.root / "rotated.jpg"
        image = Image.new("RGB", (20, 10), "blue")
        exif = image.getexif()
        exif[0x0112] = 6
        image.save(path, exif=exif)
        self.assertEqual(GIFPlayer._load_frames(str(path))[0][0].size, (10, 20))

    def test_16_bit_png_is_scaled_not_clipped_to_white(self):
        path = self.root / "deep.png"
        image = Image.new("I;16", (2, 1))
        image.putdata([0, 32768])
        image.save(path)
        frame = GIFPlayer._load_frames(str(path))[0][0]
        self.assertEqual(frame.getpixel((0, 0))[:3], (0, 0, 0))
        self.assertEqual(frame.getpixel((1, 0))[:3], (128, 128, 128))

    def test_animated_webp_and_apng_keep_frame_durations(self):
        frames = [Image.new("RGB", (4, 4), color) for color in ("red", "blue", "green")]
        for name, options in (
            ("anim.webp", {"lossless": True}),
            ("anim.png", {}),
        ):
            with self.subTest(name=name):
                path = self.root / name
                frames[0].save(
                    path, save_all=True, append_images=frames[1:],
                    duration=[50, 70, 90], loop=0, **options,
                )
                loaded = GIFPlayer._load_frames(str(path))
                self.assertEqual([round(d, 3) for _, d in loaded], [0.05, 0.07, 0.09])
                self.assertEqual(loaded[1][0].getpixel((0, 0))[:3], (0, 0, 255))

    def test_unreadable_file_reports_load_error(self):
        path = self.root / "broken.png"
        path.write_bytes(b"not an image")
        with self.assertRaisesRegex(GIFPlayerError, "Failed to load image"):
            import asyncio
            asyncio.run(GIFPlayer(acceleration="cpu").play_animation(str(path)))


class CacheListingTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.cache = CacheManager(str(self.root))
        for relative in ("all/nier.gif", "all/nier.png", "art/photo.JPG",
                         "art/sticker.webp", "loose.bmp"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (2, 2)).save(path, format=Image.registered_extensions()[path.suffix.lower()])
        (self.root / "art" / "notes.txt").write_text("not an image")

    def test_lists_supported_images_only_with_extensions(self):
        labels = [entry["label"] for entry in self.cache.list_cached()]
        self.assertEqual(labels, ["loose.bmp", "all/nier.gif", "all/nier.png",
                                  "art/photo.JPG", "art/sticker.webp"])

    def test_matching_with_and_without_extension(self):
        entries = self.cache.list_cached()

        def labels(query):
            return [e["label"] for e in cli.match_cached_gifs(entries, query)]

        self.assertEqual(labels("nier"), ["all/nier.gif", "all/nier.png"])
        self.assertEqual(labels("nier.png"), ["all/nier.png"])
        self.assertEqual(labels("all/nier.gif"), ["all/nier.gif"])
        self.assertEqual(labels("photo"), ["art/photo.JPG"])
        self.assertEqual(labels("stick"), ["art/sticker.webp"])
        self.assertEqual(labels("1"), ["loose.bmp"])

    def test_same_stem_files_are_tracked_separately(self):
        for entry in self.cache.list_cached():
            if entry["name"] == "nier":
                self.cache.mark_played(entry["path"])
        connection = sqlite3.connect(self.cache.db_path)
        try:
            rows = sorted(connection.execute("SELECT id, play_count FROM gifs"))
        finally:
            connection.close()
        self.assertEqual(rows, [("all/nier.gif", 1), ("all/nier.png", 1)])


class FileOptionTests(TempDirTestCase):
    def test_short_and_long_file_flags_and_conflicts(self):
        with patch("sys.argv", ["asciime", "-f", "a.png", "-n", "2"]):
            short = cli.parse_arguments()
        with patch("sys.argv", ["asciime", "--file", "a.png", "--repeat", "2"]):
            self.assertEqual(short, cli.parse_arguments())
        for conflict in (["-f", "a.png", "-c"], ["-f", "a.png", "-r"]):
            with patch("sys.argv", ["asciime", *conflict]):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        cli.parse_arguments()
            self.assertEqual(error.exception.code, 2)

    def test_resolve_image_file(self):
        image = self.root / "pic.png"
        Image.new("RGB", (1, 1)).save(image)
        self.assertEqual(cli.resolve_image_file(str(image)), str(image.resolve()))
        with self.assertRaisesRegex(cli.CacheSelectionError, "Image file not found"):
            cli.resolve_image_file(str(self.root / "missing.png"))
        with self.assertRaisesRegex(cli.CacheSelectionError, "Image file not found"):
            cli.resolve_image_file(str(self.root))


class FileWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_playback_skips_api_and_cache_tracking(self):
        args = argparse.Namespace(file_path="/tmp/pic.png", cache_path=None, random=False,
                                  screensaver=True, repeat=None)
        with patch.object(cli, "ConfigManager") as manager:
            manager.return_value.load_config.return_value = {"loop_count": 2}
            with patch.object(cli, "setup_logging"):
                with patch.object(cli, "play_cached_gif", new=AsyncMock()) as play:
                    with patch.object(cli, "GIFFetcher") as fetcher:
                        await cli.async_main(args)
        fetcher.assert_not_called()
        self.assertEqual(play.call_args.args[1], "/tmp/pic.png")
        self.assertFalse(play.call_args.kwargs["record_play"])
        self.assertEqual(play.call_args.args[0]["loop_count"], 0)

    async def test_play_without_recording_does_not_touch_cache(self):
        config = {"display_mode": "truecolor", "loop_count": 1}
        with patch.object(cli, "GIFPlayer") as player:
            player.return_value.play_animation = AsyncMock()
            with patch.object(cli, "CacheManager") as cache:
                await cli.play_cached_gif(config, "/tmp/pic.png", record_play=False)
        cache.assert_not_called()
        player.return_value.play_animation.assert_awaited_once_with("/tmp/pic.png")


@unittest.skipUnless(os.name == "posix", "POSIX subprocess integration tests")
class StaticImageCliTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.image = self.root / "photo.jpg"
        Image.new("RGB", (40, 30), "orange").save(self.image)
        self.env = dict(os.environ, HOME=str(self.root))
        self.command = [sys.executable, "-m", "asciime", "-a", "cpu", "-f", str(self.image)]

    def test_static_image_plays_once_and_restores_terminal(self):
        start = time.monotonic()
        result = subprocess.run([*self.command, "-n", "1"], env=self.env,
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(time.monotonic() - start, STILL_IMAGE_DURATION)
        self.assertEqual(result.stdout.count(b"\x1b[?2026h"), 1)
        self.assertIn(b"\x1b[48;2;255;165;0m", result.stdout)
        self.assertIn(b"\x1b[?1049l", result.stdout)
        self.assertFalse((self.root / ".cache" / "asciime" / "meta.db").exists())

    def test_static_screensaver_holds_until_sigterm(self):
        process = subprocess.Popen([*self.command, "-s"], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = bytearray()
        try:
            deadline = time.monotonic() + 10
            while b"\x1b[?2026h" not in output:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, "Image was not displayed")
                ready, _, _ = select.select([process.stdout], [], [], remaining)
                self.assertTrue(ready, "Image was not displayed")
                chunk = os.read(process.stdout.fileno(), 65536)
                self.assertTrue(chunk, "Playback exited prematurely")
                output.extend(chunk)
            time.sleep(0.5)
            self.assertIsNone(process.poll())
            process.send_signal(signal.SIGTERM)
            tail, errors = process.communicate(timeout=10)
            output.extend(tail)
            self.assertEqual(process.returncode, 0, errors)
            self.assertIn(b"\x1b[?1049l", output)
            self.assertNotIn(b"Traceback", bytes(output) + errors)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_missing_file_reports_error(self):
        result = subprocess.run(
            [sys.executable, "-m", "asciime", "-f", str(self.root / "nope.png")],
            env=self.env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Image file not found", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
